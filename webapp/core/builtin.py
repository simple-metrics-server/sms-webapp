import asyncio
import logging
import os
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from webapp.core.model import LabeledSample, Metric, SampleValue
from webapp.core.process import ProcessRunner

if TYPE_CHECKING:
    from webapp.core.runtime import Runtime

log = logging.getLogger(__name__)

Provider = Callable[["Runtime | None"], str]

PHASES = ("read", "verify", "execute", "finalize", "total")
_HANDLER_PREFIX = "builtin.timings.handler."

REQUESTS_COMMAND = "builtin.runtime.requests"
SCRAPES_COMMAND = "builtin.runtime.scrapes"

USER_COMMANDS = {
    "builtin.users.count",
    "builtin.users.sessions",
    "builtin.users.by_user",
}

PROCESS_COMMAND = "builtin.os.processes"
PACKAGES_COMMAND = "builtin.os.packages_upgradable"
REBOOT_COMMAND = "builtin.os.reboot_required"

CPU_COMMANDS = {
    "builtin.cpu.load1",
    "builtin.cpu.load5",
    "builtin.cpu.load15",
}
CPU_COUNT_COMMAND = "builtin.cpu.count"
MEMORY_COMMANDS = {
    "builtin.memory.total_bytes",
    "builtin.memory.available_bytes",
    "builtin.memory.used_bytes",
}
DISK_COMMANDS = {
    "builtin.disk.total_bytes",
    "builtin.disk.used_bytes",
    "builtin.disk.available_bytes",
}
NETWORK_COMMANDS = {
    "builtin.network.received_bytes",
    "builtin.network.transmitted_bytes",
}

# Debian-specific until the handler becomes platform-agnostic.
REBOOT_REQUIRED_PATH = "/var/run/reboot-required"
LOADAVG_PATH = "/proc/loadavg"
MEMINFO_PATH = "/proc/meminfo"
NETDEV_PATH = "/proc/net/dev"


# --- runtime state providers ---


def _last_timings(runtime: "Runtime | None"):
    timings = getattr(runtime, "last_timings", None)
    if timings is None:
        return None
    return timings


def _scrape_total(runtime: "Runtime | None") -> str:
    """Last scrape cycle wall time in seconds."""
    timings = _last_timings(runtime)
    if timings is None:
        return "0.0"
    return repr(float(timings.total))


def _handler_phase(runtime: "Runtime | None", handler: str, phase: str) -> str:
    """Duration of one handler phase in the last cycle, in seconds."""
    timings = _last_timings(runtime)
    if timings is None:
        return "0.0"
    ht = timings.per_handler.get(handler)
    if ht is None:
        raise ValueError(f"no timings recorded for handler {handler!r}")
    return repr(float(getattr(ht, phase)))


def _requests_count(runtime: "Runtime | None") -> str:
    """HTTP requests received since startup."""
    if runtime is None:
        return "0.0"
    return repr(float(runtime.request_count))


def _scrapes_count(runtime: "Runtime | None") -> str:
    """Scrape cycles completed successfully since startup."""
    if runtime is None:
        return "0.0"
    return repr(float(runtime.scrape_count))


def parse_handler_cmd(cmd: str) -> tuple[str, str] | None:
    """Return (handler_name, phase) for a per-handler timing cmd."""
    if not cmd.startswith(_HANDLER_PREFIX):
        return None
    rest = cmd[len(_HANDLER_PREFIX) :]
    name, sep, phase = rest.rpartition(".")
    if not sep or not name or phase not in PHASES:
        return None
    return name, phase


def _resolve_runtime(cmd: str) -> Provider | None:
    """Map a runtime-state command string to its provider, or None.

    Recognized commands: `builtin.timings.scrape` (total wall time),
    `builtin.timings.handler.<HandlerClass>.<phase>` where phase is one
    of read/verify/execute/finalize/total, `builtin.runtime.requests`
    and `builtin.runtime.scrapes`.
    """
    if cmd == "builtin.timings.scrape":
        return _scrape_total
    if cmd == REQUESTS_COMMAND:
        return _requests_count
    if cmd == SCRAPES_COMMAND:
        return _scrapes_count
    parsed = parse_handler_cmd(cmd)
    if parsed is not None:
        handler, phase = parsed
        return partial(_handler_phase, handler=handler, phase=phase)
    return None


def is_known_command(cmd: str) -> bool:
    """Whether cmd names any builtin provider."""
    return (
        _resolve_runtime(cmd) is not None
        or cmd in USER_COMMANDS
        or cmd
        in (
            PROCESS_COMMAND,
            PACKAGES_COMMAND,
            REBOOT_COMMAND,
            CPU_COUNT_COMMAND,
        )
        or cmd in CPU_COMMANDS
        or cmd in MEMORY_COMMANDS
        or cmd in DISK_COMMANDS
        or cmd in NETWORK_COMMANDS
    )


# --- sample builders ---


def _user_sample(cmd: str, counts: dict[str, int]) -> SampleValue:
    """Derive a `builtin.users.*` value from who output."""
    if cmd == "builtin.users.count":
        return repr(float(len(counts)))
    if cmd == "builtin.users.sessions":
        return repr(float(sum(counts.values())))
    return LabeledSample(
        "user", {user: repr(float(n)) for user, n in counts.items()}
    )


def _cpu_sample(cmd: str, loads: tuple[float, float, float]) -> str:
    index = {
        "builtin.cpu.load1": 0,
        "builtin.cpu.load5": 1,
        "builtin.cpu.load15": 2,
    }[cmd]
    return repr(float(loads[index]))


def _memory_sample(cmd: str, mem: dict[str, int]) -> str:
    key = cmd[len("builtin.memory.") : -len("_bytes")]
    return repr(float(mem[key]))


def _disk_sample(cmd: str, totals: dict[str, int]) -> str:
    key = cmd[len("builtin.disk.") : -len("_bytes")]
    return repr(float(totals[key]))


def _network_sample(cmd: str, totals: tuple[int, int]) -> str:
    rx, tx = totals
    value = rx if cmd == "builtin.network.received_bytes" else tx
    return repr(float(value))


# --- parsers ---


def _parse_loadavg(text: str) -> tuple[float, float, float]:
    """(1, 5, 15)-minute load averages from /proc/loadavg."""
    parts = text.split()
    loads = [0.0, 0.0, 0.0]
    for i in range(3):
        if i < len(parts):
            try:
                loads[i] = float(parts[i])
            except ValueError:
                loads[i] = 0.0
    return (loads[0], loads[1], loads[2])


def _parse_meminfo(text: str) -> dict[str, int]:
    """{total, available, used} bytes from /proc/meminfo."""
    mem = {"total": 0, "available": 0}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts or key not in ("MemTotal", "MemAvailable"):
            continue
        try:
            value = int(parts[0]) * 1024
        except ValueError:
            continue
        mem["total" if key == "MemTotal" else "available"] = value
    mem["used"] = mem["total"] - mem["available"]
    return mem


def _parse_netdev(text: str) -> tuple[int, int]:
    """(received, transmitted) bytes over non-loopback ifaces."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, sep, rest = line.partition(":")
        if not sep or name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            rx += int(fields[0])
            tx += int(fields[8])
        except ValueError:
            continue
    return rx, tx


def _parse_df(text: str) -> dict[str, int]:
    """{total, used, available} bytes over real filesystems."""
    total = used = avail = 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or not parts[0].startswith("/dev/"):
            continue
        try:
            total += int(parts[1]) * 1024
            used += int(parts[2]) * 1024
            avail += int(parts[3]) * 1024
        except ValueError:
            continue
    return {"total": total, "used": used, "available": avail}


# --- collectors ---


async def _safe_run(
    runner: ProcessRunner, argv: list[str], timeout: float
) -> str:
    """Run a command, returning "" and logging on failure."""
    try:
        return await runner.run(argv, timeout)
    except (RuntimeError, ValueError) as e:
        log.warning("builtin command %r failed: %s", argv, e)
        return ""


async def _read_text(path: str) -> str:
    """Read a file, returning "" and logging on failure."""
    try:
        return await asyncio.to_thread(_load_text, path)
    except OSError as e:
        log.warning("builtin read %r failed: %s", path, e)
        return ""


def _load_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _max_timeout(metrics: list[Metric]) -> float:
    return max(m.timeout for m in metrics)


async def _collect_who(
    runner: ProcessRunner, metrics: list[Metric]
) -> dict[str, int]:
    """Map username -> session count from one `who` run."""
    stdout = await _safe_run(runner, ["who"], _max_timeout(metrics))
    counts: dict[str, int] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if parts:
            user = parts[0]
            counts[user] = counts.get(user, 0) + 1
    return counts


async def _count_processes(runner: ProcessRunner, metrics: list[Metric]) -> int:
    """Number of processes from one `ps aux` run."""
    stdout = await _safe_run(runner, ["ps", "aux"], _max_timeout(metrics))
    lines = [line for line in stdout.splitlines() if line.strip()]
    return max(0, len(lines) - 1)


async def _count_upgradable(
    runner: ProcessRunner, metrics: list[Metric]
) -> int:
    """Number of upgradable packages from `apt-get -s upgrade`."""
    stdout = await _safe_run(
        runner, ["apt-get", "-s", "upgrade"], _max_timeout(metrics)
    )
    return sum(1 for line in stdout.splitlines() if line.startswith("Inst "))


async def collect(
    metrics: list[Metric],
    runner: ProcessRunner,
    runtime: "Runtime | None",
) -> dict[str, SampleValue]:
    """Execute every configured builtin command once and return samples.

    This is the single codepath all builtin collectors flow through:
    runtime-state providers read the bound Runtime; each OS source runs
    at most once per cycle, only if configured. OS probe failures
    report `0.0` (a warning is logged) rather than failing the cycle.
    """
    out: dict[str, SampleValue] = {}

    for m in metrics:
        provider = _resolve_runtime(m.cmd)
        if provider is not None:
            out[m.name] = provider(runtime)

    user_metrics = [m for m in metrics if m.cmd in USER_COMMANDS]
    if user_metrics:
        counts = await _collect_who(runner, user_metrics)
        for m in user_metrics:
            out[m.name] = _user_sample(m.cmd, counts)

    process_metrics = [m for m in metrics if m.cmd == PROCESS_COMMAND]
    if process_metrics:
        count = await _count_processes(runner, process_metrics)
        for m in process_metrics:
            out[m.name] = repr(float(count))

    package_metrics = [m for m in metrics if m.cmd == PACKAGES_COMMAND]
    if package_metrics:
        count = await _count_upgradable(runner, package_metrics)
        for m in package_metrics:
            out[m.name] = repr(float(count))

    reboot_metrics = [m for m in metrics if m.cmd == REBOOT_COMMAND]
    if reboot_metrics:
        value = "1.0" if os.path.exists(REBOOT_REQUIRED_PATH) else "0.0"
        for m in reboot_metrics:
            out[m.name] = value

    cpu_count_metrics = [m for m in metrics if m.cmd == CPU_COUNT_COMMAND]
    for m in cpu_count_metrics:
        out[m.name] = repr(float(os.cpu_count() or 0))

    cpu_metrics = [m for m in metrics if m.cmd in CPU_COMMANDS]
    if cpu_metrics:
        loads = _parse_loadavg(await _read_text(LOADAVG_PATH))
        for m in cpu_metrics:
            out[m.name] = _cpu_sample(m.cmd, loads)

    memory_metrics = [m for m in metrics if m.cmd in MEMORY_COMMANDS]
    if memory_metrics:
        mem = _parse_meminfo(await _read_text(MEMINFO_PATH))
        for m in memory_metrics:
            out[m.name] = _memory_sample(m.cmd, mem)

    disk_metrics = [m for m in metrics if m.cmd in DISK_COMMANDS]
    if disk_metrics:
        stdout = await _safe_run(
            runner, ["df", "-P"], _max_timeout(disk_metrics)
        )
        totals = _parse_df(stdout)
        for m in disk_metrics:
            out[m.name] = _disk_sample(m.cmd, totals)

    network_metrics = [m for m in metrics if m.cmd in NETWORK_COMMANDS]
    if network_metrics:
        totals = _parse_netdev(await _read_text(NETDEV_PATH))
        for m in network_metrics:
            out[m.name] = _network_sample(m.cmd, totals)

    return out
