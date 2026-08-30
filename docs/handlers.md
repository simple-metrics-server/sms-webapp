# Handlers

Handlers are the pluggable collectors of the app. Each handler owns a
JSON config (`data/config/<name>.json`), runs once per scrape cycle,
and renders its metrics into Prometheus exposition format. Handlers
live in `webapp/handlers/` — the module name **is** the handler name.

## Adding a handler

Two steps, no other changes:

1. Drop `foo.py` into `webapp/handlers/` defining **exactly one**
   `MetricHandler` subclass.
2. Add `data/config/foo.json` (a JSON array of metric definitions).
   Without this file the handler is discovered but not activated.

Minimal handler:

```python
from typing import ClassVar

from webapp.core.base import MetricHandler
from webapp.core.model import Metric, SampleValue


class FooMetricHandler(MetricHandler):
    required_fields: ClassVar[list[str]] = ["cmd"]

    async def execute(
        self, metrics: list[Metric]
    ) -> dict[str, SampleValue]:
        return await self.support.run_cmds(metrics)
```

Loader rules: zero handler classes in a module → skipped with a
warning; more than one → startup error. Modules starting with `_` are
ignored (useful for shared helpers).

## The handler lifecycle

Every scrape cycle the runtime runs each handler through four
**stateless, async phases**:

| Phase | Signature (default from `MetricHandler`) | Purpose |
|-------|------------------------------------------|---------|
| `read()` | `-> list[Metric]` | parse the JSON config into metric definitions |
| `verify(metrics)` | `-> None` | validate; collect **all** problems into one `ValueError` |
| `execute(metrics)` | `-> dict[str, SampleValue]` | collect the values (abstract — the only required method) |
| `finalize(metrics, results)` | `-> str` | render the Prometheus exposition block |

Rules of the game:

- **Stateless**: never store per-scrape state on `self`; instances
  are shared across cycles. Data flows through parameters and return
  values only.
- **Concurrent**: all handlers run their phases concurrently via
  `asyncio.gather`; failures are aggregated into an `ExceptionGroup`.
- **All-or-nothing**: if any phase of any handler fails, the whole
  cycle fails and the previous cache keeps being served. Never emit
  partial output.
- **Runtime access**: if the handler defines
  `bind_runtime(self, runtime)`, the runtime injects itself at
  activation — the builtin handler uses this to expose shared state.

## Metric config fields (`data/config/<handler>.json`)

| field | type | notes |
|-------|------|-------|
| `name` | str | must match `^[a-zA-Z_:][a-zA-Z0-9_:]*$`; convention: `sms_<handler>_...` (e.g. `sms_foo_requests`) |
| `help_text` | str | rendered in `# HELP`; backslashes and newlines are escaped |
| `value_type` | str | one of `counter`, `gauge`, `untyped`, `histogram`, `summary` |
| `cmd` | str | command line, parsed with `shlex` — **no shell**; which fields are required is up to the handler (`required_fields`) |
| `timeout` | number | seconds, must be > 0 |
| `buckets` | list | histogram only — non-empty, finite, strictly ascending upper bounds; `+Inf` is implicit |
| `quantiles` | list | summary only — strictly ascending numbers in (0, 1) exclusive |

Series names must be unique within a handler **and** across all
handlers (checked by the runtime every cycle). Histograms occupy
`<name>_bucket`, `<name>_sum`, `<name>_count`; summaries occupy
`<name>`, `<name>_sum`, `<name>_count`.

## Command execution rules (bash handler)

- `cmd` is split with `shlex` and executed via
  `asyncio.create_subprocess_exec` — no shell syntax (`&&`, pipes,
  `;`, redirection) works. Use single binaries (`cut`, `grep`, a
  script file).
- All commands of a cycle run concurrently.
- On timeout or cancellation the **process group** is killed — no
  stragglers.
- stdout/stderr are fully buffered (configs are trusted; do not
  point commands at huge outputs).

### Scalar output

A scalar metric's command must print exactly one line with one
finite number. Values are canonicalized with `repr(float(v))` — `1`
renders as `1.0`. Empty, multiline, non-numeric, `nan`/`inf` and
underscore-numeric (`1_0`) outputs are rejected.

```json
{
    "name": "sms_bash_uptime_seconds",
    "help_text": "System uptime in seconds",
    "value_type": "gauge",
    "cmd": "cut -d. -f1 /proc/uptime",
    "timeout": 5
}
```

```
$ cut -d. -f1 /proc/uptime
481311
```

renders as:

```
# HELP sms_bash_uptime_seconds System uptime in seconds
# TYPE sms_bash_uptime_seconds gauge
sms_bash_uptime_seconds 481311.0
```

### Histogram and summary output

Distribution metrics print one `key value` line per series, followed
by the reserved lines `sum` and `count`:

```
0.1 1
0.5 3
1 3
sum 0.97
count 3
```

Rules, all enforced per cycle:

- keys are matched **numerically** against the declared
  `buckets`/`quantiles` (`0.10` matches `0.1`)
- every declared key, `sum` and `count` must appear exactly once;
  unknown, duplicate or missing keys fail the cycle
- bucket counts must be non-decreasing and `count >=` the last bucket
- values are canonicalized like scalars

Rendering:

```
sms_bash_lat_bucket{le="0.1"} 1.0      # histogram
sms_bash_lat_bucket{le="0.5"} 3.0
sms_bash_lat_bucket{le="1.0"} 3.0
sms_bash_lat_bucket{le="+Inf"} 3.0     # from count
sms_bash_lat_sum 0.97
sms_bash_lat_count 3.0
```

```
sms_bash_lat{quantile="0.5"} 0.25      # summary
sms_bash_lat{quantile="0.99"} 0.9
sms_bash_lat_sum 0.97
sms_bash_lat_count 3.0
```

### Labeled values

`execute()` may return a `LabeledSample(label_name, values)` for a
metric — one `name{label="value"} number` line per entry, sorted by
label value, label values escaped. The builtin handler uses this for
per-user session counts; see `webapp.core.model.SampleValue` for the
full result union (`str | DistributionSample | LabeledSample`).

## The shipped handlers

### bash — `webapp/handlers/bash.py`

`BashMetricHandler` executes each metric's `cmd` (see above) via
`HandlerSupport.run_cmds` and parses scalar or distribution output.
Config: `data/config/bash.json`.

### builtin — `webapp/handlers/builtin.py`

`BuiltinMetricHandler` does not spawn a process per metric; its `cmd`
names a builtin provider (runtime state, OS probes, hardware). See
[builtin commands](builtin-commands.md) for the full reference.

## Testing a new handler

- `just check` runs the suite; add tests under `tests/` (one file
  per component, e.g. `tests/test_foo.py`).
- Tests must not depend on shipped configs — use `tmp_path`
  fixtures and write your own config JSON.
- Exercise `read`/`verify`/`execute` in isolation with
  `asyncio.run(...)`; see `tests/test_bash.py` for the pattern.
