import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass, field
from typing import ClassVar

from webapp.core.base import MetricHandler
from webapp.core.model import (
    LabeledSample,
    Metric,
    MultiLabeledSample,
    SampleValue,
)

log = logging.getLogger(__name__)

STATUS_PATH_ENV = "OPENVPN_STATUS_PATH"
"""Env var overriding the status file paths from the config `cmd`."""

UP = "sms_openvpn_up"
SERVER_UP = "sms_openvpn_server_up"
CLIENT_UP = "sms_openvpn_client_up"
STATUS_UPDATE_TIME = "sms_openvpn_status_update_time_seconds"
CONNECTED_CLIENTS = "sms_openvpn_server_connected_clients"
ROUTE_METRIC = "sms_openvpn_server_route_last_reference_time_seconds"

STATUS_PATH_LABEL = "status_path"
TYPE_LABEL = "type"
TYPE_CLIENT = "client"
TYPE_SERVER = "server"
TYPE_UNKNOWN = "unknown"

CLIENT_COUNTERS = {
    "sms_openvpn_client_tun_tap_read_bytes_total": "TUN/TAP read bytes",
    "sms_openvpn_client_tun_tap_write_bytes_total": "TUN/TAP write bytes",
    "sms_openvpn_client_tcp_udp_read_bytes_total": "TCP/UDP read bytes",
    "sms_openvpn_client_tcp_udp_write_bytes_total": "TCP/UDP write bytes",
    "sms_openvpn_client_auth_read_bytes_total": "Auth read bytes",
    "sms_openvpn_client_pre_compress_bytes_total": "pre-compress bytes",
    "sms_openvpn_client_post_compress_bytes_total": "post-compress bytes",
    "sms_openvpn_client_pre_decompress_bytes_total": "pre-decompress bytes",
    "sms_openvpn_client_post_decompress_bytes_total": "post-decompress bytes",
}

SERVER_CLIENT_METRICS = {
    "sms_openvpn_server_client_received_bytes_total": "Bytes Received",
    "sms_openvpn_server_client_sent_bytes_total": "Bytes Sent",
}

_ROUTE_COLUMN = "Last Ref (time_t)"

KNOWN_METRICS = frozenset(
    {
        UP,
        SERVER_UP,
        CLIENT_UP,
        STATUS_UPDATE_TIME,
        CONNECTED_CLIENTS,
        ROUTE_METRIC,
        *CLIENT_COUNTERS,
        *SERVER_CLIENT_METRICS,
    }
)

_CLIENT_KEYS = frozenset(CLIENT_COUNTERS.values())
_SERVER_METRIC_COLUMNS = frozenset(
    {*SERVER_CLIENT_METRICS.values(), _ROUTE_COLUMN}
)

_SERVER_CLIENT_LABELS = [
    STATUS_PATH_LABEL,
    TYPE_LABEL,
    "common_name",
    "connection_time",
    "real_address",
    "virtual_address",
    "username",
]
_SERVER_CLIENT_COLUMNS = [
    "Common Name",
    "Connected Since (time_t)",
    "Real Address",
    "Virtual Address",
    "Username",
]
_ROUTE_LABELS = [
    STATUS_PATH_LABEL,
    TYPE_LABEL,
    "common_name",
    "real_address",
    "virtual_address",
]
_ROUTE_COLUMNS = ["Common Name", "Real Address", "Virtual Address"]


@dataclass
class ParsedStatus:
    """One parsed OpenVPN status file.

    kind is "client" or "server" for a successful parse, "" on error.
    server_clients/server_routes hold one column-name -> value dict per
    row, with numeric metric columns already canonicalized; error holds
    the failure reason for an unreadable or malformed file.
    """

    kind: str = ""
    update_time: str | None = None
    client: dict[str, str] = field(default_factory=dict)
    server_clients: list[dict[str, str]] = field(default_factory=list)
    server_routes: list[dict[str, str]] = field(default_factory=list)
    connected_clients: int = 0
    error: str | None = None


def _parse_path_string(raw: str) -> list[str]:
    """Split a status path spec into individual paths.

    Accepts a JSON array (`["a", "b"]`), a bracketed list with or
    without quotes (`[a, b]`, `['a', 'b']`), a comma-separated list, or
    a plain space-separated string (shell quoting supported).
    """
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        return [str(path) for path in parsed]
    if text.startswith("[") and text.endswith("]"):
        parts = text[1:-1].split(",")
    elif "," in text:
        parts = text.split(",")
    else:
        return shlex.split(text)
    return [_strip_quotes(part.strip()) for part in parts if part.strip()]


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _paths(m: Metric) -> list[str]:
    if isinstance(m.cmd, list):
        return [str(path) for path in m.cmd]
    return _parse_path_string(m.cmd)


def _status_path_override() -> list[str]:
    """Paths from the OPENVPN_STATUS_PATH env var, empty when unset."""
    return _parse_path_string(os.environ.get(STATUS_PATH_ENV, ""))


def _timeout_for(
    path: str, metrics: list[Metric], override: list[str]
) -> float:
    """Largest timeout among the metrics requesting this status path."""
    if override:
        return max((m.timeout for m in metrics), default=1.0)
    return max((m.timeout for m in metrics if path in _paths(m)), default=1.0)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _is_empty_sample(value: SampleValue) -> bool:
    """True for a labeled family with no rows (nothing to expose)."""
    if isinstance(value, LabeledSample):
        return not value.values
    if isinstance(value, MultiLabeledSample):
        return not value.rows
    return False


def _asctime_to_epoch(value: str) -> str:
    """Parse an OpenVPN timestamp (ISO 8601 or asctime) as local time."""
    normalized = " ".join(value.split())
    for fmt in ("%Y-%m-%d %H:%M:%S", "%a %b %d %H:%M:%S %Y"):
        try:
            parsed = time.strptime(normalized, fmt)
        except ValueError:
            continue
        return repr(float(time.mktime(parsed)))
    raise ValueError(f"unrecognized timestamp {value!r}")


class OpenVpnMetricHandler(MetricHandler):
    """Handler that parses OpenVPN status files.

    Each metric's `cmd` holds the status file path(s) of the OpenVPN
    `--status` file(s) to read: either a JSON array of paths or a single
    space-separated string. The format is auto-detected
    from the file's first line: the classic `OpenVPN CLIENT LIST`
    (status version 1), server `--status-version 2` and 3, and client
    statistics are all supported. Server parsing mirrors the
    kumina/openvpn_exporter; version 1 rows are normalized to the same
    shape (timestamps converted to epoch, absent columns left empty).
    Metric names select the data; the exposed families mirror that
    exporter (with the repository's `sms_` prefix).

    A status file that is missing, unreadable or malformed reports
    `sms_openvpn_up{status_path}=0` and yields no other samples for
    that path (a warning is logged); it never fails the cycle.

    When the `OPENVPN_STATUS_PATH` env var is set, it overrides the
    paths from the config (a JSON array or a space-separated string),
    so installs can point the shipped config at their status files
    without editing every entry.
    """

    required_fields: ClassVar[list[str]] = ["cmd"]

    def _parse(self) -> list[Metric]:
        """Accept `cmd` as a path list as well as a command-line string.

        A JSON array is joined with shell quoting so the inherited
        verification and path handling can keep treating `cmd` as a
        string (paths containing spaces survive the round-trip).
        """
        metrics = super()._parse()
        for metric in metrics:
            cmd = metric.cmd
            if isinstance(cmd, list):
                metric.cmd = shlex.join(str(path) for path in cmd)
        return metrics

    async def verify(self, metrics: list[Metric]) -> None:
        await super().verify(metrics)
        unknown = sorted(
            {m.name for m in metrics if m.name not in KNOWN_METRICS}
        )
        if unknown:
            msg = f"Invalid config {self.config_path}:\n" + "\n".join(
                f"  - metric {name!r}: unknown openvpn metric"
                for name in unknown
            )
            log.error(msg)
            raise ValueError(msg)

    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        override = _status_path_override()
        paths: list[str] = list(override)
        if not paths:
            for m in metrics:
                for path in _paths(m):
                    if path not in paths:
                        paths.append(path)
        parsed: dict[str, ParsedStatus] = {}
        for path in paths:
            timeout = _timeout_for(path, metrics, override)
            parsed[path] = await self._parse_path(path, timeout)
        return self._collect(metrics, parsed)

    async def finalize(
        self,
        metrics: list[Metric],
        results: dict[str, SampleValue],
    ) -> str:
        """Render only the families that actually produced samples.

        A status file is either client or server format, so the
        configured families for the other kind have no rows. Emitting
        their HELP/TYPE with no samples is noise; drop them instead.
        """
        missing = [m.name for m in metrics if m.name not in results]
        if missing:
            raise ValueError(f"No results for metrics: {missing}")
        kept = [m for m in metrics if not _is_empty_sample(results[m.name])]
        return self.support.render_exposition(kept, results)

    async def _parse_path(self, path: str, timeout: float) -> ParsedStatus:
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(_read_file, path), timeout
            )
        except (OSError, TimeoutError) as e:
            log.warning("openvpn: reading %r failed: %s", path, e)
            return ParsedStatus(error=str(e))
        try:
            return self._parse_text(text)
        except ValueError as e:
            log.warning("openvpn: parsing %r failed: %s", path, e)
            return ParsedStatus(error=str(e))

    def _parse_text(self, text: str) -> ParsedStatus:
        first = text.splitlines()[0] if text else ""
        if first.startswith("TITLE,"):
            return self._parse_server(text, ",")
        if first.startswith("TITLE\t"):
            return self._parse_server(text, "\t")
        if first.startswith("OpenVPN CLIENT LIST"):
            return self._parse_server_v1(text)
        if first.startswith("OpenVPN STATISTICS"):
            return self._parse_client(text)
        raise ValueError(f"unexpected file contents: {first[:40]!r}")

    def _parse_client(self, text: str) -> ParsedStatus:
        parsed = ParsedStatus(kind="client")
        for line in text.splitlines():
            fields = line.split(",")
            key = fields[0]
            if key == "END" and len(fields) == 1:
                continue
            if key == "OpenVPN STATISTICS" and len(fields) == 1:
                continue
            if key == "Updated" and len(fields) == 2:
                parsed.update_time = _asctime_to_epoch(fields[1])
                continue
            if key in _CLIENT_KEYS and len(fields) == 2:
                parsed.client[key] = self.support.parse_value(
                    "openvpn", fields[1]
                )
                continue
            raise ValueError(f"unsupported key {key!r}")
        return parsed

    def _parse_server(self, text: str, separator: str) -> ParsedStatus:
        parsed = ParsedStatus(kind="server")
        headers: dict[str, list[str]] = {}
        for line in text.splitlines():
            fields = line.split(separator)
            key = fields[0]
            if key == "END" and len(fields) == 1:
                continue
            if key == "GLOBAL_STATS":
                continue
            if key == "HEADER" and len(fields) > 2:
                headers[fields[1]] = fields[2:]
                continue
            if key == "TIME" and len(fields) == 3:
                parsed.update_time = self.support.parse_value(
                    "openvpn", fields[2]
                )
                continue
            if key == "TITLE" and len(fields) == 2:
                continue
            if key == "CLIENT_LIST":
                parsed.connected_clients += 1
                parsed.server_clients.append(
                    self._server_row(
                        fields, headers.get("CLIENT_LIST"), 1, "CLIENT_LIST"
                    )
                )
                continue
            if key == "ROUTING_TABLE":
                parsed.server_routes.append(
                    self._server_row(
                        fields, headers.get("ROUTING_TABLE"), 1, "ROUTING_TABLE"
                    )
                )
                continue
            raise ValueError(f"unsupported key {key!r}")
        return parsed

    def _parse_server_v1(self, text: str) -> ParsedStatus:
        """Parse the classic `OpenVPN CLIENT LIST` status format.

        This format has no HEADER directives; column names appear as the
        section's first row and rows have no table-name prefix. Rows are
        normalized to the same shape as the v2/v3 format: timestamps are
        converted to epoch and missing columns (virtual address,
        username) are left empty.
        """
        parsed = ParsedStatus(kind="server")
        section: str | None = None
        header: list[str] | None = None
        for line in text.splitlines():
            if not line:
                continue
            fields = line.split(",")
            key = fields[0]
            if key == "OpenVPN CLIENT LIST":
                section, header = None, None
                continue
            if key == "ROUTING TABLE":
                section, header = "routing", None
                continue
            if key == "GLOBAL STATS":
                section, header = "global", None
                continue
            if key == "END":
                continue
            if key == "Updated" and len(fields) == 2:
                parsed.update_time = _asctime_to_epoch(fields[1])
                continue
            if section == "global":
                continue
            if header is None:
                header = fields
                if section is None:
                    section = "client"
                continue
            if section == "client":
                parsed.connected_clients += 1
                parsed.server_clients.append(
                    self._server_row_v1(fields, header, "CLIENT_LIST")
                )
                continue
            if section == "routing":
                parsed.server_routes.append(
                    self._server_row_v1(fields, header, "ROUTING_TABLE")
                )
                continue
            raise ValueError(f"unsupported key {key!r}")
        return parsed

    def _server_row_v1(
        self, fields: list[str], columns: list[str], table: str
    ) -> dict[str, str]:
        row = self._server_row(fields, columns, 0, table)
        since = row.get("Connected Since")
        if since:
            row["Connected Since (time_t)"] = _asctime_to_epoch(since)
        last_ref = row.get("Last Ref")
        if last_ref:
            row["Last Ref (time_t)"] = _asctime_to_epoch(last_ref)
        row.setdefault("Virtual Address", "")
        row.setdefault("Username", "")
        return row

    def _server_row(
        self,
        fields: list[str],
        columns: list[str] | None,
        offset: int,
        table: str,
    ) -> dict[str, str]:
        if columns is None:
            raise ValueError(f"{table} should be preceded by HEADER")
        if len(fields) != len(columns) + offset:
            raise ValueError(
                f"HEADER for {table} describes a different number of columns"
            )
        row = {columns[i]: fields[i + offset] for i in range(len(columns))}
        for column in _SERVER_METRIC_COLUMNS & row.keys():
            row[column] = self.support.parse_value("openvpn", row[column])
        return row

    def _collect(
        self,
        metrics: list[Metric],
        parsed: dict[str, ParsedStatus],
    ) -> dict[str, SampleValue]:
        return {m.name: self._value_for(m, parsed) for m in metrics}

    def _value_for(
        self,
        m: Metric,
        parsed: dict[str, ParsedStatus],
    ) -> SampleValue:
        if m.name == UP:
            return MultiLabeledSample(
                [STATUS_PATH_LABEL, TYPE_LABEL],
                {
                    (path, st.kind or TYPE_UNKNOWN): (
                        "0.0" if st.error else "1.0"
                    )
                    for path, st in parsed.items()
                },
            )
        if m.name == SERVER_UP:
            return LabeledSample(
                STATUS_PATH_LABEL,
                {
                    path: (
                        "1.0"
                        if st.kind == TYPE_SERVER and not st.error
                        else "0.0"
                    )
                    for path, st in parsed.items()
                },
            )
        if m.name == CLIENT_UP:
            return LabeledSample(
                STATUS_PATH_LABEL,
                {
                    path: (
                        "1.0"
                        if st.kind == TYPE_CLIENT and not st.error
                        else "0.0"
                    )
                    for path, st in parsed.items()
                },
            )
        if m.name == STATUS_UPDATE_TIME:
            return MultiLabeledSample(
                [STATUS_PATH_LABEL, TYPE_LABEL],
                {
                    (path, st.kind): st.update_time
                    for path, st in parsed.items()
                    if not st.error and st.update_time is not None
                },
            )
        if m.name == CONNECTED_CLIENTS:
            return MultiLabeledSample(
                [STATUS_PATH_LABEL, TYPE_LABEL],
                {
                    (path, TYPE_SERVER): repr(float(st.connected_clients))
                    for path, st in parsed.items()
                    if st.kind == TYPE_SERVER and not st.error
                },
            )
        if m.name == ROUTE_METRIC:
            return MultiLabeledSample(
                _ROUTE_LABELS,
                self._rows(
                    parsed,
                    "server_routes",
                    _ROUTE_COLUMNS,
                    _ROUTE_COLUMN,
                ),
            )
        if m.name in SERVER_CLIENT_METRICS:
            column = SERVER_CLIENT_METRICS[m.name]
            return MultiLabeledSample(
                _SERVER_CLIENT_LABELS,
                self._rows(
                    parsed,
                    "server_clients",
                    _SERVER_CLIENT_COLUMNS,
                    column,
                ),
            )
        key = CLIENT_COUNTERS[m.name]
        return MultiLabeledSample(
            [STATUS_PATH_LABEL, TYPE_LABEL],
            {
                (path, TYPE_CLIENT): st.client[key]
                for path, st in parsed.items()
                if st.kind == TYPE_CLIENT and not st.error and key in st.client
            },
        )

    def _rows(
        self,
        parsed: dict[str, ParsedStatus],
        attr: str,
        label_columns: list[str],
        value_column: str,
    ) -> dict[tuple[str, ...], str]:
        rows: dict[tuple[str, ...], str] = {}
        for path, status in parsed.items():
            if status.kind != TYPE_SERVER or status.error:
                continue
            for row in getattr(status, attr):
                value = row.get(value_column)
                if value is None:
                    continue
                label_key = (
                    path,
                    TYPE_SERVER,
                    *(row.get(c, "") for c in label_columns),
                )
                if label_key not in rows:
                    rows[label_key] = value
        return rows
