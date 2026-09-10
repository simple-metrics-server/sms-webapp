# webapp — Prometheus metrics exporter

A small [Quart](https://quart.palletsprojects.com/)-based exporter that
serves Prometheus metrics at `GET /metrics`. Metrics are collected by
pluggable handlers on a fixed interval; the last good result is served
from a cache, so the endpoint never blocks and never serves partial
output.

## Table of contents

- [Requirements](#requirements)
  - [Hardware](#hardware)
  - [Software](#software)
- [Remarks](#remarks)
- [Installation](#installation)
- [Usage](#usage)
- [Examples](#examples)
- [Further documentation](#further-documentation)
- [Related links](#related-links)

## Requirements

### Hardware

Minimal — any headless box works:

- ~100 MB free RAM (the app idles far below that; a small VPS or
  Raspberry Pi is plenty)
- a few MB of disk for the repo, venv and cache files
- one free TCP port for the endpoint (default 9555), reachable by
  your Prometheus instance

### Software

- Linux — **Debian** is assumed by the package and reboot probes;
  everything else runs anywhere
- Python **>= 3.11**
- [just](https://just.systems/) for the provided workflows
- systemd (optional, only for the boot-time service install)
- root/sudo only needed for the systemd install; the app itself runs
  as a normal user

## Remarks

- **Trusted network assumed** — `/metrics` has no authentication.
  HTTPS is built in (self-signed by default), but there is no
  client auth.
- Self-signed certificates trigger browser/curl warnings; use
  `curl -k` or pass a CA-signed pair via `--cert`/`--key`.
- Two OS probes (`os.packages_upgradable`, `os.reboot_required`) are
  Debian-specific and report `0.0` elsewhere; probe failures never
  crash the app.
- Handler commands run **without a shell** — no pipes, `&&` or
  redirections in `cmd`.
- `data/` holds configs plus runtime artifacts (`metrics.cache`,
  `keys/`); the artifacts are gitignored and regenerated.
- Failed scrape cycles keep serving the last good cache — the app
  always answers.

## Installation

```bash
just install-venv          # create .venv + install package and dev deps
# or, without just:
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

## Usage

```bash
just start                # background on :9555, https (self-signed on first run)
MODE=http just start       # same, over plain http
just stop                  # stop the background instance
just check                 # lint + tests (the gate before any change)

just install-systemd      # install as a boot-enabled systemd service (sudo)
just uninstall-systemd     # stop + disable + remove the service
```

Endpoint: `http(s)://<host>:9555/metrics`. The scrape interval is the
`SCRAPE_INTERVAL` env var (seconds, default 60).

## Examples

Fetch metrics:

```bash
curl -k https://localhost:9555/metrics
# # HELP sms_builtin_cpu_count Number of logical CPUs
# # TYPE sms_builtin_cpu_count gauge
# sms_builtin_cpu_count 32.0
# ...
```

Faster scraping + plain http on another port:

```bash
SCRAPE_INTERVAL=15 MODE=http just start
curl http://localhost:9555/metrics
```

Custom certificate material and SANs, run directly via the CLI:

```bash
.venv/bin/webapp-serve --scheme https \
    --san metrics.example.com --san 10.0.0.5 --days 1825
```

Add your own metric — edit `data/config/bash.json` (live on the next
cycle, no restart):

```json
{
    "name": "sms_bash_uptime_seconds",
    "help_text": "System uptime in seconds",
    "value_type": "gauge",
    "cmd": "cut -d. -f1 /proc/uptime",
    "timeout": 5
}
```

Point the OpenVPN handler at a status file on a non-standard path
(space-separated for several; no config edits needed):

```bash
OPENVPN_STATUS_PATH=/var/lib/openvpn/status/server.log just start
```

See [handlers](docs/handlers.md#openvpn--webapphandlersopenvpny) for
the OpenVPN metric reference.

## Further documentation

Detailed topic READMEs live in [docs/](docs/):

- [Architecture](docs/architecture.md) — how a scrape works, failure
  semantics, module tour
- [Handlers](docs/handlers.md) — writing and configuring handlers
- [Builtin commands](docs/builtin-commands.md) — runtime/OS/hardware
  probes reference
- [CLI](docs/cli.md) — all options, HTTPS preflight, systemd
  install/uninstall

## Related links

The software this app is built and run with:

- [Python](https://www.python.org/) — runtime (>= 3.11)
- [Quart](https://quart.palletsprojects.com/) — async web framework
  serving `/metrics`
- [Hypercorn](https://hypercorn.readthedocs.io/) — ASGI server used by
  Quart
- [click](https://click.palletsprojects.com/) — command-line interface
- [cryptography](https://cryptography.io/) — self-signed certificate
  generation
- [Prometheus](https://prometheus.io/) — exposition format the
  endpoint speaks and the intended scraper
- [just](https://just.systems/) — task runner for all workflows
- [pytest](https://docs.pytest.org/) with
  [pytest-asyncio](https://pytest-asyncio.readthedocs.io/) — test
  suite
- [black](https://black.readthedocs.io/) — code formatter
- [ruff](https://docs.astral.sh/ruff/) — linter
- [systemd](https://systemd.io/) — boot-time service management
  (`--install-systemd`)
