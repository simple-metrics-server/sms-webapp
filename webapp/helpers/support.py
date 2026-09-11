import logging
import math
import shlex
from collections.abc import Iterable

from webapp.common.model import DistributionSample, Metric, SampleValue
from webapp.core.process import ProcessRunner
from webapp.helpers import exposition

log = logging.getLogger(__name__)


class HandlerSupport:
    """Shared machinery for metric handlers.

    Handlers compose this object instead of re-implementing the common
    phases: config verification, command execution with output parsing
    and exposition rendering. The format details live in
    `webapp.helpers.exposition`; this class is the handler-facing
    facade.
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
            if not exposition.METRIC_NAME_RE.match(m.name):
                errors.append(f"invalid metric name: {m.name!r}")
            for series in exposition.series_names(m):
                if series in seen:
                    errors.append(
                        f"duplicate series {series!r} for metrics "
                        f"{seen[series]!r} and {m.name!r}"
                    )
                else:
                    seen[series] = m.name
            if m.value_type not in exposition.VALID_TYPES:
                errors.append(
                    f"invalid value_type {m.value_type!r} for {m.name!r}; "
                    f"must be one of {sorted(exposition.VALID_TYPES)}"
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
        if m.value_type in exposition.DISTRIBUTION_TYPES:
            return exposition.parse_distribution(m, raw)
        return exposition.parse_value(m.name, raw)

    def parse_value(self, name: str, raw: str) -> str:
        """Validate raw output and return a canonical float string."""
        return exposition.parse_value(name, raw)

    def parse_distribution(self, m: Metric, raw: str) -> DistributionSample:
        """Parse multiline histogram/summary output."""
        return exposition.parse_distribution(m, raw)

    def render_exposition(
        self,
        metrics: Iterable[Metric],
        results: dict[str, SampleValue],
    ) -> str:
        """Render metrics with values into Prometheus exposition format."""
        return exposition.render_exposition(metrics, results)
