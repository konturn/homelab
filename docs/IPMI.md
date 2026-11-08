# Router BMC (IPMI) as code

The router is a Supermicro X9DR3-F. Its BMC (`ipmi.lab.nkontur.com`,
10.4.128.7 on the management VLAN) is managed **in-band**: `ipmitool -I open`
on the router itself, through the kernel IPMI driver (`/dev/ipmi0`). In-band
access needs no BMC credentials and does not use the BMC's network port.

| | |
|---|---|
| Tool | `ipmictl` (`ansible/roles/ipmi/files/ipmictl.py`, installed as `/usr/local/sbin/ipmictl`) |
| Desired state | `ipmi_desired` in `ansible/roles/ipmi/defaults/main.yml`, rendered to `/etc/ipmictl/desired.yaml` |
| Metrics | `ipmi-metrics.timer` (every 2 min) → `/var/lib/ipmictl/bmc.influx` → Telegraf `[[inputs.file]]` → InfluxDB |
| Alerts | `bmc-*` rules in `hardware-alerts` (`docker/grafana/provisioning/alerting/infrastructure.yml`) |
| Drift check | CI `ipmi:drift`, daily schedule `SCHEDULED_TASK=ipmi-drift`; opens an `ipmi-drift` issue |
| Tests | CI `ipmi:test` (offline) |

A deploy installs the tool, the desired-state file and the metrics timer. It
never changes a BMC setting.

## Desired state

| Section | Settings | `apply` |
|---|---|---|
| `chassis` | `power_restore_policy` (from `ipmi_power_restore_policy`) | yes (`chassis policy`) |
| `fan` | `mode` (from `ipmi_fan_mode`: standard, full, optimal, heavyio) | yes (Supermicro raw `0x30 0x45 0x01`) |
| `sol` | enabled, privilege level, bit rates, accumulate/send/retry | yes (`sol set`) |
| `sel` | `logging_enabled` | yes (`mc setenables system_event_log=`) |
| `sensor_thresholds` | fan lower and temperature upper thresholds | yes (`sensor thresh`) |
| `lan` | IP source, address, netmask, gateway, VLAN, cipher suite 0 disabled | **check only** |
| `channel_access` | access mode, privilege limit (channel 1) | **check only** |

**Power restore policy** is the setting `docs/POWER.md` relies on: the server
must boot when the UPS restores its outlets after a power-fail shutdown.
`ipmi_power_restore_policy` in the role defaults is its source of truth
(`always-on`). On the X9DR3-F it is the same setting as BIOS "Restore on AC
Power Loss".

**LAN** is check-only, and the address must match the DHCP reservation (host
`ipmi` in `networking/dhcp/dhcpd.conf`, `ipmi_address` in the inventory). The
BMC gets its address by DHCP from the router. To change the address, change
the reservation; do not set a static address on the BMC from here.

## Commands (on the router, as root)

```bash
ipmictl check              # diff against /etc/ipmictl/desired.yaml; exit 2 on drift
ipmictl export             # live settings in the desired-state format
ipmictl plan               # ipmitool commands that would fix the drift (sends nothing)
ipmictl apply              # same as plan
ipmictl apply --execute    # sends the plan's writes, then checks again
ipmictl metrics --out -    # print the metrics instead of writing the file
ipmictl audit              # full read-only inventory for a person; print only, never commit
```

Exit codes: 0 ok, 1 error, 2 drift, 3 refused by a guard.

`apply --execute` refuses under CI and as non-root. Every write goes through
one allow list: `chassis policy`, the fan-mode raw command, `sol set`,
`sensor thresh` and `mc setenables system_event_log=`. It refuses anything
else: chassis power, reset or boot-device commands, `mc reset`, `sel clear`,
and LAN, user or channel changes.

To change a setting: edit the role defaults in an MR, merge (the deploy
updates `/etc/ipmictl/desired.yaml`), then on the router run `ipmictl plan`,
then `ipmictl apply --execute`. The next `ipmi:drift` run closes the issue.

## Drift check

`ipmi:drift` runs daily on `main` (protected runner). It SSHes to the router,
runs `ipmictl check --markdown`, and passes the result to
`ci/ipmi/drift_issue.py`:

- drift (exit 2): opens an issue labelled `ipmi-drift`, or adds a note to the
  open one when the report has changed. The job fails.
- no drift: notes and closes the open `ipmi-drift` issue, if there is one.
- any other exit: no issue change; the job fails.

It never applies anything. Run it by hand with Build > Pipelines > Run
pipeline on `main` and `SCHEDULED_TASK=ipmi-drift`.

## Metrics and alerts

`ipmictl metrics` writes InfluxDB line protocol. Each line carries its
collection time, so Telegraf reading the same file twice writes the same
points.

| Measurement | Tags | Fields |
|---|---|---|
| `bmc_sensor` | `sensor`, `type` (temperature, fan, voltage, psu, intrusion, discrete) | `value`, `status` (0 ok, 1 non-critical, 2 critical, 3 non-recoverable), fan `lower_critical`, temperature `upper_critical`; discrete: `state`, PSU `present`, `fault` |
| `bmc_chassis` | | `power_on`, `power_restore_policy`, `power_overload`, `main_power_fault`, `power_control_fault`, `cooling_fault`, `drive_fault`, `intrusion` |
| `bmc_sel` | | `entries`, `capacity`, `used_pct`, `overflow`, `logging_enabled`, `clock_set`, `new_<category>` and `<category>_total` for critical, ecc, warning, info; `collected_at` |
| `bmc_sel_event` | `category`, `sensor` | `event`, `direction`, `record`: one point per new SEL entry |
| `bmc_config` | | `check_ok`, `drift_items` (the same diff as `ipmictl check`) |

Sensors without a reading (empty DIMM slots, absent fan headers) are skipped.

SEL categories (asserted events only; deasserted events are `info`):

| Category | Matches |
|---|---|
| critical | critical or non-recoverable threshold crossings, uncorrectable ECC, PSU failure / predictive failure / AC lost, thermal trip, machine check |
| ecc | correctable ECC |
| warning | non-critical threshold crossings, chassis intrusion, watchdog |
| info | everything else (session audit, OEM records, deasserts) |

The first run records the current SEL as a baseline; only entries that appear
later count as new. New entries are also logged to the journal
(`journalctl -t ipmi-metrics`), at priority crit for critical ones. The SEL is
re-read only when its entry count or add/delete time changes.

| Alert | Fires when | Severity |
|---|---|---|
| `bmc-sel-critical` | a new critical SEL entry in the last 15 min | critical |
| `bmc-sel-ecc` | a new correctable ECC entry in the last hour | warning |
| `bmc-psu-fault` | a PSU sensor reports not present or a fault, for 2 min | critical |
| `bmc-fan-failure` | a fan is below its lower critical threshold, for 4 min | critical |
| `bmc-temp-critical` | a temperature is at or above its upper critical threshold, for 4 min | critical |
| `bmc-sel-full` | the SEL is more than 90 % full, for 1 h | warning |
| `bmc-metrics-stale` | no `bmc_sel` point for 15 min (or none in 24 h) | warning |

The existing `high-cpu-temps` rule (Telegraf `ipmi_sensor`) stays as it is.

## SEL

The X9 BMC keeps 512 SEL entries and does not wrap: when the log is full it
stops recording, so new events are lost and `bmc-sel-critical` cannot see
them. `bmc-sel-full` warns before that happens (it was shipped paused until
the old entries were cleared in November 2026, MR !998). Clearing is a manual
step:

```bash
ipmitool sel elist > /root/sel-$(date +%F).txt   # keep a copy first
ipmitool sel clear                               # by hand only; ipmictl never clears
```

The next metrics run sees the lower entry count and counts the new entries
(the "log cleared" record is `info`).

The BMC clock is not synchronised (`bmc_sel.clock_set` = 0), so SEL entries
carry "Pre-Init" relative timestamps. That does not affect the alerts, which
use the time each entry was first seen. TODO: set the time
(`ipmitool sel time set now`, in-band) after a decision on NTP in the BMC web
UI, then add it to the desired state.

## Not managed here yet (needs BMC credentials)

These are left out on purpose. Each one needs a login to the BMC to verify
before and after, or it risks locking out remote access:

- **BMC users and passwords.** Account changes, password rotation into Vault
  (`homelab/infrastructure/ipmi`), and removing unused accounts. Telegraf's
  `[[inputs.ipmi_sensor]]` and the JIT `ipmi` resource use these credentials.
- **RMCP+ cipher suites and IPMI 1.5 authentication types.** Review them
  together with the users, then verify a `lanplus` login with the credentials
  from Vault before and after.
- **SOL security options** (the force-encryption and force-authentication
  flags), for the same reason.
- **Telegraf `ipmi_sensor` in-band.** Moving that input to the local
  interface (with `use_sudo`) would remove the BMC password from
  `telegraf.conf`. It changes the `server` tag of the `ipmi_sensor` series,
  so the dashboards and `high-cpu-temps` need checking at the same time.
- **BMC network settings** (web UI services, NTP, SNMP) are not readable
  in-band with standard commands and stay manual.
