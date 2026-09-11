import math
import re
from collections.abc import Iterable

from webapp.common.model import (
    DistributionSample,
    LabeledSample,
    Metric,
    MultiLabeledSample,
    SampleValue,
)

METRIC_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
VALID_TYPES = {"counter", "gauge", "untyped", "histogram", "summary"}

DISTRIBUTION_TYPES = {"histogram", "summary"}


def series_names(m: Metric) -> list[str]:
    """Return every exposition series name a metric emits.

    Scalars export a single series named after the metric. Histograms
    export `<name>_bucket`, `<name>_sum` and `<name>_count`; summaries
    export `<name>` (the quantile samples), `<name>_sum` and
    `<name>_count`.
    """
    if m.value_type == "histogram":
        return [f"{m.name}_bucket", f"{m.name}_sum", f"{m.name}_count"]
    if m.value_type == "summary":
        return [m.name, f"{m.name}_sum", f"{m.name}_count"]
    return [m.name]


def parse_value(name: str, raw: str) -> str:
    """Validate raw output and return a canonical float string.

    Rejects empty, multiline, non-numeric and non-finite (nan/inf)
    values; the returned string is valid exposition format float
    syntax (repr of the parsed float).
    """
    value = raw.strip()
    if not value or "\n" in value:
        raise ValueError(
            f"metric {name!r}: expected a single numeric line, got {raw!r}"
        )
    return canonical_float(name, value)


def parse_distribution(m: Metric, raw: str) -> DistributionSample:
    """Parse multiline histogram/summary output.

    Every non-empty line must be a `key value` pair. Keys are the
    declared bounds/quantiles (matched numerically) plus the reserved
    `sum` and `count`; each of them is required exactly once. Bucket
    counts must be non-decreasing and the last one not larger than
    `count`.
    """
    key_field = "buckets" if m.value_type == "histogram" else "quantiles"
    declared = m.buckets if m.value_type == "histogram" else m.quantiles
    assert declared is not None
    declared_set = set(declared)
    values: dict[float, str] = {}
    sum_value: str | None = None
    count_value: str | None = None
    errors: list[str] = []

    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 2:
            errors.append(
                f"metric {m.name!r}: expected 'key value' line, got {line!r}"
            )
            continue
        key, value = parts
        if key in ("sum", "count"):
            try:
                canonical = canonical_float(m.name, value)
            except ValueError as e:
                errors.append(str(e))
                continue
            if key == "sum":
                sum_value = canonical
            else:
                count_value = canonical
                if float(canonical) < 0:
                    errors.append(
                        f"metric {m.name!r}: count must be >= 0, got {value!r}"
                    )
            continue
        try:
            bound = float(key)
        except ValueError:
            errors.append(f"metric {m.name!r}: unknown key {key!r}")
            continue
        if not math.isfinite(bound):
            errors.append(f"metric {m.name!r}: key must be finite, got {key!r}")
            continue
        if bound not in declared_set:
            errors.append(
                f"metric {m.name!r}: {key_field} key {key!r} not declared"
            )
            continue
        if bound in values:
            errors.append(
                f"metric {m.name!r}: duplicate {key_field} key {key!r}"
            )
            continue
        try:
            values[bound] = canonical_float(m.name, value)
        except ValueError as e:
            errors.append(str(e))

    missing = declared_set - set(values)
    if missing:
        errors.append(
            f"metric {m.name!r}: missing {key_field} values for "
            f"{sorted(missing)!r}"
        )
    if sum_value is None:
        errors.append(f"metric {m.name!r}: missing 'sum' line")
    if count_value is None:
        errors.append(f"metric {m.name!r}: missing 'count' line")

    if m.value_type == "histogram" and count_value is not None:
        prev: float | None = None
        for bound in declared:
            if bound not in values:
                break
            v = float(values[bound])
            if prev is not None and v < prev:
                errors.append(
                    f"metric {m.name!r}: bucket counts must be non-decreasing"
                )
                break
            prev = v
        if prev is not None and float(count_value) < prev:
            errors.append(
                f"metric {m.name!r}: count must be >= last bucket count"
            )

    if errors:
        raise ValueError("; ".join(errors))
    return DistributionSample(
        values=values,
        sum_value=sum_value or "",
        count_value=count_value or "",
    )


def canonical_float(name: str, value: str) -> str:
    """Return the canonical float string of a single numeric token."""
    if not value or "_" in value:
        raise ValueError(f"metric {name!r}: not a number: {value!r}")
    try:
        v = float(value)
    except ValueError:
        raise ValueError(
            f"metric {name!r}: output is not a number: {value!r}"
        ) from None
    if not math.isfinite(v):
        raise ValueError(
            f"metric {name!r}: value must be finite, got {value!r}"
        )
    return repr(v)


def render_exposition(
    metrics: Iterable[Metric],
    results: dict[str, SampleValue],
) -> str:
    """Render metrics with values into Prometheus exposition format."""
    blocks = []
    for m in metrics:
        sample = _render_sample(m, results[m.name])
        blocks.append(
            f"# HELP {m.name} {_escape_help(m.help_text)}\n"
            f"# TYPE {m.name} {m.value_type}\n"
            f"{sample}"
        )
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _render_sample(m: Metric, value: SampleValue) -> str:
    if m.value_type == "histogram":
        assert isinstance(value, DistributionSample)
        return _render_histogram(m, value)
    if m.value_type == "summary":
        assert isinstance(value, DistributionSample)
        return _render_summary(m, value)
    if isinstance(value, LabeledSample):
        return _render_labeled(m, value)
    if isinstance(value, MultiLabeledSample):
        return _render_multi_labeled(m, value)
    return f"{m.name} {value}"


def _render_histogram(m: Metric, sample: DistributionSample) -> str:
    assert m.buckets is not None
    lines = [
        f'{m.name}_bucket{{le="{float(b)!r}"}} {sample.values[float(b)]}'
        for b in m.buckets
    ]
    lines.append(f'{m.name}_bucket{{le="+Inf"}} {sample.count_value}')
    lines.append(f"{m.name}_sum {sample.sum_value}")
    lines.append(f"{m.name}_count {sample.count_value}")
    return "\n".join(lines)


def _render_summary(m: Metric, sample: DistributionSample) -> str:
    assert m.quantiles is not None
    lines = [
        f'{m.name}{{quantile="{float(q)!r}"}} {sample.values[float(q)]}'
        for q in m.quantiles
    ]
    lines.append(f"{m.name}_sum {sample.sum_value}")
    lines.append(f"{m.name}_count {sample.count_value}")
    return "\n".join(lines)


def _render_labeled(m: Metric, sample: LabeledSample) -> str:
    return "\n".join(
        f'{m.name}{{{sample.label_name}="{_escape_label_value(v)}"}} '
        f"{sample.values[v]}"
        for v in sorted(sample.values)
    )


def _render_multi_labeled(m: Metric, sample: MultiLabeledSample) -> str:
    lines = []
    for key in sorted(sample.rows):
        labels = ",".join(
            f'{name}="{_escape_label_value(v)}"'
            for name, v in zip(sample.label_names, key, strict=True)
        )
        lines.append(f"{m.name}{{{labels}}} {sample.rows[key]}")
    return "\n".join(lines)


def _escape_help(help_text: str) -> str:
    return help_text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
