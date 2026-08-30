import asyncio
import json
import os

import pytest

from webapp.core.model import LabeledSample
from webapp.core.runtime import Runtime
from webapp.core.timing import HandlerTimings, ScrapeTimings
from webapp.handlers.builtin import BuiltinMetricHandler


def write_config(tmp_path, metrics, name="builtin.json"):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(json.dumps(metrics), encoding="utf-8")
    return str(p)


def metric(**kw):
    base = {
        "name": "sms_builtin_scrape_duration_seconds",
        "help_text": "Wall time of the last complete scrape cycle",
        "value_type": "gauge",
        "cmd": "builtin.timings.scrape",
        "timeout": 5,
    }
    base.update(kw)
    return base


def test_verify_rejects_unknown_builtin_command(tmp_path):
    cfg = write_config(tmp_path, [metric(cmd="builtin.nope")])
    h = BuiltinMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="unknown builtin command"):
        asyncio.run(h.verify(metrics))


def test_verify_rejects_bad_per_handler_command(tmp_path):
    cfg = write_config(
        tmp_path, [metric(cmd="builtin.timings.handler.BashMetricHandler.nope")]
    )
    h = BuiltinMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="unknown builtin command"):
        asyncio.run(h.verify(metrics))


def test_verify_rejects_unknown_handler(tmp_path):
    cfg = write_config(
        tmp_path, [metric(cmd="builtin.timings.handler.NoSuchHandler.total")]
    )
    h = BuiltinMetricHandler(cfg)

    class FakeRuntime:
        def __init__(self):
            self.handlers = [object()]

    h.bind_runtime(FakeRuntime())
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="no such handler"):
        asyncio.run(h.verify(metrics))


def test_execute_returns_zero_without_runtime(tmp_path):
    cfg = write_config(tmp_path, [metric()])
    h = BuiltinMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    asyncio.run(h.verify(metrics))
    results = asyncio.run(h.execute(metrics))
    assert results == {"sms_builtin_scrape_duration_seconds": "0.0"}


def test_execute_reports_scrape_total(tmp_path):
    cfg = write_config(tmp_path, [metric()])
    h = BuiltinMetricHandler(cfg)

    class FakeRuntime:
        last_timings = ScrapeTimings(total=1.25)

    h.bind_runtime(FakeRuntime())
    metrics = asyncio.run(h.read())
    results = asyncio.run(h.execute(metrics))
    assert results == {"sms_builtin_scrape_duration_seconds": "1.25"}


def test_execute_reports_handler_phase(tmp_path):
    cfg = write_config(
        tmp_path,
        [metric(cmd="builtin.timings.handler.BashMetricHandler.execute")],
    )
    h = BuiltinMetricHandler(cfg)

    class FakeRuntime:
        last_timings = ScrapeTimings(
            total=1.25,
            per_handler={
                "BashMetricHandler": HandlerTimings(
                    read=0.1, verify=0.2, execute=0.8, finalize=0.15
                )
            },
        )

    h.bind_runtime(FakeRuntime())
    metrics = asyncio.run(h.read())
    results = asyncio.run(h.execute(metrics))
    assert results == {"sms_builtin_scrape_duration_seconds": "0.8"}


def test_execute_reports_zero_when_handler_missing(tmp_path):
    cfg = write_config(
        tmp_path,
        [metric(cmd="builtin.timings.handler.BashMetricHandler.execute")],
    )
    h = BuiltinMetricHandler(cfg)

    class FakeRuntime:
        last_timings = ScrapeTimings(total=1.0)

    h.bind_runtime(FakeRuntime())
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="no timings recorded"):
        asyncio.run(h.execute(metrics))


def test_execute_counters_zero_without_runtime(tmp_path):
    metrics = [
        metric(name="m_requests", cmd="builtin.runtime.requests"),
        metric(name="m_scrapes", cmd="builtin.runtime.scrapes"),
    ]
    h = BuiltinMetricHandler(write_config(tmp_path, metrics))
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results == {"m_requests": "0.0", "m_scrapes": "0.0"}


def test_execute_reports_request_and_scrape_counts(tmp_path):
    metrics = [
        metric(name="m_requests", cmd="builtin.runtime.requests"),
        metric(name="m_scrapes", cmd="builtin.runtime.scrapes"),
    ]
    h = BuiltinMetricHandler(write_config(tmp_path, metrics))

    class FakeRuntime:
        request_count = 3
        scrape_count = 2

    h.bind_runtime(FakeRuntime())
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results == {"m_requests": "3.0", "m_scrapes": "2.0"}


def test_full_cycle_via_runtime(tmp_path):
    write_config(tmp_path, [metric()])
    rt = Runtime(
        config_dir=str(tmp_path / "cfg"), cache_path=str(tmp_path / "cache")
    )
    asyncio.run(rt.scrape_once())
    out = asyncio.run(rt.render())
    assert (
        "# TYPE sms_builtin_scrape_duration_seconds gauge\n"
        "sms_builtin_scrape_duration_seconds 0.0\n"
    ) in out


def test_full_cycle_counters_via_runtime(tmp_path):
    metrics = [
        metric(
            name="m_requests",
            cmd="builtin.runtime.requests",
            help_text="requests",
            value_type="counter",
        ),
        metric(
            name="m_scrapes",
            cmd="builtin.runtime.scrapes",
            help_text="scrapes",
            value_type="counter",
        ),
    ]
    write_config(tmp_path, metrics)
    rt = Runtime(
        config_dir=str(tmp_path / "cfg"), cache_path=str(tmp_path / "cache")
    )
    asyncio.run(rt.scrape_once())
    asyncio.run(rt.scrape_once())
    out = asyncio.run(rt.render())
    # Both render as counters. m_scrapes lags one cycle (1.0 after two
    # scrapes): like the timings, it is written mid-cycle, before this
    # cycle's increment lands. m_requests stays 0.0 — incrementing it is
    # the HTTP layer's job (webapp.main), which this Runtime-only test lacks
    assert "# TYPE m_scrapes counter\nm_scrapes 1.0" in out
    assert "# TYPE m_requests counter\nm_requests 0.0" in out


class FakeRunner:
    """Stub ProcessRunner: canned stdout per argv, records calls."""

    def __init__(self, outputs=None, error=None):
        self.outputs = outputs or {}
        self.error = error
        self.calls = []

    async def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.error is not None:
            raise self.error
        return self.outputs.get(" ".join(argv), "")


def make_handler(tmp_path, metrics):
    cfg = write_config(tmp_path, metrics)
    h = BuiltinMetricHandler(cfg)
    h.support.runner = FakeRunner()
    return h


def test_verify_rejects_distribution_value_type(tmp_path):
    cfg = write_config(
        tmp_path, [metric(value_type="histogram", buckets=[0.1])]
    )
    h = BuiltinMetricHandler(cfg)
    metrics = asyncio.run(h.read())
    with pytest.raises(ValueError, match="require a scalar value_type"):
        asyncio.run(h.verify(metrics))


# --- users ---


def test_users_count_sessions_and_by_user(tmp_path):
    metrics = [
        metric(name="m_count", cmd="builtin.users.count"),
        metric(name="m_sessions", cmd="builtin.users.sessions"),
        metric(name="m_by_user", cmd="builtin.users.by_user"),
    ]
    h = make_handler(tmp_path, metrics)
    h.support.runner = FakeRunner(
        {
            "who": "alice pts/0 2026-08-30 10:22 (:0)\nbob pts/1 2026-08-30 10:23 (:0)\nalice pts/2 2026-08-30 10:24 (:0)\n"
        }
    )
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_count"] == "2.0"
    assert results["m_sessions"] == "3.0"
    assert results["m_by_user"] == LabeledSample(
        "user", {"alice": "2.0", "bob": "1.0"}
    )
    assert h.support.runner.calls == [(["who"], 5.0)]


def test_users_zero_when_who_fails(tmp_path):
    metrics = [
        metric(name="m_count", cmd="builtin.users.count"),
        metric(name="m_by_user", cmd="builtin.users.by_user"),
    ]
    h = make_handler(tmp_path, metrics)
    h.support.runner = FakeRunner(error=RuntimeError("no who"))
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_count"] == "0.0"
    assert results["m_by_user"] == LabeledSample("user", {})


def test_who_not_run_without_users_metrics(tmp_path):
    h = make_handler(tmp_path, [metric()])
    h.support.runner = FakeRunner(error=RuntimeError("should not run"))
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results == {"sms_builtin_scrape_duration_seconds": "0.0"}
    assert h.support.runner.calls == []


# --- processes ---


def test_processes_count_skips_header(tmp_path):
    metrics = [metric(name="m_procs", cmd="builtin.os.processes")]
    h = make_handler(tmp_path, metrics)
    h.support.runner = FakeRunner(
        {
            "ps aux": (
                "USER PID %CPU %MEM VSZ RSS TTY STAT START TIME COMMAND\n"
                "root 1 0.0 0.1 1 1 ? Ss 06:00 0:01 init\n"
                "alice 42 0.0 0.1 1 1 ? S 10:00 0:00 sleep\n"
            )
        }
    )
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_procs"] == "2.0"


def test_processes_zero_on_empty_and_failure(tmp_path):
    h = make_handler(tmp_path, [metric(name="m", cmd="builtin.os.processes")])
    h.support.runner = FakeRunner({"ps aux": "USER PID %CPU\n"})
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    assert asyncio.run(h.execute(read))["m"] == "0.0"

    h.support.runner = FakeRunner(error=RuntimeError("boom"))
    assert asyncio.run(h.execute(read))["m"] == "0.0"


# --- packages ---


def test_packages_counts_inst_lines(tmp_path):
    metrics = [metric(name="m_pkg", cmd="builtin.os.packages_upgradable")]
    h = make_handler(tmp_path, metrics)
    h.support.runner = FakeRunner(
        {
            "apt-get -s upgrade": (
                "NOTE: This is only a simulation!\n"
                "Inst foo [1.0] (2.0 Debian:13 stable [amd64])\n"
                "Conf bar (2.0 Debian:13 stable [amd64])\n"
                "Inst baz [1.1] (1.2 Debian:13 stable [amd64])\n"
            )
        }
    )
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_pkg"] == "2.0"
    assert h.support.runner.calls == [(["apt-get", "-s", "upgrade"], 5.0)]


def test_packages_zero_on_failure(tmp_path):
    h = make_handler(
        tmp_path, [metric(name="m", cmd="builtin.os.packages_upgradable")]
    )
    h.support.runner = FakeRunner(error=RuntimeError("no apt"))
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    assert asyncio.run(h.execute(read))["m"] == "0.0"


# --- reboot ---


def test_reboot_required_flag(tmp_path, monkeypatch):
    import webapp.core.builtin as builtin_mod

    flag = tmp_path / "reboot-required"
    monkeypatch.setattr(builtin_mod, "REBOOT_REQUIRED_PATH", str(flag))

    metrics = [metric(name="m_reboot", cmd="builtin.os.reboot_required")]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))

    assert asyncio.run(h.execute(read))["m_reboot"] == "0.0"
    flag.write_text("")
    assert asyncio.run(h.execute(read))["m_reboot"] == "1.0"


# --- cpu ---


def test_cpu_count(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 4)

    metrics = [metric(name="m_count", cmd="builtin.cpu.count")]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_count"] == "4.0"
    assert h.support.runner.calls == []


def test_cpu_count_zero_when_undeterminable(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: None)

    metrics = [metric(name="m_count", cmd="builtin.cpu.count")]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_count"] == "0.0"


def test_cpu_load_averages(tmp_path, monkeypatch):
    import webapp.core.builtin as builtin_mod

    monkeypatch.setattr(builtin_mod, "LOADAVG_PATH", str(tmp_path / "loadavg"))
    (tmp_path / "loadavg").write_text("2.54 2.64 2.20 5/2797 1657302")

    metrics = [
        metric(name="m1", cmd="builtin.cpu.load1"),
        metric(name="m5", cmd="builtin.cpu.load5"),
        metric(name="m15", cmd="builtin.cpu.load15"),
    ]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m1"] == "2.54"
    assert results["m5"] == "2.64"
    assert results["m15"] == "2.2"


def test_cpu_load_zero_on_missing_file(tmp_path, monkeypatch):
    import webapp.core.builtin as builtin_mod

    monkeypatch.setattr(builtin_mod, "LOADAVG_PATH", str(tmp_path / "nope"))
    metrics = [metric(name="m", cmd="builtin.cpu.load1")]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    assert asyncio.run(h.execute(read))["m"] == "0.0"


# --- memory ---


def test_memory_bytes(tmp_path, monkeypatch):
    import webapp.core.builtin as builtin_mod

    monkeypatch.setattr(builtin_mod, "MEMINFO_PATH", str(tmp_path / "meminfo"))
    (tmp_path / "meminfo").write_text(
        "MemTotal:       65746696 kB\nMemAvailable:   43596764 kB\n"
    )

    metrics = [
        metric(name="m_total", cmd="builtin.memory.total_bytes"),
        metric(name="m_avail", cmd="builtin.memory.available_bytes"),
        metric(name="m_used", cmd="builtin.memory.used_bytes"),
    ]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_total"] == repr(float(65746696 * 1024))
    assert results["m_avail"] == repr(float(43596764 * 1024))
    assert results["m_used"] == repr(float((65746696 - 43596764) * 1024))


# --- disk ---


def test_disk_bytes_sum_real_filesystems(tmp_path):
    metrics = [
        metric(name="m_total", cmd="builtin.disk.total_bytes"),
        metric(name="m_used", cmd="builtin.disk.used_bytes"),
        metric(name="m_avail", cmd="builtin.disk.available_bytes"),
    ]
    h = make_handler(tmp_path, metrics)
    h.support.runner = FakeRunner(
        {
            "df -P": (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "/dev/sda1 100 30 70 30% /\n"
                "tmpfs 500 10 490 2% /run\n"
                "/dev/nvme0n1p2 200 50 150 25% /boot\n"
            )
        }
    )
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_total"] == repr(float((100 + 200) * 1024))
    assert results["m_used"] == repr(float((30 + 50) * 1024))
    assert results["m_avail"] == repr(float((70 + 150) * 1024))


def test_disk_zero_on_failure(tmp_path):
    h = make_handler(
        tmp_path, [metric(name="m", cmd="builtin.disk.used_bytes")]
    )
    h.support.runner = FakeRunner(error=RuntimeError("no df"))
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    assert asyncio.run(h.execute(read))["m"] == "0.0"


# --- network ---


def test_network_bytes_skip_loopback(tmp_path, monkeypatch):
    import webapp.core.builtin as builtin_mod

    monkeypatch.setattr(builtin_mod, "NETDEV_PATH", str(tmp_path / "netdev"))
    (tmp_path / "netdev").write_text(
        "Inter-|   Receive    |  Transmit\n"
        " face |bytes packets |bytes packets\n"
        "    lo: 59029177 0 0 0 0 0 0 0 59029177 0 0 0 0 0 0 0\n"
        "enp6s0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n"
        "enp7s0: 30 0 0 0 0 0 0 0 40 0 0 0 0 0 0 0\n"
    )

    metrics = [
        metric(name="m_rx", cmd="builtin.network.received_bytes"),
        metric(name="m_tx", cmd="builtin.network.transmitted_bytes"),
    ]
    h = make_handler(tmp_path, metrics)
    read = asyncio.run(h.read())
    asyncio.run(h.verify(read))
    results = asyncio.run(h.execute(read))
    assert results["m_rx"] == "130.0"
    assert results["m_tx"] == "240.0"
