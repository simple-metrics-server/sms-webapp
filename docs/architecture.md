# Architecture

A small [Quart](https://quart.palletsprojects.com/) web app that
exposes Prometheus metrics at `GET /metrics`. Metric collection is done
by **pluggable handlers** discovered dynamically at startup; a
background loop scrapes all handlers on a fixed interval and serves
the last good result from a cache file.

Design goals, in order:

1. **Simple and straightforward** — no heavy abstraction, flat code
2. **Rock solid** — failures are contained, nothing leaks, the app
   keeps serving
3. **Easily extendable** — adding a handler = dropping one file +
   one JSON config

## Bird's-eye view

```
                HTTP GET /metrics
                       │
                       ▼
  main.py (Quart app)  ── reads ──▶ data/metrics.cache ──▶ 200
      │                     │            (missing)      ──▶ 503
      │ before_request      │
      │  request_count += 1 │
      │ before_serving:     │ first scrape must succeed, then
      ▼                     │ spawn background loop
  Runtime (core/runtime.py) │
      │  every SCRAPE_INTERVAL seconds, cycle capped at interval:
      │
      │   read()      per handler, concurrent: config JSON → Metrics
      │   verify()    concurrent: validate, ALL errors collected
      │   ── cross-handler duplicate series check ──
      │   execute()   concurrent: collect values
      │   finalize()  concurrent: render exposition blocks
      │
      ▼  all succeeded ──▶ atomic write to data/metrics.cache,
                            update last_timings + scrape_count
         any failure   ──▶ ExceptionGroup logged, previous cache kept
```

## Module tour

| Module | Responsibility |
|--------|----------------|
| `webapp/main.py` | Quart app, `/metrics` endpoint, request counting, `run_app()` seam used by the CLI |
| `webapp/cli.py` | click CLI: TLS preflight, self-signed cert generation, systemd install/uninstall |
| `webapp/core/model.py` | `Metric` dataclass and the `SampleValue` result union |
| `webapp/core/base.py` | `MetricHandler` ABC — the handler interface with default phases |
| `webapp/core/helpers.py` | `HandlerSupport`: config verification, command execution, output parsing, exposition rendering |
| `webapp/core/process.py` | `ProcessRunner`: safe async subprocess execution |
| `webapp/core/loader.py` | dynamic handler discovery |
| `webapp/core/timing.py` | `ScrapeTimings`/`HandlerTimings` + `measure()` context manager |
| `webapp/core/builtin.py` | builtin command providers and OS collectors |
| `webapp/core/runtime.py` | `Runtime`: scrape loop, cache orchestration, shared state |
| `webapp/handlers/bash.py` | `BashMetricHandler` — metrics from shell commands |
| `webapp/handlers/builtin.py` | `BuiltinMetricHandler` — runtime/OS state, no subprocess per metric |

## The scrape cycle

One cycle in `Runtime.scrape_once()`:

1. **Prepare** — all handlers concurrently run
   `read()` (parse `data/config/<handler>.json` into `Metric` lists)
   and `verify()` (validate; every problem is collected into one
   `ValueError`, not just the first). Configs are re-read and
   re-verified **every cycle**, so config edits go live without a
   restart.
2. **Duplicate check** — the runtime verifies that no metric series
   (including `_bucket`/`_sum`/`_count` series of histograms and
   summaries) is exported by two different handlers.
3. **Collect** — all handlers concurrently run
   `execute(metrics)` and `finalize(metrics, results)`. Fan-out uses
   `asyncio.gather(..., return_exceptions=True)`; collected failures
   are raised as one `ExceptionGroup`.
4. **Publish** — the per-handler exposition blocks are joined and
   written **atomically** to `data/metrics.cache` (temp file +
   `os.replace`). Only on success: `last_timings` is replaced and
   `scrape_count` is incremented.

The background loop (`Runtime.serve(interval)`) repeats cycles
forever. Each cycle is capped at the interval with
`asyncio.wait_for`, so a stuck handler can never block the next
cycle. On timeout the cycle is aborted and the previous cache is
kept.

## Serving model

- `/metrics` reads the cache file (via `asyncio.to_thread`) and
  returns it verbatim: HTTP 200 with
  `text/plain; version=0.0.4; charset=utf-8`.
- Before the first successful scrape the endpoint answers
  **503** with a static body (`no metrics available yet\n`) — the app
  never serves partial or broken exposition output.
- Every incoming HTTP request increments `Runtime.request_count`
  via a `before_request` hook; the counter is exported as
  `builtin.runtime.requests`.
- Internal details (stack traces, error reasons) go to the log —
  HTTP error bodies are static strings.

## Handler discovery

`load_handlers("webapp.handlers")` imports every module in the
handlers package (skipping `_`-prefixed ones) and looks for
`MetricHandler` subclasses:

- exactly one handler class per module → registered under the module
  name (e.g. `foo.py` → handler `foo`)
- zero classes → the module is skipped with a warning
- more than one → startup error

A handler is only *activated* if `data/config/<name>.json` exists.
If the handler class defines `bind_runtime(runtime)`, the runtime
injects itself (the builtin handler uses this to read shared state).

## Failure semantics

| What fails | What happens |
|------------|--------------|
| First scrape at startup | the app refuses to start |
| Any handler phase during a cycle | whole cycle fails; exception group is logged; previous cache and timings stay; app keeps serving |
| A single command of the bash handler | its execute fails → the cycle fails (no partial output) |
| A builtin OS probe (missing binary, timeout, non-zero exit) | logged as warning, the metric reports `0.0`; the cycle still succeeds |
| Cycle exceeds the scrape interval | cycle aborted by `asyncio.wait_for`, logged, previous cache kept |

## Concurrency and state

Everything runs on a single asyncio event loop: handlers are
coroutines, subprocesses are spawned with
`asyncio.create_subprocess_exec`, blocking file reads go through
`asyncio.to_thread`. Handler instances are **stateless** — phases
communicate through parameters and return values, instances are
shared across cycles. The only mutable state lives on the `Runtime`:

- `last_timings` — `ScrapeTimings` of the last successful cycle
- `request_count` / `scrape_count` — monotonic counters
  (incremented by the HTTP hook / on cycle success)

Both are exported by the builtin handler (see
[builtin commands](builtin-commands.md)).

Note that builtin metrics lag one cycle: they are written *during* a
cycle, before that cycle's counters/timings are updated — the same
one-cycle lag the timings have.

## Files on disk

| Path | Meaning |
|------|---------|
| `data/config/*.json` | handler configs (source-controlled) |
| `data/metrics.cache` | last good scrape result (runtime artifact) |
| `data/keys/cert.pem`, `data/keys/key.pem` | TLS material (runtime artifact, generated on first https start) |

Runtime artifacts are gitignored and removed by `just clean`.

## Deployment

`run_app()` in `main.py` is the single seam that starts Quart
(`app.run(..., use_reloader=False)`); the CLI calls it after its TLS
preflight. `--install-systemd` wraps the whole thing in a systemd
service — see the [CLI reference](cli.md).

## Known boundaries

- No authentication on `/metrics` — trusted network assumed
- Stdout/stderr of commands are fully buffered; configs are trusted
- OS probes (packages, reboot-required) are Debian-specific
- No multi-process serving: one process, one event loop, one port
