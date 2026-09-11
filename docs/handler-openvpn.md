# Handler: openvpn

`OpenVpnMetricHandler` (`webapp/handlers/openvpn.py`, config
`data/config/openvpn.json`) reads OpenVPN `--status` files and maps
each config entry to an `openvpn.*` command that extracts one value.
See [handlers.md](handlers.md) for the handler model and how to add
your own.

## How it works

Two environment variables provide the inputs, as arrays of paths
(JSON array, CSV, bracketed list or space-separated):

| Variable | Meaning |
|----------|---------|
| `OPENVPN_CONFIG_PATH` | OpenVPN config file paths (one per network) |
| `OPENVPN_STATUS_PATH` | Status file paths, paired with the config paths **by position** |

For each pair the handler:

1. reads the config and parses it into key/values;
2. requires `status-version 3` — a config without it is skipped with a
   warning (only this version is supported);
3. treats `mode server` as a server, anything else as a client;
4. reads the status file listed in `OPENVPN_STATUS_PATH` (the
   `status` directive inside the config is **not** consulted);
5. sets the `network` label to the config file name without extension
   (`.../tortuga.conf` → `tortuga`).

Server status files are tab-separated (`--status-version 3`); client
status files use the classic `OpenVPN STATISTICS` key/value format.
A missing or malformed status file reports `sms_openvpn_up` `0.0` and
yields no other samples for that source (a warning is logged); the
cycle still succeeds.

All data metrics carry `network` and `type` (`client`/`server`); the
`sms_openvpn_up` metric additionally carries `common_name`, the CN of a
client config's embedded certificate (empty when none is present). The
status file path is exposed only by the dedicated
`sms_openvpn_status_path` metric.

## Command reference

### Status health

| Command | Default Metric Name | Type | Meaning |
|---------|--------|------|---------|
| `openvpn.up` | `sms_openvpn_up` | gauge | `1.0` when the status file parsed, else `0.0`; labels `network,type,common_name` (the CN of the config's embedded `<cert>`, empty when absent) |
| `openvpn.status_path` | `sms_openvpn_status_path` | gauge | `1.0`/`0.0` per status file; labels `status_path,network,type` |
| `openvpn.status_update_time` | `sms_openvpn_status_update_time_seconds` | gauge | UNIX timestamp of the last status update; labels `network,type` |

### Server status

| Command | Default Metric Name | Type | Meaning |
|---------|--------|------|---------|
| `openvpn.server.connected_clients` | `sms_openvpn_server_connected_clients` | gauge | number of `CLIENT_LIST` rows; labels `network,type` |
| `openvpn.server.client_received_bytes` | `sms_openvpn_server_client_received_bytes_total` | counter | per-client `Bytes Received`; adds labels `common_name,connection_time,real_address,virtual_address,username` |
| `openvpn.server.client_sent_bytes` | `sms_openvpn_server_client_sent_bytes_total` | counter | per-client `Bytes Sent`; same labels as above |
| `openvpn.server.route_last_reference` | `sms_openvpn_server_route_last_reference_time_seconds` | gauge | per-route `Last Ref (time_t)`; labels `network,type,common_name,real_address,virtual_address` |
| `openvpn.server.max_bcast_mcast_queue_length` | `sms_openvpn_server_max_bcast_mcast_queue_length` | gauge | `GLOBAL_STATS` "Max bcast/mcast queue length"; labels `network,type` |

### Client status

The status file's `OpenVPN STATISTICS` counters (labels `network,type`):

| Command | Default Metric Name | Type | Source line |
|---------|--------|------|-------------|
| `openvpn.client.tun_tap_read_bytes` | `sms_openvpn_client_tun_tap_read_bytes_total` | counter | `TUN/TAP read bytes` |
| `openvpn.client.tun_tap_write_bytes` | `sms_openvpn_client_tun_tap_write_bytes_total` | counter | `TUN/TAP write bytes` |
| `openvpn.client.tcp_udp_read_bytes` | `sms_openvpn_client_tcp_udp_read_bytes_total` | counter | `TCP/UDP read bytes` |
| `openvpn.client.tcp_udp_write_bytes` | `sms_openvpn_client_tcp_udp_write_bytes_total` | counter | `TCP/UDP write bytes` |
| `openvpn.client.auth_bytes` | `sms_openvpn_client_auth_bytes_total` | counter | `Auth read bytes` (OpenVPN only reports the read side) |
| `openvpn.client.pre_compress_bytes` | `sms_openvpn_client_pre_compress_bytes_total` | counter | `pre-compress bytes` |
| `openvpn.client.post_compress_bytes` | `sms_openvpn_client_post_compress_bytes_total` | counter | `post-compress bytes` |
| `openvpn.client.pre_decompress_bytes` | `sms_openvpn_client_pre_decompress_bytes_total` | counter | `pre-decompress bytes` |
| `openvpn.client.post_decompress_bytes` | `sms_openvpn_client_post_decompress_bytes_total` | counter | `post-decompress bytes` |

### Server config

Static values from the config file; all carry `network,type`.

| Command | Default Metric Name | Type | Meaning |
|---------|--------|------|---------|
| `openvpn.config.info` | `sms_openvpn_server_info` | gauge | always `1.0`, with `proto`, `dev`, `auth`, `cipher` and `tls_version_min` labels from the config |
| `openvpn.config.port` | `sms_openvpn_server_port` | gauge | `port` |
| `openvpn.config.verb` | `sms_openvpn_server_verb` | gauge | `verb` |
| `openvpn.config.script_security` | `sms_openvpn_server_script_security` | gauge | `script-security` |
| `openvpn.config.keepalive_interval` | `sms_openvpn_server_keepalive_interval_seconds` | gauge | first `keepalive` value |
| `openvpn.config.keepalive_timeout` | `sms_openvpn_server_keepalive_timeout_seconds` | gauge | second `keepalive` value |
| `openvpn.config.pool_size` | `sms_openvpn_server_pool_size` | gauge | number of addresses in `ifconfig-pool` |
| `openvpn.config.client_to_client` | `sms_openvpn_server_client_to_client` | gauge | `1.0` if `client-to-client` is set, else `0.0` |
| `openvpn.config.duplicate_cn` | `sms_openvpn_server_duplicate_cn` | gauge | `1.0` if `duplicate-cn` is set, else `0.0` |
| `openvpn.config.persist_key` | `sms_openvpn_server_persist_key` | gauge | `1.0` if `persist-key` is set, else `0.0` |
| `openvpn.config.persist_tun` | `sms_openvpn_server_persist_tun` | gauge | `1.0` if `persist-tun` is set, else `0.0` |

## Running in a container

Process discovery is intentionally absent — point the handler at the
config and status files explicitly:

```bash
OPENVPN_CONFIG_PATH='/etc/openvpn/tortuga.conf' \
OPENVPN_STATUS_PATH='/status/tortuga-status.log' \
    just start
```

With several networks (config and status paired by position):

```bash
OPENVPN_CONFIG_PATH='/etc/openvpn/tortuga.conf,/etc/openvpn/client.conf' \
OPENVPN_STATUS_PATH='/status/tortuga-status.log,/status/client-status.log' \
    just start
```

For a systemd service, set both variables in the unit's environment
(e.g. `Environment=OPENVPN_CONFIG_PATH=...` and
`Environment=OPENVPN_STATUS_PATH=...`).

## Customizing

Edit `data/config/openvpn.json`: drop metrics you do not want, rename
them, or add several metrics using the same command (the status file
is parsed once per cycle). The metric `cmd` must be one of the
`openvpn.*` commands above — unknown commands are rejected by
`verify()`.
