import asyncio
import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

from webapp.common.model import Metric, MultiLabeledSample, SampleValue
from webapp.core.base import MetricHandler
from webapp.helpers.util import (
    env_list,
    map_row,
    param,
    param_is,
    parse_key_value,
    path_stem,
    read_text,
    to_float,
)

log = logging.getLogger(__name__)

CONFIG_PATHS_ENV = "OPENVPN_CONFIG_PATH"
"""Env var holding an array of OpenVPN config file paths."""

STATUS_PATHS_ENV = "OPENVPN_STATUS_PATH"
"""Env var holding an array of OpenVPN status file paths."""

STATUS_VERSION_KEY = "status-version"
STATUS_VERSION = "3"
MODE_KEY = "mode"
MODE_SERVER = "server"

NETWORK_LABEL = "network"
TYPE_LABEL = "type"
STATUS_PATH_LABEL = "status_path"

CLIENT_COLUMNS = {
    "openvpn.client.tun_tap_read_bytes": "TUN/TAP read bytes",
    "openvpn.client.tun_tap_write_bytes": "TUN/TAP write bytes",
    "openvpn.client.tcp_udp_read_bytes": "TCP/UDP read bytes",
    "openvpn.client.tcp_udp_write_bytes": "TCP/UDP write bytes",
    "openvpn.client.auth_bytes": "Auth read bytes",
    "openvpn.client.pre_compress_bytes": "pre-compress bytes",
    "openvpn.client.post_compress_bytes": "post-compress bytes",
    "openvpn.client.pre_decompress_bytes": "pre-decompress bytes",
    "openvpn.client.post_decompress_bytes": "post-decompress bytes",
}

SERVER_CLIENT_COLUMNS = {
    "openvpn.server.client_received_bytes": "Bytes Received",
    "openvpn.server.client_sent_bytes": "Bytes Sent",
}

CLIENT_LABELS = [NETWORK_LABEL, TYPE_LABEL]
SERVER_CLIENT_LABELS = [
    NETWORK_LABEL,
    TYPE_LABEL,
    "common_name",
    "connection_time",
    "real_address",
    "virtual_address",
    "username",
]
SERVER_CLIENT_KEYS = [
    "Common Name",
    "Connected Since (time_t)",
    "Real Address",
    "Virtual Address",
    "Username",
]
ROUTE_LABELS = [
    NETWORK_LABEL,
    TYPE_LABEL,
    "common_name",
    "real_address",
    "virtual_address",
]
ROUTE_KEYS = ["Common Name", "Real Address", "Virtual Address"]

CONFIG_NUMBERS = {
    "openvpn.config.port": ("port", 0),
    "openvpn.config.verb": ("verb", 0),
    "openvpn.config.script_security": ("script-security", 0),
    "openvpn.config.keepalive_interval": ("keepalive", 0),
    "openvpn.config.keepalive_timeout": ("keepalive", 1),
}
CONFIG_FLAGS = {
    "openvpn.config.client_to_client": "client-to-client",
    "openvpn.config.duplicate_cn": "duplicate-cn",
    "openvpn.config.persist_key": "persist-key",
    "openvpn.config.persist_tun": "persist-tun",
}
INFO_KEYS = ["proto", "dev", "auth", "data-ciphers", "tls-version-min"]
INFO_LABELS = [
    NETWORK_LABEL,
    TYPE_LABEL,
    "proto",
    "dev",
    "auth",
    "cipher",
    "tls_version_min",
]
MAX_QUEUE_KEY = "Max bcast/mcast queue length"


@dataclass
class StatusData:
    """Parsed contents of an OpenVPN status file."""

    kind: str
    update_time: str | None = None
    clients: list[dict[str, str]] = field(default_factory=list)
    routes: list[dict[str, str]] = field(default_factory=list)
    counters: dict[str, str] = field(default_factory=dict)
    global_stats: dict[str, str] = field(default_factory=dict)


@dataclass
class Source:
    """A config/status pair with the info needed to parse the status."""

    network: str
    params: dict[str, list[str]]
    status_path: str
    kind: str


ParsedSource = tuple[Source, "StatusData | None"]


def parse_server_status(text: str) -> StatusData:
    """Parse a server status file (status-version 3, tab-separated)."""
    data = StatusData(kind="server")
    headers: dict[str, list[str]] = {}
    for line in text.splitlines():
        _parse_server_line(line, data, headers)
    return data


def _parse_server_line(
    line: str, data: StatusData, headers: dict[str, list[str]]
) -> None:
    """Handle one tab-separated server status line."""
    fields = line.split("\t")
    key = fields[0]
    if key == "TIME" and len(fields) == 3:
        data.update_time = fields[2]
    elif key == "HEADER" and len(fields) > 2:
        headers[fields[1]] = fields[2:]
    elif key == "GLOBAL_STATS" and len(fields) >= 3:
        data.global_stats[fields[1]] = fields[2]
    elif key in ("CLIENT_LIST", "ROUTING_TABLE"):
        rows = data.clients if key == "CLIENT_LIST" else data.routes
        rows.append(map_row(fields, headers.get(key), context=key))


def parse_client_status(text: str) -> StatusData:
    """Parse a client statistics file (comma-separated key,value)."""
    data = StatusData(kind="client")
    for line in text.splitlines():
        fields = line.split(",")
        if len(fields) != 2:
            continue
        if fields[0] == "Updated":
            data.update_time = fields[1]
        else:
            data.counters[fields[0]] = fields[1]
    return data


_PARSERS = {"server": parse_server_status, "client": parse_client_status}


def _number(value: str | None) -> str | None:
    if value is None:
        return None
    number = to_float(value)
    return None if number is None else repr(number)


def _timestamp(value: str) -> str | None:
    number = _number(value)
    if number is not None:
        return number
    parsed = time.strptime(" ".join(value.split()), "%a %b %d %H:%M:%S %Y")
    return repr(float(time.mktime(parsed)))


# --- metric extractors (openvpn.<command> -> SampleValue) ---


def _up(parsed: list[ParsedSource]) -> SampleValue:
    return MultiLabeledSample(
        CLIENT_LABELS,
        {
            (source.network, source.kind): (
                "1.0" if data is not None else "0.0"
            )
            for source, data in parsed
        },
    )


def _status_path(parsed: list[ParsedSource]) -> SampleValue:
    return MultiLabeledSample(
        [STATUS_PATH_LABEL, *CLIENT_LABELS],
        {
            (source.status_path, source.network, source.kind): (
                "1.0" if data is not None else "0.0"
            )
            for source, data in parsed
        },
    )


def _status_update_time(parsed: list[ParsedSource]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for source, data in parsed:
        if data is None or data.update_time is None:
            continue
        value = _timestamp(data.update_time)
        if value is not None:
            rows[(source.network, source.kind)] = value
    return MultiLabeledSample(CLIENT_LABELS, rows)


def _connected_clients(parsed: list[ParsedSource]) -> SampleValue:
    return MultiLabeledSample(
        CLIENT_LABELS,
        {
            (source.network, source.kind): repr(float(len(data.clients)))
            for source, data in parsed
            if data is not None and source.kind == "server"
        },
    )


def _client_counter(
    command: str,
) -> Callable[[list[ParsedSource]], SampleValue]:
    column = CLIENT_COLUMNS[command]

    def extract(parsed: list[ParsedSource]) -> SampleValue:
        rows: dict[tuple[str, ...], str] = {}
        for source, data in parsed:
            if data is None or source.kind != "client":
                continue
            value = _number(data.counters.get(column))
            if value is not None:
                rows[(source.network, source.kind)] = value
        return MultiLabeledSample(CLIENT_LABELS, rows)

    return extract


def _server_client(
    command: str,
) -> Callable[[list[ParsedSource]], SampleValue]:
    column = SERVER_CLIENT_COLUMNS[command]

    def extract(parsed: list[ParsedSource]) -> SampleValue:
        rows: dict[tuple[str, ...], str] = {}
        for source, data in parsed:
            if data is None or source.kind != "server":
                continue
            for row in data.clients:
                value = _number(row.get(column))
                if value is None:
                    continue
                key = (
                    source.network,
                    source.kind,
                    *(row.get(k, "") for k in SERVER_CLIENT_KEYS),
                )
                rows.setdefault(key, value)
        return MultiLabeledSample(SERVER_CLIENT_LABELS, rows)

    return extract


def _route(parsed: list[ParsedSource]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for source, data in parsed:
        if data is None or source.kind != "server":
            continue
        for row in data.routes:
            value = _number(row.get("Last Ref (time_t)"))
            if value is None:
                continue
            key = (
                source.network,
                source.kind,
                *(row.get(k, "") for k in ROUTE_KEYS),
            )
            rows.setdefault(key, value)
    return MultiLabeledSample(ROUTE_LABELS, rows)


def _config_number(
    key: str, index: int = 0
) -> Callable[[list[ParsedSource]], SampleValue]:
    def extract(parsed: list[ParsedSource]) -> SampleValue:
        rows: dict[tuple[str, ...], str] = {}
        for source, _ in parsed:
            value = _number(param(source.params, key, index))
            if value is not None:
                rows[(source.network, source.kind)] = value
        return MultiLabeledSample(CLIENT_LABELS, rows)

    return extract


def _config_flag(
    key: str,
) -> Callable[[list[ParsedSource]], SampleValue]:
    def extract(parsed: list[ParsedSource]) -> SampleValue:
        return MultiLabeledSample(
            CLIENT_LABELS,
            {
                (source.network, source.kind): (
                    "1.0" if key in source.params else "0.0"
                )
                for source, _ in parsed
            },
        )

    return extract


def _config_pool_size(parsed: list[ParsedSource]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for source, _ in parsed:
        values = source.params.get("ifconfig-pool")
        if not values or len(values) < 2:
            continue
        try:
            start = int(ipaddress.ip_address(values[0]))
            end = int(ipaddress.ip_address(values[1]))
        except ValueError:
            continue
        rows[(source.network, source.kind)] = repr(float(end - start + 1))
    return MultiLabeledSample(CLIENT_LABELS, rows)


def _config_info(parsed: list[ParsedSource]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for source, _ in parsed:
        labels = tuple(param(source.params, key) or "" for key in INFO_KEYS)
        rows[(source.network, source.kind, *labels)] = "1.0"
    return MultiLabeledSample(INFO_LABELS, rows)


def _max_bcast_mcast_queue(parsed: list[ParsedSource]) -> SampleValue:
    rows: dict[tuple[str, ...], str] = {}
    for source, data in parsed:
        if data is None or source.kind != "server":
            continue
        value = _number(data.global_stats.get(MAX_QUEUE_KEY))
        if value is not None:
            rows[(source.network, source.kind)] = value
    return MultiLabeledSample(CLIENT_LABELS, rows)


COMMANDS: dict[str, Callable[[list[ParsedSource]], SampleValue]] = {
    "openvpn.up": _up,
    "openvpn.status_path": _status_path,
    "openvpn.status_update_time": _status_update_time,
    "openvpn.server.connected_clients": _connected_clients,
    "openvpn.server.route_last_reference": _route,
    "openvpn.server.max_bcast_mcast_queue_length": _max_bcast_mcast_queue,
    "openvpn.config.pool_size": _config_pool_size,
    "openvpn.config.info": _config_info,
    **{command: _client_counter(command) for command in CLIENT_COLUMNS},
    **{command: _server_client(command) for command in SERVER_CLIENT_COLUMNS},
    **{
        command: _config_number(key, index)
        for command, (key, index) in CONFIG_NUMBERS.items()
    },
    **{command: _config_flag(key) for command, key in CONFIG_FLAGS.items()},
}


def is_known_command(command: str) -> bool:
    return command in COMMANDS


class OpenVpnMetricHandler(MetricHandler):
    """OpenVPN status handler.

    Config paths come from `OPENVPN_CONFIG_PATH` and status paths from
    `OPENVPN_STATUS_PATH`, both arrays of paths. Each config must set
    `status-version 3`; `mode server` marks a server config, anything
    else is a client. Metrics in `data/config/openvpn.json` name an
    `openvpn.*` command that extracts the value on demand.
    """

    required_fields: ClassVar[list[str]] = ["cmd"]

    async def verify(self, metrics: list[Metric]) -> None:
        await super().verify(metrics)
        unknown = sorted(
            {m.cmd for m in metrics if not is_known_command(m.cmd)}
        )
        if unknown:
            raise ValueError(
                f"Invalid config {self.config_path}: unknown openvpn "
                f"commands: {unknown}"
            )

    def sources(self) -> list[Source]:
        """Build the valid config/status sources.

        Config and status paths are matched by position. A config
        without `status-version 3` is skipped (it is mandatory and only
        one version is supported).
        """
        configs = env_list(CONFIG_PATHS_ENV)
        statuses = env_list(STATUS_PATHS_ENV)
        if configs and len(configs) != len(statuses):
            log.warning(
                "openvpn: %s has %d paths but %s has %d",
                CONFIG_PATHS_ENV,
                len(configs),
                STATUS_PATHS_ENV,
                len(statuses),
            )
        sources: list[Source] = []
        for index, status_path in enumerate(statuses):
            config_path = configs[index] if index < len(configs) else ""
            config_text = read_text(config_path) if config_path else None
            params = {} if config_text is None else parse_key_value(config_text)
            if not param_is(params, STATUS_VERSION_KEY, STATUS_VERSION):
                log.warning(
                    "openvpn: %r is not status-version 3, skipping",
                    config_path or status_path,
                )
                continue
            kind = (
                "server"
                if param_is(params, MODE_KEY, MODE_SERVER)
                else "client"
            )
            sources.append(
                Source(
                    network=path_stem(config_path) or path_stem(status_path),
                    params=params,
                    status_path=status_path,
                    kind=kind,
                )
            )
        return sources

    def parsed(self) -> list[ParsedSource]:
        """Every source with its parsed status data (None on failure)."""
        result: list[ParsedSource] = []
        for source in self.sources():
            text = read_text(source.status_path)
            data = _PARSERS[source.kind](text) if text is not None else None
            result.append((source, data))
        return result

    async def execute(self, metrics: list[Metric]) -> dict[str, SampleValue]:
        parsed = await asyncio.to_thread(self.parsed)
        return {m.name: COMMANDS[m.cmd](parsed) for m in metrics}
