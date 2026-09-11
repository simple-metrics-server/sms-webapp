# Handler: bash

`BashMetricHandler` (`webapp/handlers/bash.py`, config
`data/config/bash.json`) collects metrics by running one command per
metric and parsing its stdout. See [handlers.md](handlers.md) for the
handler model and the full output-parsing rules.

## Command reference

The bash handler defines **no fixed command constants** — every
metric's `cmd` is an arbitrary command line. It is split with `shlex`
and executed with `asyncio.create_subprocess_exec`, so there is **no
shell**: `&&`, pipes, `;` and redirections do not work. Use a single
binary (`cut`, `grep`, a script file).

| `cmd` | Explanation |
|-------|-------------|
| any command line | run without a shell; stdout is parsed according to the metric's `value_type` |

## Output contract

| `value_type` | Required stdout |
|--------------|-----------------|
| `counter`, `gauge`, `untyped` | exactly one line with one finite number (canonicalized with `repr(float(v))`; `1` renders as `1.0`) |
| `histogram` | one `key value` line per declared bucket, plus `sum` and `count` |
| `summary` | one `key value` line per declared quantile, plus `sum` and `count` |

Details, rejection rules and rendering examples live in
[handlers.md](handlers.md#command-execution-rules-bash-handler).

## Example config

```json
[
    {
        "name": "sms_bash_uptime_seconds",
        "help_text": "System uptime in seconds",
        "value_type": "gauge",
        "cmd": "cut -d. -f1 /proc/uptime",
        "timeout": 5
    }
]
```

## Customizing

Edit `data/config/bash.json` (changes go live on the next cycle — no
restart): add, remove or rename metrics. All commands of a cycle run
concurrently, and a failing command fails the whole cycle (no partial
output).
