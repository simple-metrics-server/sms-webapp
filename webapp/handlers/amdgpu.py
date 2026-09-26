import json
import logging
import math
from collections.abc import Callable
from typing import ClassVar

from webapp.common.model import Metric, MultiLabeledSample, SampleValue
from webapp.core.base import MetricHandler
from webapp.helpers import exposition
from webapp.helpers.util import run_command, to_float

log = logging.getLogger(__name__)

AMD_SMI_ARGV = ["amd-smi", "monitor", "--json"]
"""One one-shot amd-smi invocation serves all metrics of a cycle."""

UNTHROTTLED = "UNTHROTTLED"
GPU_LABEL = "gpu"

GpuEntry = tuple[str, dict]
"""A parsed GPU row: its `gpu` label value plus the raw JSON entry."""


def _number(value: object) -> str | None:
    """Canonical float string of a JSON scalar, None when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return repr(number) if math.isfinite(number) else None
    if isinstance(value, str):
        number = to_float(value.strip())
        return None if number is None else repr(number)
    return None


def field_number(entry: dict, key: str) -> str | None:
    """Extract one numeric field from a GPU entry.

    amd-smi reports `{"value": n, "unit": u}` objects, plain numbers
    and plain strings; `N/A` (or anything non-numeric) yields None.
    """
    value = entry.get(key)
    if isinstance(value, dict):
        value = value.get("value")
    return _number(value)


def gpu_label(entry: dict, index: int) -> str:
    """The gpu label value: the reported id, else the row position."""
    gpu = entry.get("gpu")
    if isinstance(gpu, int) and not isinstance(gpu, bool):
        return str(gpu)
    return str(index)


def parse_amd_smi(text: str) -> list[dict]:
    """Parse `amd-smi monitor --json` output into per-GPU entries.

    Returns [] when the output is not a JSON array of objects.
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, dict)]


def _column(key: str) -> Callable[[list[GpuEntry]], SampleValue]:
    """Extractor factory: one numeric JSON field per GPU."""

    def extract(parsed: list[GpuEntry]) -> SampleValue:
        rows: dict[tuple[str, ...], str] = {}
        for label, entry in parsed:
            value = field_number(entry, key)
            if value is not None:
                rows[(label,)] = value
        return MultiLabeledSample([GPU_LABEL], rows)

    return extract


def _throttled(parsed: list[GpuEntry]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for label, entry in parsed:
        if "throttle_status" not in entry:
            continue
        throttled = entry["throttle_status"] != UNTHROTTLED
        rows[(label,)] = "1.0" if throttled else "0.0"
    return MultiLabeledSample([GPU_LABEL], rows)


def _up(parsed: list[GpuEntry]) -> SampleValue:
    return MultiLabeledSample(
        [GPU_LABEL],
        {(label,): ("1.0" if entry else "0.0") for label, entry in parsed},
    )


def _down() -> list[GpuEntry]:
    """A single down row; only `amdgpu.up` finds something to report."""
    return [("", {})]


FIELDS = {
    "amdgpu.power_usage": "power_usage",
    "amdgpu.hotspot_temperature": "hotspot_temperature",
    "amdgpu.memory_temperature": "memory_temperature",
    "amdgpu.gfx_utilization": "gfx",
    "amdgpu.gfx_clock": "gfx_clock",
    "amdgpu.mem_utilization": "mem",
    "amdgpu.mem_clock": "mem_clock",
    "amdgpu.encoder_utilization": "encoder",
    "amdgpu.encoder_clock": "encoder_clock",
    "amdgpu.decoder_utilization": "decoder",
    "amdgpu.decoder_clock": "decoder_clock",
    "amdgpu.single_bit_ecc": "single_bit_ecc",
    "amdgpu.double_bit_ecc": "double_bit_ecc",
    "amdgpu.pcie_replay": "pcie_replay",
    "amdgpu.vram_used": "vram_used",
    "amdgpu.vram_total": "vram_total",
    "amdgpu.pcie_bandwidth": "pcie_bw",
}

COMMANDS: dict[str, Callable[[list[GpuEntry]], SampleValue]] = {
    "amdgpu.up": _up,
    "amdgpu.throttled": _throttled,
    **{command: _column(field) for command, field in FIELDS.items()},
}


def is_known_command(command: str) -> bool:
    return command in COMMANDS


class AmdGpuMetricHandler(MetricHandler):
    """AMD GPU handler backed by `amd-smi monitor --json`.

    One amd-smi invocation per cycle serves every configured metric;
    its timeout is the max of the requesting metrics' timeouts. Each
    JSON field maps to an `amdgpu.*` command; samples carry the `gpu`
    label. A failed run is tolerated: `sms_amdgpu_up` reports 0.0, the
    other metrics yield no samples and the cycle succeeds. Fields
    reported as `N/A` yield no sample for that GPU.
    """

    required_fields: ClassVar[list[str]] = ["cmd"]

    async def verify(self, metrics: list[Metric]) -> None:
        await super().verify(metrics)
        errors: list[str] = []
        for m in metrics:
            if not is_known_command(m.cmd):
                errors.append(
                    f"metric {m.name!r}: unknown amdgpu command {m.cmd!r}"
                )
                continue
            if m.value_type in exposition.DISTRIBUTION_TYPES:
                errors.append(
                    f"metric {m.name!r}: amdgpu commands require a scalar "
                    f"value_type, got {m.value_type!r}"
                )
        if errors:
            msg = f"Invalid config {self.config_path}:\n" + "\n".join(
                f"  - {e}" for e in errors
            )
            log.error(msg)
            raise ValueError(msg)

    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        parsed = await self._probe(metrics)
        return {m.name: COMMANDS[m.cmd](parsed) for m in metrics}

    async def _probe(self, metrics: list[Metric]) -> list[GpuEntry]:
        """Run amd-smi once; a failed run downgrades to the down row."""
        if not metrics:
            return []
        timeout = max(m.timeout for m in metrics)
        raw = await run_command(self.support.runner, AMD_SMI_ARGV, timeout)
        parsed = parse_amd_smi(raw) if raw is not None else []
        if not parsed:
            log.warning(
                "amdgpu: %r produced no usable GPU data",
                " ".join(AMD_SMI_ARGV),
            )
            return _down()
        return [
            (gpu_label(entry, index), entry)
            for index, entry in enumerate(parsed)
        ]
