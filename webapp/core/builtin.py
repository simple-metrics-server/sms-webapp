import asyncio
import logging
import os
import platform
import pwd
import shutil
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING

from webapp.common.model import LabeledSample, Metric, SampleValue
from webapp.core.process import ProcessRunner
from webapp.helpers.util import (
    read_text,
    run_command,
    run_command_status,
    to_float,
    to_int,
)

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

# Debian and Red Hat (RHEL/Alma/Rocky/Fedora) families are supported.
DEBIAN_IDS = {"debian", "ubuntu", "linuxmint", "raspbian", "pop"}
RHEL_IDS = {"rhel", "fedora", "centos", "almalinux", "rocky", "ol", "amzn"}
REBOOT_REQUIRED_PATHS = (
    "/run/reboot-required",
    "/var/run/reboot-required",
)
NEEDS_RESTARTING = "needs-restarting"
PACKAGE_MANAGERS = ("apt-get", "dnf", "yum")
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


def _user_sample(
    cmd: str, counts: dict[str, int], uids: dict[str, str]
) -> SampleValue:
    """Derive a `builtin.users.*` value from who output.

    count/sessions aggregate the per-user session counts; by_user maps
    each logged-in user to their uid string.
    """
    if cmd == "builtin.users.count":
        return repr(float(len(counts)))
    if cmd == "builtin.users.sessions":
        return repr(float(sum(counts.values())))
    return LabeledSample("user", {user: uids[user] for user in counts})


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
    a, b, c = (to_float(parts[i]) if i < len(parts) else None for i in range(3))
    return (
        a if a is not None else 0.0,
        b if b is not None else 0.0,
        c if c is not None else 0.0,
    )


def _parse_meminfo(text: str) -> dict[str, int]:
    """{total, available, used} bytes from /proc/meminfo."""
    mem = {"total": 0, "available": 0}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep or key not in ("MemTotal", "MemAvailable"):
            continue
        parts = rest.split()
        value = to_int(parts[0]) if parts else None
        if value is None:
            continue
        mem["total" if key == "MemTotal" else "available"] = value * 1024
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
        received, transmitted = to_int(fields[0]), to_int(fields[8])
        if received is None or transmitted is None:
            continue
        rx += received
        tx += transmitted
    return rx, tx


def _parse_df(text: str) -> dict[str, int]:
    """{total, used, available} bytes over real filesystems."""
    total = used = avail = 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or not parts[0].startswith("/dev/"):
            continue
        total_kb = to_int(parts[1])
        used_kb = to_int(parts[2])
        avail_kb = to_int(parts[3])
        if total_kb is None or used_kb is None or avail_kb is None:
            continue
        total += total_kb * 1024
        used += used_kb * 1024
        avail += avail_kb * 1024
    return {"total": total, "used": used, "available": avail}


# --- collectors ---


async def _safe_run(
    runner: ProcessRunner, argv: list[str], timeout: float
) -> str:
    """Run a command, returning "" and logging on failure."""
    return await run_command(runner, argv, timeout) or ""


async def _read_text(path: str) -> str:
    """Read a file, returning "" and logging on failure."""
    return await asyncio.to_thread(read_text, path) or ""


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


def _user_uids(usernames: list[str]) -> dict[str, str]:
    """Map username -> canonical uid string; 0.0 on lookup failure."""
    uids: dict[str, str] = {}
    for user in usernames:
        try:
            uids[user] = repr(float(pwd.getpwnam(user).pw_uid))
        except KeyError:
            log.warning("builtin: no passwd entry for user %r", user)
            uids[user] = "0.0"
    return uids


async def _count_processes(runner: ProcessRunner, metrics: list[Metric]) -> int:
    """Number of processes from one `ps aux` run."""
    stdout = await _safe_run(runner, ["ps", "aux"], _max_timeout(metrics))
    lines = [line for line in stdout.splitlines() if line.strip()]
    return max(0, len(lines) - 1)


def _distro_family() -> str | None:
    """Return "debian", "rhel" or None based on /etc/os-release."""
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        return None
    ids = {release.get("ID", "").lower()}
    ids.update(release.get("ID_LIKE", "").lower().split())
    if ids & DEBIAN_IDS:
        return "debian"
    if ids & RHEL_IDS:
        return "rhel"
    return None


def _package_manager() -> str | None:
    """First available package manager for this distribution."""
    family = _distro_family() or ""
    candidates = {
        "debian": ("apt-get",),
        "rhel": ("dnf", "yum"),
    }.get(family, PACKAGE_MANAGERS)
    return next((name for name in candidates if shutil.which(name)), None)


def _count_check_update(stdout: str) -> int:
    """Count upgrade entries in `dnf`/`yum check-update` output."""
    count = 0
    obsoleting = False
    for line in stdout.splitlines():
        if not line.strip():
            obsoleting = False
        elif line.startswith("Obsoleting Packages"):
            obsoleting = True
        elif not obsoleting:
            count += 1
    return count


async def _count_upgradable(
    runner: ProcessRunner, metrics: list[Metric]
) -> int:
    """Number of upgradable packages (apt-get or dnf/yum)."""
    manager = _package_manager()
    timeout = _max_timeout(metrics)
    if manager == "apt-get":
        stdout = await _safe_run(runner, ["apt-get", "-s", "upgrade"], timeout)
        return sum(
            1 for line in stdout.splitlines() if line.startswith("Inst ")
        )
    if manager is None:
        return 0
    result = await run_command_status(
        runner, [manager, "-q", "check-update"], timeout
    )
    if result is None:
        return 0
    code, stdout = result
    # dnf/yum exit 100 when updates are available, 0 when there are none.
    return _count_check_update(stdout) if code in (0, 100) else 0


async def _reboot_required(
    runner: ProcessRunner, metrics: list[Metric]
) -> bool:
    """True if the host is flagged for a reboot.

    Debian writes `/run/reboot-required`; on the Red Hat family
    `needs-restarting -r` (dnf-utils) exits 1 when a reboot is due.
    """
    if any(os.path.exists(path) for path in REBOOT_REQUIRED_PATHS):
        return True
    if not shutil.which(NEEDS_RESTARTING):
        return False
    result = await run_command_status(
        runner, [NEEDS_RESTARTING, "-r"], _max_timeout(metrics)
    )
    return result is not None and result[0] == 1


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
        uids: dict[str, str] = {}
        if any(m.cmd == "builtin.users.by_user" for m in user_metrics):
            uids = await asyncio.to_thread(_user_uids, list(counts))
        for m in user_metrics:
            out[m.name] = _user_sample(m.cmd, counts, uids)

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
        needed = await _reboot_required(runner, reboot_metrics)
        value = "1.0" if needed else "0.0"
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
