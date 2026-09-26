# Handler: amdgpu

`AmdGpuMetricHandler` (`webapp/handlers/amdgpu.py`, config
`data/config/amdgpu.json`) collects AMD GPU metrics by running
`amd-smi monitor --json`. See [handlers.md](handlers.md) for the
handler model and how to add your own.

## How it works

One amd-smi invocation per scrape cycle serves all configured
metrics; its timeout is the max of the requesting metrics' `timeout`
values. The JSON output is parsed into one entry per GPU, and each
configured metric's `cmd` names an `amdgpu.*` command that extracts
one field. Every sample carries a `gpu` label (the GPU id reported by
amd-smi, row position as fallback), so multi-GPU hosts export one
series per card.

- A field reported as `N/A` (e.g. encoder/decoder utilization without
  video sessions, PCIe bandwidth without a link measurement) yields
  **no sample** for that GPU — no zero is fabricated.
- A failed amd-smi run (missing binary, non-zero exit, timeout,
  unparseable output) is tolerated: `sms_amdgpu_up` reports `0.0`
  (label `gpu=""`), the other metrics yield no samples, a warning is
  logged and the cycle succeeds — the rest of `/metrics` keeps
  updating.
- `amdgpu.throttled` maps the throttle status: `0.0` when amd-smi
  reports `UNTHROTTLED`, `1.0` for any other state. A missing field
  yields no sample.

## Command reference

| Command | JSON field | Default Metric Name | Type | Meaning |
|---------|------------|---------------------|------|---------|
| `amdgpu.up` | — | `sms_amdgpu_up` | gauge | `1.0` when amd-smi data parsed for the GPU, `0.0` after a failed run |
| `amdgpu.power_usage` | `power_usage` | `sms_amdgpu_power_usage_watts` | gauge | power draw in watts |
| `amdgpu.hotspot_temperature` | `hotspot_temperature` | `sms_amdgpu_hotspot_temperature_celsius` | gauge | GPU hotspot temperature in celsius |
| `amdgpu.memory_temperature` | `memory_temperature` | `sms_amdgpu_memory_temperature_celsius` | gauge | GPU memory temperature in celsius |
| `amdgpu.gfx_utilization` | `gfx` | `sms_amdgpu_gfx_utilization_percent` | gauge | graphics utilization in percent |
| `amdgpu.gfx_clock` | `gfx_clock` | `sms_amdgpu_gfx_clock_mhz` | gauge | graphics clock in MHz |
| `amdgpu.mem_utilization` | `mem` | `sms_amdgpu_mem_utilization_percent` | gauge | memory utilization in percent |
| `amdgpu.mem_clock` | `mem_clock` | `sms_amdgpu_mem_clock_mhz` | gauge | memory clock in MHz |
| `amdgpu.encoder_utilization` | `encoder` | `sms_amdgpu_encoder_utilization_percent` | gauge | encoder utilization in percent |
| `amdgpu.encoder_clock` | `encoder_clock` | `sms_amdgpu_encoder_clock_mhz` | gauge | encoder clock in MHz |
| `amdgpu.decoder_utilization` | `decoder` | `sms_amdgpu_decoder_utilization_percent` | gauge | decoder utilization in percent |
| `amdgpu.decoder_clock` | `decoder_clock` | `sms_amdgpu_decoder_clock_mhz` | gauge | decoder clock in MHz |
| `amdgpu.throttled` | `throttle_status` | `sms_amdgpu_throttled` | gauge | `1.0` for any throttle state other than `UNTHROTTLED` |
| `amdgpu.single_bit_ecc` | `single_bit_ecc` | `sms_amdgpu_ecc_single_bit_total` | counter | single-bit ECC error count |
| `amdgpu.double_bit_ecc` | `double_bit_ecc` | `sms_amdgpu_ecc_double_bit_total` | counter | double-bit ECC error count |
| `amdgpu.pcie_replay` | `pcie_replay` | `sms_amdgpu_pcie_replay_total` | counter | PCIe replay counter |
| `amdgpu.vram_used` | `vram_used` | `sms_amdgpu_vram_used_megabytes` | gauge | VRAM used in MB |
| `amdgpu.vram_total` | `vram_total` | `sms_amdgpu_vram_total_megabytes` | gauge | VRAM total in MB |
| `amdgpu.pcie_bandwidth` | `pcie_bw` | `sms_amdgpu_pcie_bandwidth_megabits_per_second` | gauge | PCIe bandwidth in Mb/s |

## Customizing

Edit `data/config/amdgpu.json`: drop metrics you do not want, rename
them, or add several metrics using the same command (amd-smi runs
once per cycle regardless). The metric `cmd` must be one of the
`amdgpu.*` commands above — unknown commands are rejected by
`verify()`. The handler activates only when the config file exists;
remove `data/config/amdgpu.json` (or don't create it) to disable the
handler on hosts without an AMD GPU.
