# AGENTS.md — webapp

## What this is

A small [Quart](https://quart.palletsprojects.com/) web app that exposes
Prometheus metrics at `GET /metrics`. Metric collection is done by
**pluggable handlers** that are discovered dynamically at startup; a
background loop scrapes all handlers on a fixed interval and serves the
last good result from a cache file.

Design goals, in order:

1. **Simple and straightforward** — no heavy abstraction, flat code
2. **Rock solid** — failures are contained, nothing leaks, the app
   keeps serving
3. **Easily extendable** — adding a handler = dropping one file +
   one JSON config

Python >= 3.11. Runtime dependencies: `quart`, `click` (CLI),
`cryptography` (self-signed cert generation).

## Repository layout

```
webapp/
├── AGENTS.md            # this file
├── Justfile             # all dev/build/run tasks (use just, not raw commands)
├── pyproject.toml       # deps, black/ruff config
├── .gitignore
├── data/                # all static + runtime data (never code)
│   ├── config/          # one <name>.json per active handler
│   │   ├── bash.json
│   │   └── builtin.json
│   ├── keys/            # TLS material (runtime artifact, gitignored)
│   └── metrics.cache    # last good scrape result (runtime artifact, gitignored)
├── docs/                # topic READMEs (architecture, handlers, builtin commands, cli)
├── webapp/              # the Python package
│   ├── main.py          # Quart app, /metrics endpoint, run_app() seam
│   ├── cli.py           # click CLI: scheme/cert/key preflight, self-signed cert generation, systemd install
│   ├── core/            # framework code — handler-agnostic
│   │   ├── model.py     #   Metric dataclass
│   │   ├── base.py      #   MetricHandler ABC (the handler interface)
│   │   ├── helpers.py   #   HandlerSupport (verify/exec/render helpers)
│   │   ├── process.py   #   ProcessRunner (safe async subprocess execution)
│   │   ├── loader.py    #   dynamic handler discovery
│   │   ├── timing.py    #   ScrapeTimings/HandlerTimings + measure()
│   │   ├── builtin.py   #   builtin collectors/providers (who/ps/apt/proc)
│   │   └── runtime.py   #   Runtime (scrape loop, cache, orchestration)
│   └── handlers/        # drop-in folder for handlers
│       ├── bash.py      # BashMetricHandler
│       ├── builtin.py   # BuiltinMetricHandler (runtime state, no subprocess)
│       └── openvpn.py   # OpenVpnMetricHandler (parses OpenVPN status files)
└── tests/               # pytest suite, one file per component
```

## Quick start

All workflows go through [just](https://just.systems/). Run `just` to
list targets.

```bash
just install-venv   # create .venv, install package + dev deps
just check     # lint (ruff + black) + tests — the gate before any commit
just start     # run in background on :9555 over https (installs first)
MODE=http just start   # same, but over plain http
just stop      # stop it
just clean     # remove venv, caches, build artifacts, metrics.cache, data/keys
```

Other targets: `prepare` (venv only), `build` (wheel into `dist/`),
`lint`, `test`, `install-systemd` / `uninstall-systemd` (service).

Manual run (without just):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m webapp.main          # foreground, http
.venv/bin/webapp-serve --scheme https    # foreground, https
```

Endpoint: `http(s)://<host>:9555/metrics`. Port/host are CLI options
(`--host`/`--port`); the scrape interval is the env var `SCRAPE_INTERVAL`
(seconds, default 60).

## Running over HTTPS

`--scheme https` triggers a TLS preflight **before** Quart starts:

- Material lives in `data/keys/` (`cert.pem` + `key.pem`, defaults;
  override with `--cert`/`--key`/`--key-dir`). The directory is created
  if missing; it is gitignored and removed by `just clean`.
- Both files present → reused as-is.
- Exactly one present → startup error (ambiguous state).
- Neither present → a self-signed certificate is generated on the fly
  (RSA 2048, SHA-256, CN from `--common-name`, SANs from repeatable
  `--san` — default `localhost, 127.0.0.1, ::1` — valid `--days` days).
  Pass `--no-generate` to fail instead of generating.
- Key files are written with `0600`, certs with `0644`.

The same CLI is available both as `python -m webapp.main` and the
`webapp-serve` console script; `run_app()` in `main.py` is the seam the
CLI (and tests) use to start Quart.

## Installing as a systemd service

```bash
just install-systemd    # install + enable + start (prompts for sudo)
just uninstall-systemd  # stop + disable + remove the unit
```

Both targets delegate to the CLI flags `--install-systemd` /
`--uninstall-systemd` (also usable directly via
`.venv/bin/webapp-serve`), install the venv first if needed, and pass
the current `MODE` as the scheme.

- `--install-systemd` writes `/etc/systemd/system/sms-webapp.service`,
  then `daemon-reload` + `enable` (boot via `WantedBy=multi-user.target`)
  + `restart` the service, then exits — the service serves, not the
  CLI invocation.
- Privileged steps are `sudo`-prefixed when not run as root (run the
  target from a terminal so the password prompt works).
- The unit runs as the installing user, `WorkingDirectory` = the repo
  root, `ExecStart` = the current interpreter with the invocation's
  non-default options. TLS preflight (and key generation) happens on
  service start.
- Re-running install updates the unit and restarts the service. Stop
  a `just start` instance first (`just stop`) or the service cannot
  bind the port. After `just clean` the venv is gone — the next
  `just install-systemd` rebuilds it, but the running service must be
  reinstalled right away.
- `--uninstall-systemd` stops + disables the service, removes the unit
  file and reloads the daemon. Both flags are mutually exclusive.

## Architecture: how a scrape works

1. **Startup** (`main.py` → `before_serving`): the Runtime discovers
   handlers, runs one full scrape (`scrape_once()`); if it fails, the
   app refuses to start. Then the background loop (`serve(interval)`)
   is spawned.
2. **Each cycle**, per handler, fully concurrently:
   `read()` → `verify(metrics)` → `execute(metrics)` → `finalize(metrics, results)`.
   Configs are re-read and re-verified every cycle, so config edits go
   live without restart.
 3. **On success**: output is written atomically to `data/metrics.cache`
    (tmp file + `os.replace`), cycle timings are stored in
    `runtime.last_timings` and `runtime.scrape_count` is incremented.
 4. **On failure**: the exception group is logged; the previous cache
    and timings are kept. The app never serves partial/broken output.
 5. **Serving**: every request increments `runtime.request_count` (via
    a `before_request` hook); `/metrics` just reads the cache file
    (200), or 503 if no successful scrape has happened yet.

A cycle is capped at the scrape interval (`asyncio.wait_for`), so a
stuck handler can never block the next cycle.

## Writing a new handler

Two steps, no other changes:

1. Drop `foo.py` into `webapp/handlers/` defining **exactly one**
   `MetricHandler` subclass. The module name becomes the handler name.
2. Add `data/config/foo.json` (a JSON array of metric definitions).
   Without this file the handler is skipped at startup.

Minimal handler:

```python
from typing import ClassVar
from webapp.core.base import MetricHandler
from webapp.core.model import DistributionSample, Metric

class FooMetricHandler(MetricHandler):
    required_fields: ClassVar[list[str]] = ["cmd"]

    async def execute(
        self, metrics: list[Metric]
    ) -> dict[str, str | DistributionSample]:
        return await self.support.run_cmds(metrics)
```

Only `execute()` is abstract. `read`/`verify`/`finalize` have concrete
defaults in the base class; override them if your config format or
rendering differs. Loader rules: zero handler classes in a module →
skipped with a warning; more than one → startup error.

### Metric config fields (`data/config/<handler>.json`)

| field        | type   | notes                                            |
|--------------|--------|--------------------------------------------------|
| `name`       | str    | must match `[a-zA-Z_:][a-zA-Z0-9_:]*`, unique across ALL handlers, prefixed `sms_<handler>_...` (e.g. `sms_bash_test_gauge`) |
| `help_text`  | str    | rendered in `# HELP`                             |
| `value_type` | str    | one of `counter`, `gauge`, `untyped`, `histogram`, `summary` |
| `cmd`        | str    | command line, parsed with `shlex` (no shell!)    |
| `timeout`    | number | seconds, must be > 0                             |
| `buckets`    | list   | histogram only — ascending finite numbers (upper bounds); `+Inf` implicit |
| `quantiles`  | list   | summary only — ascending numbers in (0, 1)       |

### Histograms and summaries

Scalar metrics print a single numeric line. Histograms and summaries
print one `key value` line per series instead, followed by the two
reserved lines `sum` and `count`:

```
0.1 1
0.5 3
1 3
sum 0.97
count 3
```

Keys are matched numerically against the declared `buckets`/`quantiles`
(`0.10` matches `0.1`). Every declared key, `sum` and `count` must
appear exactly once; unknown, duplicate or missing keys fail the cycle.
Bucket counts must be non-decreasing and `count >=` the last bucket.
Values are canonicalized with `repr(float(v))` like scalars.

Rendering:

- histogram → `name_bucket{le="0.1"} …`, `name_bucket{le="+Inf"} …`
  (from `count`), `name_sum`, `name_count`
- summary → `name{quantile="0.5"} …`, `name_sum`, `name_count`

## Conventions and rules for agents

- **Use `just` targets**, not raw pytest/pip commands. `just check`
  must pass before any change is considered done.
- **Everything is async.** Handlers and the runtime are coroutine-based;
  use `asyncio.gather(..., return_exceptions=True)` for fan-out and
  raise `ExceptionGroup` on collected failures.
- **Handlers are stateless.** Phases communicate via parameters and
  return values (`read() -> list[Metric]`, `execute(metrics) ->
  dict[str, str | DistributionSample]`). Never store per-scrape state on
  `self` — handler instances are shared across cycles.
- **No shell execution.** Commands run via
  `asyncio.create_subprocess_exec(*shlex.split(cmd))` — no shell
  syntax (`&&`, pipes, `;`) works in `cmd`. Process groups are killed
  on timeout/cancellation; keep it that way.
- **Failure semantics**: fail the whole cycle, serve the last good
  cache. Never emit partial exposition output. Collect all config
  errors into one `ValueError` instead of failing on the first.
- **Metric values** — scalar metric output must be a single-line, finite
  number; histograms/summaries emit a `key value` line per series (see
  above). All values are canonicalized via `repr(float(v))` (`1` renders
  as `1.0`). Reject `nan`/`inf`/`1_0`.
- **Exposition format**: `# HELP`/`# TYPE`/sample blocks separated by
  blank lines; escape `\` and newlines in HELP. Content type is
  `text/plain; version=0.0.4; charset=utf-8`.
- **Never leak internals in HTTP responses** — 500/503 bodies are
  static strings; details go to the log.
- **Style**: black (line-length 80) + ruff; both enforced by `just lint`.
  Stdlib-first; think twice before adding dependencies to
  `pyproject.toml`.
- **Tests**: pytest in `tests/`, one file per component; async via
  `asyncio.run()` in sync tests, `pytest-asyncio` only where needed
  (test_main.py). Tests must not depend on the shipped
  `data/config/bash.json` — use `tmp_path` fixtures.
- **`data/` holds data only** (configs, runtime cache). Code lives in
  `webapp/`, tests in `tests/`. Runtime artifacts (`metrics.cache`,
  `webapp.pid`, `webapp.log`) are gitignored and removed by
  `just clean`.

## Builtin commands

`Runtime` holds the shared state the builtin handler reports: a
`ScrapeTimings` from the last successful cycle (`last_timings`:
`total` wall time plus per-handler phase durations
`read`/`verify`/`execute`/`finalize`, keyed by handler class name)
and two monotonic counters. The builtin handler exposes internal and
OS state via commands:

### Timings

- `builtin.timings.scrape` — total wall time (seconds)
- `builtin.timings.handler.<HandlerClass>.<phase>` — per-handler phase
  duration where `<phase>` is `read`, `verify`, `execute`, `finalize` or
  `total`

All timings report `0.0` until the first cycle completes.

### Runtime counters

- `builtin.runtime.requests` — HTTP requests received since startup
  (`Runtime.request_count`, incremented by a `before_request` hook in
  `main.py`)
- `builtin.runtime.scrapes` — successfully completed scrape cycles
  (`Runtime.scrape_count`, incremented where `last_timings` is updated;
  failed cycles do not count)

Both render as Prometheus `counter` metrics.

### OS state

- `builtin.users.count` — distinct logged-in users (`who`)
- `builtin.users.sessions` — active login sessions (`who`)
- `builtin.users.by_user` — numeric user ID of each logged-in user
  as a labeled family (`name{user="alice"} 1000.0`, resolved via
  `pwd`, `0.0` if unresolvable); renders HELP/TYPE only when nobody
  is logged in
- `builtin.os.processes` — number of processes (`ps aux`, header skipped)
- `builtin.os.packages_upgradable` — upgradable packages
  (`apt-get -s upgrade`, counts `Inst ` lines)
- `builtin.os.reboot_required` — `1.0` if `/var/run/reboot-required`
  exists, else `0.0` (no subprocess)

Each OS source runs **once per cycle, only if configured**; its timeout
is the max of the requesting metrics' `timeout` values. OS probe
failures (missing command, timeout, non-zero exit) are logged as a
warning and report `0.0` — they never fail the cycle.
`CancelledError` is not swallowed.

**Debian-specific**: the package and reboot checks assume Debian (apt +
`/var/run/reboot-required`). Platform-agnostic detection is future work.

Builtin commands only produce scalar or labeled values, so `verify()`
rejects `histogram`/`summary` `value_type` for any builtin `cmd`.

### Hardware

- `builtin.cpu.count` — logical CPUs (`os.cpu_count()`, no subprocess;
  `0.0` if undeterminable)
- `builtin.cpu.load1` / `.load5` / `.load15` — load averages
  (`/proc/loadavg`, no subprocess)
- `builtin.memory.total_bytes` / `.available_bytes` / `.used_bytes` —
  physical memory (`/proc/meminfo`, kB × 1024, no subprocess)
- `builtin.disk.total_bytes` / `.used_bytes` / `.available_bytes` —
  summed over real filesystems only (`df -P`, skips non-`/dev/` entries)
- `builtin.network.received_bytes` / `.transmitted_bytes` — summed over
  non-loopback interfaces (`/proc/net/dev`, no subprocess)

File-backed sources (loadavg/meminfo/netdev) are read via
`asyncio.to_thread`; unreadable files report `0.0` like OS probe
failures.

### Labeled values

A handler's `execute()` may also return a `LabeledSample(label_name,
values)`, rendered as one `name{label="v"} value` line per entry (sorted
by label value, label values escaped), or a
`MultiLabeledSample(label_names, rows)` for several label dimensions
(each `rows` key is a label value tuple in `label_names` order). See
`webapp.core.model.SampleValue` for the full result union.

## OpenVPN handler

`OpenVpnMetricHandler` (`webapp/handlers/openvpn.py`) parses OpenVPN
`--status` files — the classic `OpenVPN CLIENT LIST` (version 1),
client stats and server `--status-version 2`/`3` — mirroring the
discontinued kumina/openvpn_exporter (metrics prefixed
`sms_openvpn_`). The format is auto-detected from the first line.
Unlike bash, its config `cmd` holds the
**status file path(s)** as a JSON array (a space-separated string is
also accepted), not a command line; no
subprocess runs. The env var `OPENVPN_STATUS_PATH` (JSON array,
bracketed/comma-separated list, or space-separated) overrides the
paths from the config at scrape time. The `status_path` label appears
only on the dedicated `sms_openvpn_status_path` metric (value `1`/`0`
per file); every other metric uses a `type` label (`client`/`server`)
to distinguish roles. `sms_openvpn_up` tracks parse success while
`sms_openvpn_server_up` / `sms_openvpn_client_up` report the detected
role. A missing, unreadable or malformed status file reports
`sms_openvpn_up` `0.0` for that path and yields no other samples
(warning only; the cycle succeeds). Metric names are a fixed schema —
see
[docs/handlers.md](docs/handlers.md#openvpn--webapphandlersopenvpny).

## Known boundaries (out of scope for now)

- No auth/TLS on `/metrics` — assumed to run in a trusted network.
- Stdout/stderr of commands are fully buffered; configs are trusted.
- OS probes are Debian-specific (to be made platform-agnostic later).
