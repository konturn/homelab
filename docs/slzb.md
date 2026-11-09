# SLZB-MR1 Zigbee coordinator

The SMLIGHT SLZB-MR1 (`zigbee_radio_address`, 10.6.128.20, IoT VLAN) is the
network-attached Zigbee coordinator for zigbee2mqtt (TCP 7638, the CC2652P7
radio). Its configuration is exported read-only to `networking/slzb/`:

| File | Purpose |
|---|---|
| `slzb-mr1.yaml` | Snapshot of the coordinator's settings and firmware versions (the baseline) |
| `slzbctl.py` | `export`, `drift`, `check` (Python stdlib only) |
| `test_slzbctl.py` | Offline unit tests |

Nothing here writes to the coordinator. `slzbctl.py` sends only `GET` requests
to the endpoints the web UI and the Home Assistant `smlight` integration read:
`/api2?action=0&page=N` (the UI's "get page" call; the values are in the
`respValuesArr` response header), `/ha_info` and `/ha_sensors`. It has no
apply. It never calls the save, restart, firmware update or radio flash
endpoints. The web UI and API need a login (HTTP basic auth). `slzbctl.py`
reads it from the file's `vault_path` (`homelab/infrastructure/slzb`,
`username` and `password`) at run time, or from `SLZB_USER` / `SLZB_PASSWORD`,
and stops at the first rejected login. The Home Assistant `smlight`
integration holds the same credentials. zigbee2mqtt uses the radio's TCP
socket (7638), which the login does not cover.

## What is captured

| Section | Source | Contents |
|---|---|---|
| `device` | `/ha_info` | Model, ESP32 firmware (`sw_version`), update channel, each radio's chip and firmware |
| `settings.status` | page 0 (dashboard) | Only the settings shown there: Zigbee auto-update, coordinator mode, USB mode, IPv6, Ethernet DHCP, radio modes, Wi-Fi mode/AP, VPN |
| `settings.mode` | page 1 | Coordinator mode, TCP port and baud per radio, Wi-Fi bridge |
| `settings.network` | page 2 | Ethernet and Wi-Fi addressing (DHCP, the static fields used only without DHCP) |
| `settings.serial` | page 3 | Per radio: baud, hardware flow control, packet mode, multi-client, serial mutex |
| `settings.security` | page 4 | Firewall IP. The web login and passwords are left out |
| `settings.vpn` | page 5 | WireGuard endpoint and addresses. Keys are left out |
| `settings.general` | page 6 | Hostname, anonymous statistics |
| `settings.auto_update` | page 7 | Automatic firmware check |
| `settings.night_mode` | page 8 | LED night-mode hours |
| `settings.time` | page 9 | Time zone (the selected POSIX TZ string), NTP servers, 12/24 h |
| `settings.syslog`, `mqtt`, `ddns` | pages 10, 17, 18 | Remote syslog, MQTT discovery, DDNS |
| `settings.zigbee` | page 15 | Router-mode channel and TX power, debug levels. PAN IDs are left out |
| `settings.sensors` | `/ha_sensors` | LEDs disabled, auto Zigbee, VPN enabled |

Any setting whose name contains `pass`, `key`, `secret`, `token`, `psk`,
`login`, `user`, `mac` or `panid` is never written. `check` (in CI) rejects a
file that holds one. Status values (uptime, temperatures, connection state,
current time) are not captured.

Values are kept as the UI reports them, including its placeholders such as
`[OFF]` and `[radio_ZC]`. Pages that return only the common status header
(11 to 14 and 16 on firmware v3.3.1) are skipped.

### Notes

- `time`: America/New_York (`EST5EDT,M3.2.0/2:00:00,M11.1.0/2:00:00`), NTP
  from the router (10.6.0.1, the IoT VLAN gateway; the coordinator is in
  `iot_no_wan` and has no internet). Set 2026-11-09.
- `general.sendStat` is on. It goes nowhere without internet.
- `auto_update.enabled` and the Zigbee auto-update (`status.ZbAutoUpd`) are off.
  Firmware updates are manual. After one, restart zigbee2mqtt
  (inventory/dependencies.yml, `zigbee2mqtt` rule).

## Usage

Run on the router (or anywhere that reaches the IoT VLAN), with a Vault token
in the environment:

```sh
cd networking/slzb
export VAULT_ADDR=https://vault.lab.nkontur.com:8200 VAULT_TOKEN=$(glab variable get VAULT_TOKEN)
python3 slzbctl.py export          # rewrite slzb-mr1.yaml from the coordinator
python3 slzbctl.py export -o -     # print instead of writing
python3 slzbctl.py drift           # exit 2 on drift
python3 slzbctl.py check           # offline: the file parses and holds nothing secret
```

`--host` (or `SLZB_HOST`) overrides the file's `host`.

After an intended change (a firmware update, a setting changed in the web UI),
run `export`, review `git diff`, and commit.

## Drift detection

`slzb:drift` runs on the same daily pipeline schedule as the camera check,
**"iot-devices: daily config drift"** (`SCHEDULED_TASK=iot-device-drift`), on
the protected runner. Its job network may reach the coordinator on :80
(docs/FIREWALL.md), and it logs in to Vault with the job's JWT (role `ci-deploy`).

| Result | Exit | Effect |
|---|---|---|
| No differences | 0 | Job passes |
| `DRIFT` (value changed, firmware included) or `NEW` (setting not in the file) | 2 | Job fails and mails the report to root (`Homelab: SLZB-MR1 coordinator config drift`) |
| Coordinator unreachable, login rejected, other error | 1 | Same mail |

`UNREAD` lines (in the file, not reported live) are shown but do not fail the
job. A firmware change shows up as `DRIFT device.firmware` or
`device.radio<n>`. Check that zigbee2mqtt still receives frames, then
`export` and commit. The report is kept as the `slzb-drift.txt` job artifact for 30
days.

`slzb:test` (MRs and main, on changes in `networking/slzb/`) runs the unit
tests and `slzbctl.py check`.
