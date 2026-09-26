import asyncio
import json

import pytest

from webapp.handlers.amdgpu import (
    AmdGpuMetricHandler,
    field_number,
    gpu_label,
    is_known_command,
    parse_amd_smi,
)

REAL_OUTPUT = """[
    {
        "gpu": 0,
        "power_usage": {
            "value": 25,
            "unit": "W"
        },
        "hotspot_temperature": {
            "value": 49,
            "unit": "C"
        },
        "memory_temperature": {
            "value": 50,
            "unit": "C"
        },
        "gfx": {
            "value": 0,
            "unit": "%"
        },
        "gfx_clock": {
            "value": 0,
            "unit": "MHz"
        },
        "mem": {
            "value": 1,
            "unit": "%"
        },
        "mem_clock": {
            "value": 456,
            "unit": "MHz"
        },
        "encoder": "N/A",
        "encoder_clock": {
            "value": 25,
            "unit": "MHz"
        },
        "decoder": "N/A",
        "decoder_clock": {
            "value": 25,
            "unit": "MHz"
        },
        "throttle_status": "UNTHROTTLED",
        "single_bit_ecc": 0,
        "double_bit_ecc": 0,
        "pcie_replay": 0,
        "vram_used": {
            "value": 922,
            "unit": "MB"
        },
        "vram_total": {
            "value": 15760,
            "unit": "MB"
        },
        "pcie_bw": {
            "value": "N/A",
            "unit": "Mb/s"
        }
    }
]"""

TWO_GPU_OUTPUT = (
    "["
    '{"gpu": 0, "power_usage": {"value": 25, "unit": "W"}, '
    '"throttle_status": "UNTHROTTLED", "gfx": {"value": 0, "unit": "%"}}, '
    '{"gpu": 1, "power_usage": {"value": 49, "unit": "W"}, '
    '"throttle_status": "THROTTLED", "gfx": {"value": 12, "unit": "%"}, '
    '"encoder": {"value": 7, "unit": "%"}}'
    "]"
)


class FakeRunner:
    """Stub ProcessRunner: canned stdout, records calls."""

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = []

    async def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.error is not None:
            raise self.error
        return self.output


def make_config(tmp_path, metrics, name="amdgpu.json"):
    path = tmp_path / name
    path.write_text(json.dumps(metrics), encoding="utf-8")
    return str(path)


def metric(cmd, **kw):
    base = {
        "name": "sms_amdgpu_test",
        "help_text": "A test metric",
        "value_type": "gauge",
        "cmd": cmd,
        "timeout": 5.0,
    }
    base.update(kw)
    return base


def make_handler(tmp_path, metrics, output=REAL_OUTPUT, error=None):
    h = AmdGpuMetricHandler(make_config(tmp_path, metrics))
    h.support.runner = FakeRunner(output=output, error=error)
    return h


def full_cycle(handler):
    metrics = asyncio.run(handler.read())
    asyncio.run(handler.verify(metrics))
    results = asyncio.run(handler.execute(metrics))
    return asyncio.run(handler.finalize(metrics, results))


FULL_METRICS = [
    metric(
        "amdgpu.up",
        name="sms_amdgpu_up",
        help_text="amd-smi reporting status (1 = data parsed, 0 = run failed)",
    ),
    metric(
        "amdgpu.power_usage",
        name="sms_amdgpu_power_usage_watts",
        help_text="GPU power draw in watts",
    ),
    metric(
        "amdgpu.gfx_utilization",
        name="sms_amdgpu_gfx_utilization_percent",
        help_text="Graphics utilization in percent",
    ),
    metric(
        "amdgpu.encoder_utilization",
        name="sms_amdgpu_encoder_utilization_percent",
        help_text="Encoder utilization in percent",
    ),
    metric(
        "amdgpu.throttled",
        name="sms_amdgpu_throttled",
        help_text=(
            "1.0 when the GPU reports a throttle state other than UNTHROTTLED"
        ),
    ),
    metric(
        "amdgpu.single_bit_ecc",
        name="sms_amdgpu_ecc_single_bit_total",
        value_type="counter",
        help_text="Single-bit ECC error count",
    ),
    metric(
        "amdgpu.vram_used",
        name="sms_amdgpu_vram_used_megabytes",
        help_text="VRAM used in MB",
    ),
    metric(
        "amdgpu.pcie_bandwidth",
        name="sms_amdgpu_pcie_bandwidth_megabits_per_second",
        help_text="PCIe bandwidth in Mb/s",
    ),
]

EXPECTED_FULL = (
    "# HELP sms_amdgpu_up amd-smi reporting status "
    "(1 = data parsed, 0 = run failed)\n"
    "# TYPE sms_amdgpu_up gauge\n"
    'sms_amdgpu_up{gpu="0"} 1.0\n'
    "\n"
    "# HELP sms_amdgpu_power_usage_watts GPU power draw in watts\n"
    "# TYPE sms_amdgpu_power_usage_watts gauge\n"
    'sms_amdgpu_power_usage_watts{gpu="0"} 25.0\n'
    "\n"
    "# HELP sms_amdgpu_gfx_utilization_percent "
    "Graphics utilization in percent\n"
    "# TYPE sms_amdgpu_gfx_utilization_percent gauge\n"
    'sms_amdgpu_gfx_utilization_percent{gpu="0"} 0.0\n'
    "\n"
    "# HELP sms_amdgpu_encoder_utilization_percent "
    "Encoder utilization in percent\n"
    "# TYPE sms_amdgpu_encoder_utilization_percent gauge\n"
    "\n"
    "\n"
    "# HELP sms_amdgpu_throttled 1.0 when the GPU reports a throttle "
    "state other than UNTHROTTLED\n"
    "# TYPE sms_amdgpu_throttled gauge\n"
    'sms_amdgpu_throttled{gpu="0"} 0.0\n'
    "\n"
    "# HELP sms_amdgpu_ecc_single_bit_total Single-bit ECC error count\n"
    "# TYPE sms_amdgpu_ecc_single_bit_total counter\n"
    'sms_amdgpu_ecc_single_bit_total{gpu="0"} 0.0\n'
    "\n"
    "# HELP sms_amdgpu_vram_used_megabytes VRAM used in MB\n"
    "# TYPE sms_amdgpu_vram_used_megabytes gauge\n"
    'sms_amdgpu_vram_used_megabytes{gpu="0"} 922.0\n'
    "\n"
    "# HELP sms_amdgpu_pcie_bandwidth_megabits_per_second "
    "PCIe bandwidth in Mb/s\n"
    "# TYPE sms_amdgpu_pcie_bandwidth_megabits_per_second gauge\n"
    "\n"
)

EXPECTED_DOWN = (
    "# HELP sms_amdgpu_up amd-smi reporting status "
    "(1 = data parsed, 0 = run failed)\n"
    "# TYPE sms_amdgpu_up gauge\n"
    'sms_amdgpu_up{gpu=""} 0.0\n'
    "\n"
    "# HELP sms_amdgpu_power_usage_watts GPU power draw in watts\n"
    "# TYPE sms_amdgpu_power_usage_watts gauge\n"
    "\n"
)


def down_handler(tmp_path, error):
    return make_handler(
        tmp_path,
        [
            metric(
                "amdgpu.up",
                name="sms_amdgpu_up",
                help_text=(
                    "amd-smi reporting status (1 = data parsed, 0 = run failed)"
                ),
            ),
            metric(
                "amdgpu.power_usage",
                name="sms_amdgpu_power_usage_watts",
                help_text="GPU power draw in watts",
            ),
        ],
        error=error,
    )


def test_full_cycle_produces_exposition_output(tmp_path):
    out = full_cycle(make_handler(tmp_path, FULL_METRICS))
    assert out == EXPECTED_FULL


def test_multi_gpu_and_throttle_mapping(tmp_path):
    metrics = [
        metric(
            "amdgpu.up",
            name="sms_amdgpu_up",
            help_text="up",
        ),
        metric(
            "amdgpu.power_usage",
            name="sms_amdgpu_power_usage_watts",
            help_text="power",
        ),
        metric(
            "amdgpu.gfx_utilization",
            name="sms_amdgpu_gfx_utilization_percent",
            help_text="gfx",
        ),
        metric(
            "amdgpu.throttled",
            name="sms_amdgpu_throttled",
            help_text="throttled",
        ),
        metric(
            "amdgpu.encoder_utilization",
            name="sms_amdgpu_encoder_utilization_percent",
            help_text="encoder",
        ),
    ]
    out = full_cycle(make_handler(tmp_path, metrics, output=TWO_GPU_OUTPUT))
    assert out == (
        "# HELP sms_amdgpu_up up\n"
        "# TYPE sms_amdgpu_up gauge\n"
        'sms_amdgpu_up{gpu="0"} 1.0\n'
        'sms_amdgpu_up{gpu="1"} 1.0\n'
        "\n"
        "# HELP sms_amdgpu_power_usage_watts power\n"
        "# TYPE sms_amdgpu_power_usage_watts gauge\n"
        'sms_amdgpu_power_usage_watts{gpu="0"} 25.0\n'
        'sms_amdgpu_power_usage_watts{gpu="1"} 49.0\n'
        "\n"
        "# HELP sms_amdgpu_gfx_utilization_percent gfx\n"
        "# TYPE sms_amdgpu_gfx_utilization_percent gauge\n"
        'sms_amdgpu_gfx_utilization_percent{gpu="0"} 0.0\n'
        'sms_amdgpu_gfx_utilization_percent{gpu="1"} 12.0\n'
        "\n"
        "# HELP sms_amdgpu_throttled throttled\n"
        "# TYPE sms_amdgpu_throttled gauge\n"
        'sms_amdgpu_throttled{gpu="0"} 0.0\n'
        'sms_amdgpu_throttled{gpu="1"} 1.0\n'
        "\n"
        "# HELP sms_amdgpu_encoder_utilization_percent encoder\n"
        "# TYPE sms_amdgpu_encoder_utilization_percent gauge\n"
        'sms_amdgpu_encoder_utilization_percent{gpu="1"} 7.0\n'
    )


def test_amd_smi_failure_reports_down(tmp_path):
    out = full_cycle(down_handler(tmp_path, error=RuntimeError("no amd-smi")))
    assert out == EXPECTED_DOWN


def test_unparseable_output_reports_down(tmp_path):
    h = make_handler(tmp_path, FULL_METRICS[:2], output="not json at all")
    assert full_cycle(h) == EXPECTED_DOWN


def test_empty_gpu_list_reports_down(tmp_path):
    h = make_handler(tmp_path, FULL_METRICS[:2], output="[]")
    assert full_cycle(h) == EXPECTED_DOWN


def test_missing_throttle_status_yields_no_sample(tmp_path):
    output = '[{"gpu": 0, "power_usage": {"value": 1, "unit": "W"}}]'
    h = make_handler(
        tmp_path,
        [
            metric(
                "amdgpu.throttled",
                name="sms_amdgpu_throttled",
                help_text=(
                    "1.0 when the GPU reports a throttle state other than "
                    "UNTHROTTLED"
                ),
            ),
        ],
        output=output,
    )
    assert full_cycle(h) == (
        "# HELP sms_amdgpu_throttled 1.0 when the GPU reports a throttle "
        "state other than UNTHROTTLED\n"
        "# TYPE sms_amdgpu_throttled gauge\n"
        "\n"
    )


def test_runs_once_with_max_timeout(tmp_path):
    h = make_handler(
        tmp_path,
        [
            metric("amdgpu.up", name="sms_amdgpu_up", timeout=2.0),
            metric(
                "amdgpu.power_usage",
                name="sms_amdgpu_power_usage_watts",
                timeout=9.0,
            ),
        ],
    )
    metrics = asyncio.run(h.read())
    asyncio.run(h.verify(metrics))
    asyncio.run(h.execute(metrics))
    assert h.support.runner.calls == [(["amd-smi", "monitor", "--json"], 9.0)]


def test_no_metrics_skips_the_run(tmp_path):
    h = make_handler(tmp_path, [])
    assert asyncio.run(h.execute([])) == {}
    assert h.support.runner.calls == []


def test_verify_rejects_unknown_command(tmp_path):
    h = make_handler(tmp_path, [metric("amdgpu.nope")])
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="unknown amdgpu command"):
        asyncio.run(h.verify(metrics))


def test_verify_rejects_distribution_value_type(tmp_path):
    h = make_handler(
        tmp_path,
        [
            metric(
                "amdgpu.power_usage",
                value_type="histogram",
                buckets=[1.0],
            )
        ],
    )
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="scalar"):
        asyncio.run(h.verify(metrics))


def test_finalize_fails_on_missing_results(tmp_path):
    h = make_handler(tmp_path, [metric("amdgpu.up")])
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="No results"):
        asyncio.run(h.finalize(metrics, {}))


def test_is_known_command():
    assert is_known_command("amdgpu.up")
    assert is_known_command("amdgpu.power_usage")
    assert not is_known_command("amdgpu.nope")


def test_field_number_shapes():
    assert field_number({"v": {"value": 5, "unit": "%"}}, "v") == "5.0"
    assert field_number({"v": {"value": "N/A"}}, "v") is None
    assert field_number({"v": 7}, "v") == "7.0"
    assert field_number({"v": "N/A"}, "v") is None
    assert field_number({"v": "3.5"}, "v") == "3.5"
    assert field_number({"v": " 42 "}, "v") == "42.0"
    assert field_number({"v": True}, "v") is None
    assert field_number({"v": None}, "v") is None
    assert field_number({}, "v") is None
    assert field_number({"v": float("nan")}, "v") is None


def test_gpu_label():
    assert gpu_label({"gpu": 1}, 5) == "1"
    assert gpu_label({}, 5) == "5"
    assert gpu_label({"gpu": "x"}, 5) == "5"
    assert gpu_label({"gpu": True}, 5) == "5"


def test_parse_amd_smi():
    assert [e.get("gpu") for e in parse_amd_smi(REAL_OUTPUT)] == [0]
    assert [e.get("gpu") for e in parse_amd_smi(TWO_GPU_OUTPUT)] == [0, 1]
    assert parse_amd_smi("garbage") == []
    assert parse_amd_smi('{"gpu": 0}') == []
    assert parse_amd_smi("[]") == []
    assert parse_amd_smi('[1, {"gpu": 2}]') == [{"gpu": 2}]
