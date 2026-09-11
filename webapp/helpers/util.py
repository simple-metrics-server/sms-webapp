import json
import logging
import os
import shlex

from webapp.core.process import ProcessRunner

log = logging.getLogger(__name__)


def env_string(name: str, default: str = "") -> str:
    """Read environment variable `name` as a plain string."""
    return os.environ.get(name, default)


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    """Read environment variable `name` as a list of strings.

    Accepts a JSON array, a bracketed or comma-separated (CSV) list, or
    a space-separated string; entries may be single- or double-quoted.
    Unset/empty yields `default` (or an empty list).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return list(default) if default is not None else []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    if raw.startswith("[") and raw.endswith("]"):
        parts = raw[1:-1].split(",")
    elif "," in raw:
        parts = raw.split(",")
    else:
        return shlex.split(raw)
    return [_strip_quotes(part.strip()) for part in parts if part.strip()]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_key_value(text: str) -> dict[str, list[str]]:
    """Parse space-separated lines into a key -> values mapping.

    Each non-empty line is split on whitespace: the first token is the
    key, the remaining tokens are its values (possibly empty).
    """
    params: dict[str, list[str]] = {}
    for line in text.splitlines():
        tokens = line.split()
        if tokens:
            params[tokens[0]] = tokens[1:]
    return params


def param(params: dict[str, list[str]], key: str, index: int = 0) -> str | None:
    """Return a value of `key` from a parse_key_value mapping, or None."""
    values = params.get(key)
    if not values or index >= len(values):
        return None
    return values[index]


def param_is(params: dict[str, list[str]], key: str, value: str) -> bool:
    """True if `key`'s first value in `params` equals `value`."""
    return param(params, key) == value


def path_stem(path: str) -> str:
    """Return a file name without its directory or extension."""
    return os.path.splitext(os.path.basename(path))[0]


def to_float(value: str) -> float | None:
    """Parse a float, returning None when it is not numeric."""
    try:
        return float(value)
    except ValueError:
        return None


def to_int(value: str) -> int | None:
    """Parse an int, returning None when it is not numeric."""
    try:
        return int(value)
    except ValueError:
        return None


def read_text(path: str) -> str | None:
    """Read a UTF-8 text file, logging and returning None on error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as e:
        log.warning("reading %r failed: %s", path, e)
        return None


async def run_command(
    runner: ProcessRunner, argv: list[str], timeout: float
) -> str | None:
    """Run a command, logging and returning None on failure."""
    try:
        return await runner.run(argv, timeout)
    except (OSError, RuntimeError, ValueError) as e:
        log.warning("command %r failed: %s", argv, e)
        return None


async def run_command_status(
    runner: ProcessRunner, argv: list[str], timeout: float
) -> tuple[int, str] | None:
    """Run a command, returning (exit code, stdout) or None on failure.

    A non-zero exit code is returned, not treated as an error — some
    probes signal results through it.
    """
    try:
        return await runner.run_status(argv, timeout)
    except (OSError, RuntimeError, ValueError) as e:
        log.warning("command %r failed: %s", argv, e)
        return None


def map_row(
    fields: list[str],
    columns: list[str] | None,
    *,
    offset: int = 1,
    context: str = "row",
) -> dict[str, str]:
    """Map a delimited data row to {column: value} using its header.

    `offset` is the number of leading fields before the columns (e.g. 1
    when the row starts with a table name). The field count must match
    the header, otherwise ValueError is raised.
    """
    if columns is None:
        raise ValueError(f"{context} without a preceding header")
    if len(fields) != len(columns) + offset:
        raise ValueError(f"{context} column count does not match its header")
    return {columns[i]: fields[i + offset] for i in range(len(columns))}
