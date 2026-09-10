import asyncio
import json
import time

import pytest

from webapp.handlers.openvpn import (
    CONNECTED_CLIENTS,
    ROUTE_METRIC,
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


def server_text(separator):
    return (
        "\n".join(separator.join(line.split(",")) for line in SERVER_ROWS)
        + "\n"
    )


def expected_time():
    parsed = time.strptime("Tue Mar 21 10:39:09 2017", "%a %b %d %H:%M:%S %Y")
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
    assert f'sms_openvpn_up{{status_path="{path}"}} 1.0' in out
    assert (
        out.count(
            f'sms_openvpn_status_update_time_seconds{{status_path="{path}"}}'
        )
        == 1
    )
    assert f" {expected_time()}" in out
    assert (
        f'sms_openvpn_client_tun_tap_read_bytes_total{{status_path="{path}"}} '
        "153789941.0" in out
    )
    assert (
        f'sms_openvpn_client_auth_read_bytes_total{{status_path="{path}"}} '
        "308854782.0" in out
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
    assert f'sms_openvpn_up{{status_path="{path}"}} 1.0' in out
    assert (
        f'sms_openvpn_status_update_time_seconds{{status_path="{path}"}} '
        "1490089154.0" in out
    )
    assert (
        f'sms_openvpn_server_connected_clients{{status_path="{path}"}} 2.0'
        in out
    )
    assert (
        "sms_openvpn_server_client_received_bytes_total"
        f'{{status_path="{path}",common_name="alice",connection_time="1489680543",'
        f'real_address="10.0.0.1:19021",virtual_address="10.8.0.2",'
        'username="UNDEF"} 693438277.0' in out
    )
    assert (
        "sms_openvpn_server_client_sent_bytes_total"
        f'{{status_path="{path}",common_name="bob",connection_time="1489680537",'
        f'real_address="10.0.0.2:60536",virtual_address="10.8.0.3",'
        'username="user2"} 3145665.0' in out
    )
    assert (
        "sms_openvpn_server_route_last_reference_time_seconds"
        f'{{status_path="{path}",common_name="alice",'
        f'real_address="10.0.0.1:19021",virtual_address="10.8.0.2"}} '
        "1490088408.0" in out
    )


def test_missing_file_reports_up_zero(tmp_path):
    path = str(tmp_path / "does-not-exist.status")
    cfg = make_config(tmp_path, [UP], [path])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}"}} 0.0' in out


def test_malformed_file_reports_up_zero(tmp_path):
    path = write(tmp_path, "bad.status", "not an openvpn status file\n")
    cfg = make_config(tmp_path, [UP], [path])
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}"}} 0.0' in out


def test_non_numeric_metric_value_reports_up_zero(tmp_path):
    text = server_text(",").replace("693438277", "not-a-number")
    path = write(tmp_path, "bad.status", text)
    cfg = make_config(
        tmp_path, [UP, "sms_openvpn_server_client_received_bytes_total"], [path]
    )
    out = run_cycle(OpenVpnMetricHandler(cfg))
    assert f'sms_openvpn_up{{status_path="{path}"}} 0.0' in out
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
    assert f'sms_openvpn_up{{status_path="{p1}"}} 1.0' in out
    assert f'sms_openvpn_up{{status_path="{p2}"}} 1.0' in out


def test_verify_rejects_unknown_metric(tmp_path):
    path = write(tmp_path, "client.status", CLIENT_STATUS)
    cfg = make_config(tmp_path, ["sms_openvpn_bogus"], [path])
    handler = OpenVpnMetricHandler(cfg)
    metrics = asyncio.run(handler.read())
    with pytest.raises(ValueError, match="unknown openvpn metric"):
        asyncio.run(handler.verify(metrics))


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
