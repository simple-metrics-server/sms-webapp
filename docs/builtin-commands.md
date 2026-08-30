# Builtin commands

The builtin handler (`webapp/handlers/builtin.py`, config
`data/config/builtin.json`) reports the app's own runtime state and
the host's OS/hardware state — **without spawning one process per
metric**. Its `cmd` field names a builtin provider instead of a shell
command.

## Semantics

- **Config**: a JSON array of metric definitions like any other
  handler, but `value_type` must be scalar (`counter`/`gauge`/
  `untyped`) — `histogram`/`summary` are rejected by `verify()`.
- **Once per cycle**: each source (subprocess, file read, runtime
  state) is evaluated at most once per cycle and only if at least one
  metric requests it. A source's timeout is the max of the requesting
  metrics' `timeout` values.
- **Failures never fail the cycle**: a missing command, a timeout or
  a non-zero exit is logged as a warning and the affected metrics
  report `0.0`. Unreadable files (`/proc/...`) behave the same.
  `CancelledError` is not swallowed.
- **One-cycle lag**: builtin metrics are rendered during a cycle,
  before that cycle's own counters/timings are updated — the last
  cycle's state is what you see.
- Configs are re-verified every cycle: an unknown command fails
  verification (and thus the cycle) at the next scrape, not silently.

## Command reference

### Runtime counters

| Command | Metric (shipped config) | Meaning |
|---------|--------------------------|---------|
| `builtin.runtime.requests` | `sms_builtin_runtime_requests` (counter) | HTTP requests received since startup — `Runtime.request_count`, incremented by a `before_request` hook in `main.py` |
| `builtin.runtime.scrapes` | `sms_builtin_runtime_scrapes` (counter) | successfully completed scrape cycles — `Runtime.scrape_count`; failed cycles do not count |

### Timings

| Command | Metric (shipped config) | Meaning |
|---------|--------------------------|---------|
| `builtin.timings.scrape` | `sms_builtin_scrape_duration_seconds` (gauge) | total wall time of the last successful cycle, in seconds |
| `builtin.timings.handler.<HandlerClass>.<phase>` | `sms_builtin_handler_*_seconds` (gauge) | duration of one handler's phase; `<phase>` is `read`, `verify`, `execute`, `finalize` or `total` |

All timings report `0.0` until the first cycle completes. Handler
names are verified against the **active** handler classes (e.g.
`BashMetricHandler`, `BuiltinMetricHandler`) — an unknown handler
name fails verification.

### OS state

| Command | Metric (shipped config) | Meaning |
|---------|--------------------------|---------|
| `builtin.users.count` | `sms_builtin_users_count` | distinct logged-in users (`who`) |
| `builtin.users.sessions` | `sms_builtin_users_sessions` | active login sessions (`who`) |
| `builtin.users.by_user` | `sms_builtin_user_id` | numeric user ID of each logged-in user, labeled: `name{user="alice"} 1000.0` (resolved via `pwd`, `0.0` if unresolvable); renders HELP/TYPE only (no samples) when nobody is logged in |
| `builtin.os.processes` | `sms_builtin_os_processes` | process count (`ps aux`, header skipped) |
| `builtin.os.packages_upgradable` | `sms_builtin_os_packages_upgradable` | upgradable packages (`apt-get -s upgrade`, counts `Inst ` lines) — Debian |
| `builtin.os.reboot_required` | `sms_builtin_os_reboot_required` | `1.0` if `/var/run/reboot-required` exists, else `0.0` (no subprocess) — Debian |

### Hardware

| Command | Metric (shipped config) | Meaning |
|---------|--------------------------|---------|
| `builtin.cpu.count` | `sms_builtin_cpu_count` | logical CPUs (`os.cpu_count()`, no subprocess; `0.0` if undeterminable) |
| `builtin.cpu.load1` / `.load5` / `.load15` | `sms_builtin_cpu_load1/5/15` | load averages (`/proc/loadavg`) |
| `builtin.memory.total_bytes` / `.available_bytes` / `.used_bytes` | `sms_builtin_memory_*_bytes` | physical memory (`/proc/meminfo`, kB × 1024; used = total − available) |
| `builtin.disk.total_bytes` / `.used_bytes` / `.available_bytes` | `sms_builtin_disk_*_bytes` | summed over real filesystems (`df -P`, only `/dev/` entries) |
| `builtin.network.received_bytes` / `.transmitted_bytes` | `sms_builtin_network_*_bytes` | summed over non-loopback interfaces (`/proc/net/dev`) |

File-backed sources (loadavg, meminfo, netdev) are read via
`asyncio.to_thread`; unreadable files report `0.0` like probe
failures.

## Example config

```json
[
    {
        "name": "sms_builtin_cpu_count",
        "help_text": "Number of logical CPUs",
        "value_type": "gauge",
        "cmd": "builtin.cpu.count",
        "timeout": 1
    },
    {
        "name": "sms_builtin_scrape_duration_seconds",
        "help_text": "Wall time of the last complete scrape cycle",
        "value_type": "gauge",
        "cmd": "builtin.timings.scrape",
        "timeout": 1
    }
]
```

Rendered output:

```
# HELP sms_builtin_cpu_count Number of logical CPUs
# TYPE sms_builtin_cpu_count gauge
sms_builtin_cpu_count 32.0

# HELP sms_builtin_scrape_duration_seconds Wall time of the last complete scrape cycle
# TYPE sms_builtin_scrape_duration_seconds gauge
sms_builtin_scrape_duration_seconds 0.04
```

## Customizing

Edit `data/config/builtin.json` (changes go live on the next cycle —
no restart): drop metrics you do not want, rename them, or add
several metrics reading the same command (the source still runs only
once per cycle).
