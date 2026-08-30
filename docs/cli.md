# CLI reference

The CLI is a [click](https://click.palletsprojects.com/) command
(`webapp/cli.py`). It is available in two equivalent forms:

```bash
.venv/bin/webapp-serve [options]        # console script
.venv/bin/python -m webapp.main [options]  # module form
```

Both run the same code. The CLI performs an optional TLS preflight,
optionally manages a systemd service, and finally starts Quart via
`run_app()` in `webapp/main.py` — the seam tests use to avoid binding
sockets. The reloader is always disabled (no watchdog forks).

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--host` | `0.0.0.0` | interface to bind |
| `--port` | `9555` | port to bind |
| `--scheme` | `http` | `http` or `https` |
| `--key-dir` | `data/keys` | directory for cert/key material (created if missing) |
| `--cert` | `<key-dir>/cert.pem` | certificate path |
| `--key` | `<key-dir>/key.pem` | private key path |
| `--no-generate` | off | fail instead of generating missing cert/key |
| `--common-name` | `localhost` | subject CN when generating |
| `--san` | `localhost, 127.0.0.1, ::1` | subjectAltName entry when generating (repeatable; DNS vs IP is inferred) |
| `--days` | `365` | certificate validity in days when generating |
| `--install-systemd` | off | install as a boot-enabled systemd service, then exit |
| `--uninstall-systemd` | off | remove the systemd service, then exit |

Environment: `SCRAPE_INTERVAL` — seconds between scrape cycles
(default 60).

Exit codes follow click conventions: `0` success, `1` runtime error
(preflight, systemctl, generation refused), `2` usage error (bad
flags, mutually exclusive options).

## HTTPS preflight

`--scheme https` runs **before** Quart starts:

| cert exists | key exists | Result |
|-------------|------------|--------|
| yes | yes | reused as-is |
| yes | no | startup error (ambiguous state) |
| no | yes | startup error (ambiguous state) |
| no | no | self-signed certificate generated on the fly — unless `--no-generate`, which errors instead |

Generated certificates: RSA 2048, SHA-256 signature, subject/issuer
CN from `--common-name`, SANs from `--san`, validity `--days` days
(backdated one hour to avoid clock skew), `BasicConstraints ca=False`.
The key is written as unencrypted PKCS#8 PEM with mode `0600`, the
certificate with `0644`. A warning is logged that the certificate is
self-signed.

Material lives in `data/keys/` by default — gitignored and removed by
`just clean`. Point `--cert`/`--key` at a CA-signed pair to use real
TLS.

### Examples

```bash
# https with a self-signed cert on first start
.venv/bin/webapp-serve --scheme https

# https with explicit SANs for other hostnames, 5-year validity
.venv/bin/webapp-serve --scheme https \
    --san metrics.example.com --san 10.0.0.5 --days 1825

# https, fail loudly if material is missing (production)
.venv/bin/webapp-serve --scheme https --no-generate \
    --cert /etc/ssl/certs/webapp.pem --key /etc/ssl/private/webapp.pem

# http on a custom port with a faster scrape interval
SCRAPE_INTERVAL=15 .venv/bin/webapp-serve --scheme http --port 9600
```

## systemd service management

`--install-systemd` / `--uninstall-systemd` are mutually exclusive
and make the CLI exit after managing the service (the service serves,
not the CLI invocation).

### Install

1. Renders `/etc/systemd/system/sms-webapp.service` from the current
   invocation: runs as the installing user, `WorkingDirectory` is the
   repo root, `ExecStart` is the current interpreter
   (`sys.executable`) followed by `--host`/`--port`/`--scheme` plus
   every non-default TLS option — so the install is a snapshot of the
   options you passed.
2. `sudo install -m 0644` the unit (privileged steps are
   `sudo`-prefixed when not run as root — run from a terminal so the
   password prompt works).
3. `systemctl daemon-reload`, `systemctl enable` (boot via
   `WantedBy=multi-user.target`) and `systemctl restart`.

Unit shape:

```ini
[Unit]
Description=webapp - Prometheus metrics exporter
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=alice
Group=alice
WorkingDirectory=/home/alice/scratch/sms/webapp
ExecStart=/home/alice/scratch/sms/webapp/.venv/bin/python -m webapp.main --host 0.0.0.0 --port 9555 --scheme https
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Notes:

- Re-running install updates the unit and restarts the service.
- TLS preflight (and first-run key generation) happens on service
  start, in the service's `WorkingDirectory`.
- Stop a `just start` instance first (`just stop`) — the CLI warns
  when `webapp.pid` exists — or the service cannot bind the port.
- After `just clean` the venv (and thus `ExecStart`'s interpreter) is
  gone; reinstall right away.

### Uninstall

`systemctl disable --now` (stop + disable), remove the unit file,
`daemon-reload`. Errors with a clear message if the unit is not
installed.

### Examples

```bash
# the just way (see README)
just install-systemd
just uninstall-systemd

# direct CLI, custom scheme
.venv/bin/webapp-serve --scheme https --install-systemd
```

Logs: `journalctl -u sms-webapp -f`.
