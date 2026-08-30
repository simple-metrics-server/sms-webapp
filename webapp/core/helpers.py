import logging
import math
import re
import shlex
from collections.abc import Iterable

from webapp.core.model import (
    DistributionSample,
    LabeledSample,
    Metric,
    SampleValue,
)
from webapp.core.process import ProcessRunner

log = logging.getLogger(__name__)

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


class HandlerSupport:
    """Shared machinery for metric handlers.

    Handlers compose this object instead of re-implementing the common
    phases: config verification, command execution with output parsing
    and exposition rendering.
    """

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner()

    def verify_metrics(
        self,
        metrics: Iterable[Metric],
        required_fields: list[str],
        source: str,
    ) -> None:
        """Validate metrics. Collect all problems into one ValueError.

        Generic checks: valid and unique series names, known value_type,
        numeric timeout > 0. Additionally each field in required_fields
        must be non-empty and parseable as a command line. Histograms
        require a valid `buckets` list, summaries a valid `quantiles`
        list, and those fields are rejected on any other type.
        """
        errors: list[str] = []
        seen: dict[str, str] = {}
        for m in metrics:
            if not METRIC_NAME_RE.match(m.name):
                errors.append(f"invalid metric name: {m.name!r}")
            for series in series_names(m):
                if series in seen:
                    errors.append(
                        f"duplicate series {series!r} for metrics "
                        f"{seen[series]!r} and {m.name!r}"
                    )
                else:
                    seen[series] = m.name
            if m.value_type not in VALID_TYPES:
                errors.append(
                    f"invalid value_type {m.value_type!r} for {m.name!r}; "
                    f"must be one of {sorted(VALID_TYPES)}"
                )
            if m.value_type == "histogram":
                self._verify_doubles(m, "buckets", m.buckets, errors)
            elif m.buckets is not None:
                errors.append(
                    f"'buckets' only valid for value_type 'histogram', "
                    f"got {m.value_type!r} for {m.name!r}"
                )
            if m.value_type == "summary":
                self._verify_doubles(
                    m, "quantiles", m.quantiles, errors, low=0.0, high=1.0
                )
            elif m.quantiles is not None:
                errors.append(
                    f"'quantiles' only valid for value_type 'summary', "
                    f"got {m.value_type!r} for {m.name!r}"
                )
            for field in required_fields:
                self._verify_cmd_field(m, field, errors)
            if not isinstance(m.timeout, (int, float)) or isinstance(
                m.timeout, bool
            ):
                errors.append(f"timeout must be a number for {m.name!r}")
            elif m.timeout <= 0:
                errors.append(f"timeout must be > 0 for {m.name!r}")
        if errors:
            msg = f"Invalid config {source}:\n" + "\n".join(
                f"  - {e}" for e in errors
            )
            log.error(msg)
            raise ValueError(msg)

    def _verify_doubles(
        self,
        m: Metric,
        field: str,
        values: object,
        errors: list[str],
        low: float | None = None,
        high: float | None = None,
    ) -> None:
        """Validate a declared bounds/quantiles list in place."""
        if not isinstance(values, list) or not values:
            errors.append(f"{field!r} must be a non-empty list for {m.name!r}")
            return
        prev: float | None = None
        for v in values:
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                errors.append(
                    f"{field!r} values must be numbers for {m.name!r}"
                )
                continue
            v = float(v)
            if not math.isfinite(v):
                errors.append(f"{field!r} values must be finite for {m.name!r}")
                continue
            if low is not None and v <= low:
                errors.append(
                    f"{field!r} values must be > {low} for {m.name!r}, "
                    f"got {v!r}"
                )
            if high is not None and v >= high:
                errors.append(
                    f"{field!r} values must be < {high} for {m.name!r}, "
                    f"got {v!r}"
                )
            if prev is not None and v <= prev:
                errors.append(
                    f"{field!r} must be strictly ascending for {m.name!r}"
                )
            prev = v

    def _verify_cmd_field(
        self, m: Metric, field: str, errors: list[str]
    ) -> None:
        value = getattr(m, field, None)
        if not isinstance(value, str) or not value:
            errors.append(f"empty or missing {field!r} for {m.name!r}")
            return
        try:
            if not shlex.split(value):
                errors.append(f"empty {field!r} for {m.name!r}")
        except ValueError as e:
            errors.append(f"unparseable {field!r} for {m.name!r}: {e}")

    async def run_cmds(
        self, metrics: Iterable[Metric]
    ) -> dict[str, SampleValue]:
        """Execute the cmd of each metric, return {name: parsed value}.

        All commands run concurrently via the ProcessRunner. Scalar
        metrics must print a single numeric line (canonicalized to
        exposition float syntax); histograms and summaries print one
        `key value` line per series plus `sum`/`count`. Raises
        ExceptionGroup on any failure.
        """
        cmds = {m.name: (shlex.split(m.cmd), m.timeout) for m in metrics}
        raw = await self.runner.run_many(cmds)
        results: dict[str, SampleValue] = {}
        errors: list[Exception] = []
        for m in metrics:
            try:
                results[m.name] = self._parse_output(m, raw[m.name])
            except ValueError as e:
                errors.append(e)
        if errors:
            raise ExceptionGroup("metric output parsing failed", errors)
        return results

    def _parse_output(self, m: Metric, raw: str) -> str | DistributionSample:
        if m.value_type in DISTRIBUTION_TYPES:
            return self.parse_distribution(m, raw)
        return self.parse_value(m.name, raw)

    def parse_value(self, name: str, raw: str) -> str:
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
        return self._canonical_float(name, value)

    def parse_distribution(self, m: Metric, raw: str) -> DistributionSample:
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
                    f"metric {m.name!r}: expected 'key value' line, "
                    f"got {line!r}"
                )
                continue
            key, value = parts
            if key in ("sum", "count"):
                try:
                    canonical = self._canonical_float(m.name, value)
                except ValueError as e:
                    errors.append(str(e))
                    continue
                if key == "sum":
                    sum_value = canonical
                else:
                    count_value = canonical
                    if float(canonical) < 0:
                        errors.append(
                            f"metric {m.name!r}: count must be >= 0, "
                            f"got {value!r}"
                        )
                continue
            try:
                bound = float(key)
            except ValueError:
                errors.append(f"metric {m.name!r}: unknown key {key!r}")
                continue
            if not math.isfinite(bound):
                errors.append(
                    f"metric {m.name!r}: key must be finite, got {key!r}"
                )
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
                values[bound] = self._canonical_float(m.name, value)
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
                        f"metric {m.name!r}: bucket counts must be "
                        f"non-decreasing"
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

    def _canonical_float(self, name: str, value: str) -> str:
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
        self,
        metrics: Iterable[Metric],
        results: dict[str, SampleValue],
    ) -> str:
        """Render metrics with values into Prometheus exposition format."""
        blocks = []
        for m in metrics:
            sample = self._render_sample(m, results[m.name])
            blocks.append(
                f"# HELP {m.name} {self._escape_help(m.help_text)}\n"
                f"# TYPE {m.name} {m.value_type}\n"
                f"{sample}"
            )
        return "\n\n".join(blocks) + "\n" if blocks else ""

    def _render_sample(self, m: Metric, value: SampleValue) -> str:
        if m.value_type == "histogram":
            assert isinstance(value, DistributionSample)
            return self._render_histogram(m, value)
        if m.value_type == "summary":
            assert isinstance(value, DistributionSample)
            return self._render_summary(m, value)
        if isinstance(value, LabeledSample):
            return self._render_labeled(m, value)
        return f"{m.name} {value}"

    def _render_histogram(self, m: Metric, sample: DistributionSample) -> str:
        assert m.buckets is not None
        lines = [
            f'{m.name}_bucket{{le="{float(b)!r}"}} {sample.values[float(b)]}'
            for b in m.buckets
        ]
        lines.append(f'{m.name}_bucket{{le="+Inf"}} {sample.count_value}')
        lines.append(f"{m.name}_sum {sample.sum_value}")
        lines.append(f"{m.name}_count {sample.count_value}")
        return "\n".join(lines)

    def _render_summary(self, m: Metric, sample: DistributionSample) -> str:
        assert m.quantiles is not None
        lines = [
            f'{m.name}{{quantile="{float(q)!r}"}} {sample.values[float(q)]}'
            for q in m.quantiles
        ]
        lines.append(f"{m.name}_sum {sample.sum_value}")
        lines.append(f"{m.name}_count {sample.count_value}")
        return "\n".join(lines)

    def _render_labeled(self, m: Metric, sample: LabeledSample) -> str:
        label = self._escape_label_value
        return "\n".join(
            f'{m.name}{{{sample.label_name}="{label(v)}"}} {sample.values[v]}'
            for v in sorted(sample.values)
        )

    def _escape_help(self, help_text: str) -> str:
        return help_text.replace("\\", "\\\\").replace("\n", "\\n")

    def _escape_label_value(self, value: str) -> str:
        return (
            value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        )
