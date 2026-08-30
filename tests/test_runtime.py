import asyncio
import json

import pytest

from webapp.core.model import DistributionSample, Metric
from webapp.core.runtime import Runtime
from webapp.handlers.bash import BashMetricHandler


def write_config(tmp_path, metrics, name="bash.json"):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(metrics), encoding="utf-8")
    return str(d)


def metric(name, cmd="echo 1", **kw):
    base = {
        "name": name,
        "help_text": f"help for {name}",
        "value_type": "gauge",
        "cmd": cmd,
        "timeout": 5,
    }
    base.update(kw)
    return base


def make_runtime(tmp_path, metrics, name="bash.json"):
    cfg_dir = write_config(tmp_path, metrics, name)
    cache = str(tmp_path / "metrics.cache")
    return Runtime(config_dir=cfg_dir, cache_path=cache)


def test_handler_activated_by_matching_config(tmp_path):
    rt = make_runtime(tmp_path, [metric("m_a")])
    assert len(rt.handlers) == 1


def test_handler_without_config_is_skipped(tmp_path):
    rt = Runtime(
        config_dir=str(tmp_path), cache_path=str(tmp_path / "metrics.cache")
    )
    assert rt.handlers == []


def test_scrape_once_writes_cache_file(tmp_path):
    rt = make_runtime(tmp_path, [metric("m_a"), metric("m_b", cmd="echo 2")])
    asyncio.run(rt.scrape_once())
    out = asyncio.run(rt.render())
    assert "m_a 1.0" in out
    assert "m_b 2.0" in out


def test_render_raises_without_cache_file(tmp_path):
    rt = make_runtime(tmp_path, [metric("m_a")])
    with pytest.raises(FileNotFoundError):
        asyncio.run(rt.render())


def test_failed_scrape_keeps_previous_cache(tmp_path):
    cfg_dir = write_config(tmp_path, [metric("m_a")])
    cache = str(tmp_path / "metrics.cache")
    rt = Runtime(config_dir=cfg_dir, cache_path=cache)
    asyncio.run(rt.scrape_once())
    good = asyncio.run(rt.render())

    # break the config, next cycle fails, cache keeps last good content
    write_config(tmp_path, [metric("m_a", cmd="false")])
    with pytest.raises(ExceptionGroup):
        asyncio.run(rt.scrape_once())
    assert asyncio.run(rt.render()) == good


def test_scrape_picks_up_config_changes(tmp_path):
    cfg_dir = write_config(tmp_path, [metric("m_a")])
    cache = str(tmp_path / "metrics.cache")
    rt = Runtime(config_dir=cfg_dir, cache_path=cache)
    asyncio.run(rt.scrape_once())
    assert "m_a 1.0" in asyncio.run(rt.render())

    write_config(tmp_path, [metric("m_new", cmd="echo 9")])
    asyncio.run(rt.scrape_once())
    out = asyncio.run(rt.render())
    assert "m_new 9.0" in out
    assert "m_a" not in out


def test_scrape_fails_on_invalid_config(tmp_path):
    rt = make_runtime(tmp_path, [metric("m_bad", value_type="nope")])
    with pytest.raises(ExceptionGroup):
        asyncio.run(rt.scrape_once())


def test_scrape_rejects_duplicate_names_across_handlers(tmp_path):
    cfg_dir = write_config(tmp_path, [metric("same_name")], name="bash.json")
    cache = str(tmp_path / "metrics.cache")
    rt = Runtime(config_dir=cfg_dir, cache_path=cache)
    # simulate a second handler exporting the same metric name
    other = type(rt.handlers[0])(str(tmp_path / "cfg" / "bash.json"))
    rt.handlers.append(other)
    with pytest.raises(ValueError, match="Duplicate metric names"):
        asyncio.run(rt.scrape_once())


def test_scrape_rejects_series_collision_across_handlers(tmp_path):
    cfg_dir = write_config(
        tmp_path,
        [
            metric(
                "foo",
                value_type="histogram",
                buckets=[0.1, 0.5],
                cmd="echo 1",
            )
        ],
        name="bash.json",
    )
    cache = str(tmp_path / "metrics.cache")
    rt = Runtime(config_dir=cfg_dir, cache_path=cache)

    class Other(BashMetricHandler):
        async def read(self):
            return [
                Metric(
                    name="foo_count",
                    help_text="h",
                    value_type="gauge",
                    cmd="echo 1",
                    timeout=5,
                )
            ]

        async def execute(
            self, metrics: list[Metric]
        ) -> dict[str, str | DistributionSample]:
            return {"foo_count": "1.0"}

    rt.handlers.append(Other(str(tmp_path / "cfg" / "bash.json")))
    with pytest.raises(ValueError, match="Duplicate metric names"):
        asyncio.run(rt.scrape_once())


# --- timings ---


def test_scrape_returns_timings(tmp_path):
    rt = make_runtime(tmp_path, [metric("m_a")])
    timings = asyncio.run(rt.scrape_once())
    assert timings.total > 0
    assert "BashMetricHandler" in timings.per_handler
    ht = timings.per_handler["BashMetricHandler"]
    for phase in (ht.read, ht.verify, ht.execute, ht.finalize):
        assert phase >= 0
    assert ht.total <= timings.total
    assert rt.last_timings is timings


def test_scrape_measures_execute_duration(tmp_path):
    # helper script that sleeps, then prints a value
    script = tmp_path / "slow.sh"
    script.write_text("#!/bin/sh\nsleep 0.3\necho 1\n")
    script.chmod(0o755)
    rt = make_runtime(tmp_path, [metric("m_slow", cmd=str(script), timeout=5)])
    timings = asyncio.run(rt.scrape_once())
    ht = timings.per_handler["BashMetricHandler"]
    assert ht.execute >= 0.3


def test_failed_scrape_keeps_previous_timings(tmp_path):
    cfg_dir = write_config(tmp_path, [metric("m_a")])
    cache = str(tmp_path / "metrics.cache")
    rt = Runtime(config_dir=cfg_dir, cache_path=cache)
    good_timings = asyncio.run(rt.scrape_once())

    write_config(tmp_path, [metric("m_a", cmd="false")])
    with pytest.raises(ExceptionGroup):
        asyncio.run(rt.scrape_once())
    assert rt.last_timings is good_timings
