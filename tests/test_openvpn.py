import asyncio
import json
import time

import pytest

from webapp.handlers.openvpn import (
    CLIENT_UP,
    CONNECTED_CLIENTS,
    ROUTE_METRIC,
    SERVER_UP,
    STATUS_UPDATE_TIME,
    UP,
    OpenVpnMetricHandler,
)

CLIENT_STATUS = """OpenVPN STATISTICS
Updated,Tue Mar 21 10:39:09 2017
TUN/TAP read bytes,153789941
TUN/TAP write bytes,308764078
TCP/UDP read bytes,292806201
TCP/UDP write bytes,197558969
Auth read bytes,308854782
pre-compress bytes,45388190
post-compress bytes,45446864
pre-decompress bytes,162596168
post-decompress bytes,216965355
END
"""

SERVER_ROWS = [
    "TITLE,OpenVPN 2.3.2 test",
    "TIME,Tue Mar 21 10:39:14 2017,1490089154",
    (
        "HEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,"
        "Bytes Received,Bytes Sent,Connected Since,"
        "Connected Since (time_t),Username"
    ),
    (
        "CLIENT_LIST,alice,10.0.0.1:19021,10.8.0.2,693438277,228390856,"
        "Thu Mar 16 17:09:03 2017,1489680543,UNDEF"
    ),
    (
        "CLIENT_LIST,bob,10.0.0.2:60536,10.8.0.3,2925752,3145665,"
        "Thu Mar 16 17:08:57 2017,1489680537,user2"
    ),
    (
        "HEADER,ROUTING_TABLE,Virtual Address,Common Name,Real Address,"
        "Last Ref,Last Ref (time_t)"
    ),
    (
        "ROUTING_TABLE,10.8.0.2,alice,10.0.0.1:19021,"
        "Tue Mar 21 10:26:48 2017,1490088408"
    ),
    "GLOBAL_STATS,Max bcast/mcast queue length,0",
    "END",
]


SERVER_V1_STATUS = """OpenVPN CLIENT LIST
Updated,2026-09-10 21:51:57
Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since
barbossa,128.0.145.8:38072,1498074,1100680,2026-09-10 20:53:10
blackbeard,194.163.136.152:54191,1104293,1496111,2026-09-10 20:52:31
ROUTING TABLE
Virtual Address,Common Name,Real Address,Last Ref
5a:c9:06:2e:2e:2b@0,barbossa,128.0.145.8:38072,2026-09-10 21:51:52
d6:6c:59:45:36:72@0,blackbeard,194.163.136.152:54191,2026-09-10 21:51:52
GLOBAL STATS
Max bcast/mcast queue length,2
END
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def make_config(tmp_path, names, paths, timeout=5):
    metrics = [
        {
            "name": name,
            "help_text": "help",
            "value_type": "gauge",
            "cmd": " ".join(paths),
            "timeout": timeout,
        }
        for name in names
    ]
    p = tmp_path / "openvpn.json"
    p.write_text(json.dumps(metrics), encoding="utf-8")
    return str(p)


def run_cycle(handler):
    metrics = asyncio.run(handler.read())
    asyncio.run(handler.verify(metrics))
    results = asyncio.run(handler.execute(metrics))
    return asyncio.run(handler.finalize(metrics, results))


@pytest.fixture(autouse=True)
def _clear_status_path(monkeypatch):
    monkeypatch.delenv("OPENVPN_STATUS_PATH", raising=False)


def server_text(separator):
    return (
        "\n".join(separator.join(line.split(",")) for line in SERVER_ROWS)
        + "\n"
    )


def expected_time():
    parsed = time.strptime("Tue Mar 21 10:39:09 2017", "%a %b %d %H:%M:%S %Y")
    return repr(float(time.mktime(parsed)))


def iso_epoch(value):
    parsed = time.strptime(value, "%Y-%m-%d %H:%M:%S")
    return repr(float(time.mktime(parsed)))


def test_client_status(tmp_path):
    path = write(tmp_path, "client.status", CLIENT_STATUS)
    cfg = make_config(
        tmp_path,
        [
            UP,
            STATUS_UPDATE_TIME,
            "sms_openvpn_client_tun_tap_read_bytes_total",
            "sms_openvpn_client_auth_read_bytes_total",
        ],
        [path],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="client"}} 1.0' in out
    assert (
        out.count(
            "sms_openvpn_status_update_time_seconds"
            f'{{status_path="{path}",type="client"}}'
        )
        == 1
    )
    assert f" {expected_time()}" in out
    assert (
        "sms_openvpn_client_tun_tap_read_bytes_total"
        f'{{status_path="{path}",type="client"}} 153789941.0' in out
    )
    assert (
        "sms_openvpn_client_auth_read_bytes_total"
        f'{{status_path="{path}",type="client"}} 308854782.0' in out
    )


@pytest.mark.parametrize("separator", [",", "\t"])
def test_server_status(tmp_path, separator):
    path = write(tmp_path, "server.status", server_text(separator))
    cfg = make_config(
        tmp_path,
        [
            UP,
            STATUS_UPDATE_TIME,
            CONNECTED_CLIENTS,
            "sms_openvpn_server_client_received_bytes_total",
            "sms_openvpn_server_client_sent_bytes_total",
            ROUTE_METRIC,
        ],
        [path],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="server"}} 1.0' in out
    assert (
        "sms_openvpn_status_update_time_seconds"
        f'{{status_path="{path}",type="server"}} 1490089154.0' in out
    )
    assert (
        "sms_openvpn_server_connected_clients"
        f'{{status_path="{path}",type="server"}} 2.0' in out
    )
    assert (
        "sms_openvpn_server_client_received_bytes_total"
        f'{{status_path="{path}",type="server",common_name="alice",'
        'connection_time="1489680543",'
        f'real_address="10.0.0.1:19021",virtual_address="10.8.0.2",'
        'username="UNDEF"} 693438277.0' in out
    )
    assert (
        "sms_openvpn_server_client_sent_bytes_total"
        f'{{status_path="{path}",type="server",common_name="bob",'
        'connection_time="1489680537",'
        f'real_address="10.0.0.2:60536",virtual_address="10.8.0.3",'
        'username="user2"} 3145665.0' in out
    )
    assert (
        "sms_openvpn_server_route_last_reference_time_seconds"
        f'{{status_path="{path}",type="server",common_name="alice",'
        f'real_address="10.0.0.1:19021",virtual_address="10.8.0.2"}} '
        "1490088408.0" in out
    )


def test_server_status_v1(tmp_path):
    path = write(tmp_path, "server-v1.status", SERVER_V1_STATUS)
    cfg = make_config(
        tmp_path,
        [
            UP,
            STATUS_UPDATE_TIME,
            CONNECTED_CLIENTS,
            "sms_openvpn_server_client_received_bytes_total",
            "sms_openvpn_server_client_sent_bytes_total",
            ROUTE_METRIC,
        ],
        [path],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="server"}} 1.0' in out
    assert (
        "sms_openvpn_status_update_time_seconds"
        f'{{status_path="{path}",type="server"}} '
        f"{iso_epoch('2026-09-10 21:51:57')}" in out
    )
    assert (
        "sms_openvpn_server_connected_clients"
        f'{{status_path="{path}",type="server"}} 2.0' in out
    )
    assert (
        "sms_openvpn_server_client_received_bytes_total"
        f'{{status_path="{path}",type="server",common_name="barbossa",'
        f'connection_time="{iso_epoch("2026-09-10 20:53:10")}",'
        f'real_address="128.0.145.8:38072",virtual_address="",'
        'username=""} 1498074.0' in out
    )
    assert (
        "sms_openvpn_server_client_sent_bytes_total"
        f'{{status_path="{path}",type="server",common_name="blackbeard",'
        f'connection_time="{iso_epoch("2026-09-10 20:52:31")}",'
        f'real_address="194.163.136.152:54191",virtual_address="",'
        'username=""} 1496111.0' in out
    )
    assert (
        "sms_openvpn_server_route_last_reference_time_seconds"
        f'{{status_path="{path}",type="server",common_name="barbossa",'
        'real_address="128.0.145.8:38072",'
        f'virtual_address="5a:c9:06:2e:2e:2b@0"}} '
        f"{iso_epoch('2026-09-10 21:51:52')}" in out
    )


def test_empty_families_are_omitted(tmp_path):
    server = write(tmp_path, "server.status", server_text(","))
    client = write(tmp_path, "client.status", CLIENT_STATUS)
    cfg = make_config(
        tmp_path,
        [
            UP,
            CONNECTED_CLIENTS,
            "sms_openvpn_client_tun_tap_read_bytes_total",
            "sms_openvpn_server_client_received_bytes_total",
        ],
        [server],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert "sms_openvpn_client_tun_tap_read_bytes_total" not in out
    assert "sms_openvpn_server_client_received_bytes_total" in out

    cfg_client = make_config(
        tmp_path,
        [
            UP,
            CONNECTED_CLIENTS,
            "sms_openvpn_client_tun_tap_read_bytes_total",
            "sms_openvpn_server_client_received_bytes_total",
        ],
        [client],
    )
    out_client = run_cycle(OpenVpnMetricHandler(cfg_client))
    assert "sms_openvpn_client_tun_tap_read_bytes_total" in out_client
    assert "sms_openvpn_server_client_received_bytes_total" not in out_client
    assert "sms_openvpn_server_connected_clients" not in out_client


def test_missing_file_reports_up_zero(tmp_path):
    path = str(tmp_path / "does-not-exist.status")
    cfg = make_config(tmp_path, [UP], [path])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="unknown"}} 0.0' in out


def test_malformed_file_reports_up_zero(tmp_path):
    path = write(tmp_path, "bad.status", "not an openvpn status file\n")
    cfg = make_config(tmp_path, [UP], [path])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="unknown"}} 0.0' in out


def test_non_numeric_metric_value_reports_up_zero(tmp_path):
    text = server_text(",").replace("693438277", "not-a-number")
    path = write(tmp_path, "bad.status", text)
    cfg = make_config(
        tmp_path, [UP, "sms_openvpn_server_client_received_bytes_total"], [path]
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}",type="unknown"}} 0.0' in out
    assert "sms_openvpn_server_client_received_bytes_total{" not in out


def test_multiple_paths_are_labeled(tmp_path):
    p1 = write(tmp_path, "a.status", CLIENT_STATUS)
    p2 = write(tmp_path, "b.status", CLIENT_STATUS)
    cfg = make_config(
        tmp_path,
        [UP, "sms_openvpn_client_tun_tap_read_bytes_total"],
        [p1, p2],
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{p1}",type="client"}} 1.0' in out
    assert f'sms_openvpn_up{{status_path="{p2}",type="client"}} 1.0' in out


def test_server_and_client_up_distinguish_roles(tmp_path):
    server = write(tmp_path, "server.status", server_text(","))
    cfg = make_config(tmp_path, [SERVER_UP, CLIENT_UP], [server])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_server_up{{status_path="{server}"}} 1.0' in out
    assert f'sms_openvpn_client_up{{status_path="{server}"}} 0.0' in out

    client = write(tmp_path, "client.status", CLIENT_STATUS)
    cfg = make_config(tmp_path, [SERVER_UP, CLIENT_UP], [client])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_server_up{{status_path="{client}"}} 0.0' in out
    assert f'sms_openvpn_client_up{{status_path="{client}"}} 1.0' in out


def test_verify_rejects_unknown_metric(tmp_path):
    path = write(tmp_path, "client.status", CLIENT_STATUS)
    cfg = make_config(tmp_path, ["sms_openvpn_bogus"], [path])
    handler = OpenVpnMetricHandler(cfg)
    metrics = asyncio.run(handler.read())
    with pytest.raises(ValueError, match="unknown openvpn metric"):
        asyncio.run(handler.verify(metrics))


def test_env_override_uses_path(tmp_path, monkeypatch):
    real = write(tmp_path, "real.status", CLIENT_STATUS)
    missing = str(tmp_path / "missing.status")
    cfg = make_config(
        tmp_path,
        [UP, "sms_openvpn_client_tun_tap_read_bytes_total"],
        [missing],
    )
    monkeypatch.setenv("OPENVPN_STATUS_PATH", real)
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{real}",type="client"}} 1.0' in out
    assert missing not in out
    assert (
        "sms_openvpn_client_tun_tap_read_bytes_total"
        f'{{status_path="{real}",type="client"}} 153789941.0' in out
    )


def test_env_override_multiple_paths(tmp_path, monkeypatch):
    p1 = write(tmp_path, "a.status", CLIENT_STATUS)
    p2 = write(tmp_path, "b.status", CLIENT_STATUS)
    cfg = make_config(tmp_path, [UP], [str(tmp_path / "missing.status")])
    monkeypatch.setenv("OPENVPN_STATUS_PATH", f"{p1} {p2}")
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{p1}",type="client"}} 1.0' in out
    assert f'sms_openvpn_up{{status_path="{p2}",type="client"}} 1.0' in out


def test_duplicate_client_rows_keep_first(tmp_path):
    text = (
        "TITLE,x\n"
        "TIME,x,1\n"
        "HEADER,CLIENT_LIST,Common Name,Bytes Received,Bytes Sent,"
        "Connected Since (time_t),Real Address,Virtual Address,Username\n"
        "CLIENT_LIST,alice,10,1,111,addr,1.1.1.1,u\n"
        "CLIENT_LIST,alice,20,2,111,addr,1.1.1.1,u\n"
        "END\n"
    )
    path = write(tmp_path, "server.status", text)
    cfg = make_config(
        tmp_path, ["sms_openvpn_server_client_received_bytes_total"], [path]
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert "} 10.0" in out
    assert "} 20.0" not in out
