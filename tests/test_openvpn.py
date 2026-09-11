import asyncio
import json
import time

import pytest

from webapp.handlers.openvpn import (
    CONFIG_PATHS_ENV,
    STATUS_PATHS_ENV,
    OpenVpnMetricHandler,
    is_known_command,
    parse_client_status,
    parse_server_status,
)

SERVER_V3 = (
    "TITLE\tOpenVPN 2.6 test\n"
    "TIME\t2026-09-10 21:51:57\t1789077897\n"
    "HEADER\tCLIENT_LIST\tCommon Name\tReal Address\tVirtual Address\t"
    "Bytes Received\tBytes Sent\tConnected Since (time_t)\tUsername\n"
    "CLIENT_LIST\tbarbossa\t1.2.3.4:1234\t10.0.0.2\t100\t200\t"
    "1789000000\tUNDEF\n"
    "HEADER\tROUTING_TABLE\tVirtual Address\tCommon Name\t"
    "Real Address\tLast Ref (time_t)\n"
    "ROUTING_TABLE\t10.0.0.2\tbarbossa\t1.2.3.4:1234\t1789077887\n"
    "GLOBAL_STATS\tMax bcast/mcast queue length\t2\n"
    "END\n"
)

CLIENT_STATUS = """OpenVPN STATISTICS
Updated,Tue Mar 21 10:39:09 2017
TUN/TAP read bytes,153789941
END
"""

CLIENT_STATUS_ISO = """OpenVPN STATISTICS
Updated,2026-09-11 20:44:00
TUN/TAP read bytes,153789941
END
"""


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def make_config(tmp_path, commands):
    metrics = [
        {
            "name": f"sms_openvpn_test_{index}",
            "help_text": "help",
            "value_type": "gauge",
            "cmd": command,
            "timeout": 5,
        }
        for index, command in enumerate(commands)
    ]
    path = tmp_path / "openvpn.json"
    path.write_text(json.dumps(metrics), encoding="utf-8")
    return str(path)


def run_cycle(handler):
    metrics = asyncio.run(handler.read())
    asyncio.run(handler.verify(metrics))
    results = asyncio.run(handler.execute(metrics))
    return asyncio.run(handler.finalize(metrics, results))


def test_parse_server_status():
    data = parse_server_status(SERVER_V3)
    assert data.kind == "server"
    assert data.update_time == "1789077897"
    assert data.clients[0]["Common Name"] == "barbossa"
    assert data.clients[0]["Bytes Received"] == "100"
    assert data.routes[0]["Last Ref (time_t)"] == "1789077887"


def test_parse_client_status():
    data = parse_client_status(CLIENT_STATUS)
    assert data.kind == "client"
    assert data.update_time == "Tue Mar 21 10:39:09 2017"
    assert data.counters["TUN/TAP read bytes"] == "153789941"


def test_is_known_command():
    assert is_known_command("openvpn.up")
    assert not is_known_command("openvpn.nope")


def test_sources_requires_status_version_3(monkeypatch, tmp_path):
    config = write(
        tmp_path, "tortuga.conf", "mode server\nstatus-version 3\nport 1194\n"
    )
    status = write(tmp_path, "tortuga-status.log", SERVER_V3)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    sources = OpenVpnMetricHandler("unused.json").sources()
    assert len(sources) == 1
    assert sources[0].network == "tortuga"
    assert sources[0].kind == "server"
    assert sources[0].status_path == status


def test_sources_skips_non_v3(monkeypatch, tmp_path):
    config = write(tmp_path, "tortuga.conf", "mode server\nport 1194\n")
    status = write(tmp_path, "tortuga-status.log", SERVER_V3)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    assert OpenVpnMetricHandler("unused.json").sources() == []


def test_verify_rejects_unknown_command(tmp_path):
    cfg = make_config(tmp_path, ["openvpn.bogus"])
    handler = OpenVpnMetricHandler(cfg)
    metrics = asyncio.run(handler.read())
    with pytest.raises(ValueError, match="unknown openvpn"):
        asyncio.run(handler.verify(metrics))


def test_execute_server_metrics(monkeypatch, tmp_path):
    config = write(tmp_path, "tortuga.conf", "mode server\nstatus-version 3\n")
    status = write(tmp_path, "tortuga-status.log", SERVER_V3)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    cfg = make_config(
        tmp_path,
        [
            "openvpn.up",
            "openvpn.status_path",
            "openvpn.server.connected_clients",
            "openvpn.server.client_received_bytes",
        ],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert 'network="tortuga",type="server"' in out
    assert "} 1.0" in out
    assert (
        'common_name="barbossa",connection_time="1789000000",'
        'real_address="1.2.3.4:1234",virtual_address="10.0.0.2",'
        'username="UNDEF"} 100.0' in out
    )


def test_execute_client_counter(monkeypatch, tmp_path):
    config = write(tmp_path, "client.conf", "status-version 3\n")
    status = write(tmp_path, "client-status.log", CLIENT_STATUS)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    cfg = make_config(
        tmp_path,
        ["openvpn.up", "openvpn.client.tun_tap_read_bytes"],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert (
        'sms_openvpn_test_1{network="client",type="client"} 153789941.0' in out
    )


def test_execute_client_iso_update_time(monkeypatch, tmp_path):
    config = write(tmp_path, "client.conf", "status-version 3\n")
    status = write(tmp_path, "client-status.log", CLIENT_STATUS_ISO)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    cfg = make_config(tmp_path, ["openvpn.status_update_time"])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    parsed = time.strptime("2026-09-11 20:44:00", "%Y-%m-%d %H:%M:%S")
    expected = repr(float(time.mktime(parsed)))
    assert (
        f'sms_openvpn_test_0{{network="client",type="client"}} {expected}'
        in out
    )


CONFIG_TEXT = """dev tap-tortuga
proto udp
port 1194
mode server
ifconfig-pool 10.23.43.10 10.23.43.100 255.255.255.0
keepalive 10 120
client-to-client
duplicate-cn
persist-key
persist-tun
data-ciphers AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305
auth SHA256
tls-version-min 1.2
status-version 3
status /var/log/tortuga-status.log
"""


def test_execute_config_metrics(monkeypatch, tmp_path):
    config = write(tmp_path, "tortuga.conf", CONFIG_TEXT)
    status = write(tmp_path, "tortuga-status.log", SERVER_V3)
    monkeypatch.setenv(CONFIG_PATHS_ENV, config)
    monkeypatch.setenv(STATUS_PATHS_ENV, status)
    cfg = make_config(
        tmp_path,
        [
            "openvpn.config.port",
            "openvpn.config.pool_size",
            "openvpn.config.keepalive_interval",
            "openvpn.config.keepalive_timeout",
            "openvpn.config.client_to_client",
            "openvpn.config.info",
            "openvpn.server.max_bcast_mcast_queue_length",
        ],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    prefix = 'network="tortuga",type="server"'
    assert f"sms_openvpn_test_0{{{prefix}}} 1194.0" in out
    assert f"sms_openvpn_test_1{{{prefix}}} 91.0" in out
    assert f"sms_openvpn_test_2{{{prefix}}} 10.0" in out
    assert f"sms_openvpn_test_3{{{prefix}}} 120.0" in out
    assert f"sms_openvpn_test_4{{{prefix}}} 1.0" in out
    assert (
        "sms_openvpn_test_5{"
        f'{prefix},proto="udp",dev="tap-tortuga",auth="SHA256",'
        'cipher="AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305",'
        'tls_version_min="1.2"} 1.0' in out
    )
    assert f"sms_openvpn_test_6{{{prefix}}} 2.0" in out
