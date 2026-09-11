import asyncio
import json

import pytest

from webapp.handlers.bash import BashMetricHandler
from webapp.helpers import HandlerSupport


def make_config(tmp_path, metrics, name="cfg.json"):
    p = tmp_path / name
    p.write_text(json.dumps(metrics), encoding="utf-8")
    return str(p)


def metric(**kw):
    base = {
        "name": "test_metric",
        "help_text": "A test metric",
        "value_type": "gauge",
        "cmd": "echo 42",
        "timeout": 5,
    }
    base.update(kw)
    return base


async def full_cycle(handler):
    """Run all four phases like the runtime does."""
    metrics = await handler.read()
    await handler.verify(metrics)
    results = await handler.execute(metrics)
    return await handler.finalize(metrics, results)


def test_full_cycle_produces_exposition_output(tmp_path):
    cfg = make_config(
        tmp_path,
        [
            metric(
                name="sms_bash_test_counter",
                help_text="Test counter",
                value_type="counter",
                cmd="echo 1",
            ),
            metric(
                name="sms_bash_test_gauge",
                help_text="Test gauge",
                cmd="echo 3.5",
            ),
        ],
    )
    out = asyncio.run(full_cycle(BashMetricHandler(cfg)))
    assert out == (
        "# HELP sms_bash_test_counter Test counter\n"
        "# TYPE sms_bash_test_counter counter\n"
        "sms_bash_test_counter 1.0\n"
        "\n"
        "# HELP sms_bash_test_gauge Test gauge\n"
        "# TYPE sms_bash_test_gauge gauge\n"
        "sms_bash_test_gauge 3.5\n"
    )


def test_full_cycle_histogram_and_summary(tmp_path):
    cfg = make_config(
        tmp_path,
        [
            metric(
                name="sms_bash_test_histogram",
                help_text="Test histogram",
                value_type="histogram",
                buckets=[0.1, 0.5, 1.0],
                cmd="printf '0.1 1\\n0.5 2\\n1 2\\nsum 0.9\\ncount 2\\n'",
            ),
            metric(
                name="sms_bash_test_summary",
                help_text="Test summary",
                value_type="summary",
                quantiles=[0.5, 0.99],
                cmd="printf '0.5 0.25\\n0.99 0.9\\nsum 1.2\\ncount 3\\n'",
            ),
        ],
    )
    out = asyncio.run(full_cycle(BashMetricHandler(cfg)))
    assert out == (
        "# HELP sms_bash_test_histogram Test histogram\n"
        "# TYPE sms_bash_test_histogram histogram\n"
        'sms_bash_test_histogram_bucket{le="0.1"} 1.0\n'
        'sms_bash_test_histogram_bucket{le="0.5"} 2.0\n'
        'sms_bash_test_histogram_bucket{le="1.0"} 2.0\n'
        'sms_bash_test_histogram_bucket{le="+Inf"} 2.0\n'
        "sms_bash_test_histogram_sum 0.9\n"
        "sms_bash_test_histogram_count 2.0\n"
        "\n"
        "# HELP sms_bash_test_summary Test summary\n"
        "# TYPE sms_bash_test_summary summary\n"
        'sms_bash_test_summary{quantile="0.5"} 0.25\n'
        'sms_bash_test_summary{quantile="0.99"} 0.9\n'
        "sms_bash_test_summary_sum 1.2\n"
        "sms_bash_test_summary_count 3.0\n"
    )


def test_execute_fails_on_command_failure(tmp_path):
    cfg = make_config(tmp_path, [metric(cmd="false")])
    h = BashMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    asyncio.run(h.verify(metrics))
    with pytest.raises(ExceptionGroup):
        asyncio.run(h.execute(metrics))


def test_verify_fails_on_invalid_config(tmp_path):
    cfg = make_config(tmp_path, [metric(value_type="nope")])
    h = BashMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="invalid value_type"):
        asyncio.run(h.verify(metrics))


def test_read_missing_config_file():
    with pytest.raises(FileNotFoundError):
        asyncio.run(BashMetricHandler("/nonexistent/cfg.json").read())


def test_read_config_not_an_array(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(TypeError, match="JSON array"):
        asyncio.run(BashMetricHandler(str(p)).read())


def test_read_config_unknown_keys_rejected(tmp_path):
    cfg = make_config(tmp_path, [metric(bogus="x")])
    with pytest.raises(ValueError, match="Invalid metric definition"):
        asyncio.run(BashMetricHandler(cfg).read())


def test_finalize_fails_on_missing_results(tmp_path):
    cfg = make_config(tmp_path, [metric(name="m_a"), metric(name="m_b")])
    h = BashMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="No results"):
        asyncio.run(h.finalize(metrics, {"m_a": "1.0"}))


def test_defaults_are_composed(tmp_path):
    h = BashMetricHandler(str(tmp_path / "cfg.json"))
    assert isinstance(h.support, HandlerSupport)
    assert h.required_fields == ["cmd"]
