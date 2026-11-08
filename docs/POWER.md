# Power: UPS, outage behaviour and tests

The router (and with it every service in this repo) runs on an APC Smart-UPS
1500 with an AP9631 network management card. NUT on the router watches the UPS
over SNMP and shuts the router down cleanly on a long outage. When mains returns,
the UPS turns its outlets back on and the server powers up by itself.

| | |
|---|---|
| UPS | APC Smart-UPS 1500, `ups.mfr.date` 01/06/2016, UPS firmware `UPS 09.3 (ID18)` |
| Card | AP9631 (NMC2) at `ups.lab.nkontur.com` / 10.4.128.8 (mgmt VLAN), AOS 6.8.2, Smart-UPS app (sumx) 6.8.0 |
| Monitoring | Telegraf SNMPv3 → InfluxDB `snmp` measurement → Grafana dashboard **UPS** and the `hardware-alerts` rules |
| Shutdown control | NUT 2.8.1 on the router: `snmp-ups` driver (device `apc`), `upsd`, `upsmon` (primary), `upssched` |
| Config | `base/nut/` (deployed by `configure-homelab-services`), settings in `ansible/roles/configure-homelab-services/defaults/main.yml` |

## What is on the UPS

| Device | On UPS | Notes |
|---|---|---|
| Router / server (Supermicro X9DR3-F, 10.4.0.1) incl. its IPMI BMC | yes | All containers, Vault, DNS (Pi-hole), GitLab, the `excel-vm` VM |
| Cable modem | yes | Internet keeps working until the router shuts down |
| Aruba S2500 core switch | yes | Wired LAN keeps working until the router shuts down |
| NETGEAR switches (movie, office) | **probably not** | They PoE-power the Aruba APs |
| Aruba APs | **no** (PoE from the NETGEAR switches) | **Wi-Fi drops the moment mains fails** |
| Raspberry Pis (zwave, satellite-2), Mac mini | probably not | Not NUT secondaries (see below) |

Confirm the NETGEAR/AP row during the pull-the-plug test (step 3 below): if Wi-Fi
drops at t=0, the switches are not on the UPS.

**Recommendation (Wi-Fi):** Wi-Fi only matters during an outage for as long as
the router is up, which by design is about 5 minutes plus the shutdown. Two
options:

1. Accept it (the cheapest option). Wired devices and the modem ride through
   short blips; Wi-Fi does not.
2. Put the NETGEAR switch that powers the main AP (office) on the UPS. This
   adds roughly 30-60 W (switch plus PoE), which costs a few minutes of runtime
   at today's load. It makes Wi-Fi survive short blips, which are the common
   case. Do this together with the battery replacement.

## Expected runtime

At the time of writing: load 36-39 % (about 360-390 W), charge 100 %, the
UPS estimates **31-35 minutes** (`battery.runtime` 1860-2100 s).
`battery.runtime.low` is 120 s (the UPS raises LOWBATT with 2 minutes left).

That estimate is the UPS's model of a battery of **unknown age** that has had
**no successful self-test on record since 2017** (see Battery health). Treat it
as optimistic until the self-test in step 1 below has passed.

## Shutdown sequence (power-fail)

```
t = 0        mains fails; UPS on battery (OB). upsmon: ONBATT
             -> notify.sh (syslog nut-notify, webhook) -> upssched:
                timers onbatt-confirm (30 s) and onbatt-shutdown (5 min)
             Grafana: "UPS On Battery" after 1 min
t = 30 s     onbatt-confirm fires: ups-power launches the runtime watch
             (polls every 10 s; exits as soon as the UPS reports OL)
t = 5 min    onbatt-shutdown fires                 (ups_onbatt_shutdown_minutes)
  or earlier the watch sees battery.runtime < 600 s for 3 polls in a row
             (ups_runtime_shutdown_seconds), whichever comes first
             -> ups-power trigger: reads ups.status twice, 5 s apart; only if
                both say OB (and neither OL, and no FSD yet) -> upsmon -c fsd
backstop     upsmon's own critical state: OB + LOWBATT (2 min left) -> same path
upsmon       sets FSD, waits HOSTSYNC (15 s) for secondaries, writes
             /etc/killpower (POWERDOWNFLAG), FINALDELAY 5 s, runs SHUTDOWNCMD:
ups-orderly-shutdown --execute       (log: /var/log/ups-shutdown.log, syslog ups-shutdown)
  1. libvirt VMs (excel-vm): virsh shutdown, wait up to 90 s, then virsh destroy
  2. docker compose stop -t 30 (main project), at most 150 s overall
  3. sync
  4. /sbin/shutdown -h +0
             When battery.runtime is already < 300 s (LOWBATT path), the
             timeouts are cut to 30 s / 10 s / 45 s.
systemd      normal shutdown: remaining services, libvirt-guests, ZFS pool
             export (mpool), LUKS close, root fs read-only
nutshutdown  /lib/systemd/system-shutdown/nutshutdown: upsmon -K sees
             /etc/killpower -> upsdrvctl shutdown -> snmp-ups sends
             shutdown.return (PowerNet upsBasicControlConserveBattery):
             the UPS turns its outlets off and back on when mains returns.
             Then it sleeps POWEROFF_WAIT (10 min) and reboots.
```

A worst-case shutdown takes about 10 minutes from t=0, about a third of the
estimated runtime.

**Why the driver uses the card's IP:** `ups.lab.nkontur.com` is served only by
Pi-hole, a container. By the time `nutshutdown` runs, every container is gone,
so `ups.conf` uses `{{ ups_address }}`.

### Guards: nothing fires on line power or on a blip

- The timers are started only by ONBATT and cancelled by ONLINE. `upssched.conf`
  acts on nothing else, and a unit test checks that every timer it starts is
  cancelled on ONLINE.
- Every action re-reads `ups.status` twice, 5 s apart, and does nothing unless
  both reads say OB and neither says OL. An `OL` status, an unreadable status,
  or FSD already set means the request is logged and ignored.
- An SNMP blip that reports OB for a moment either cancels the timers
  (ONLINE) or fails the double read. The runtime path also needs 3 consecutive
  low readings.
- `ups-orderly-shutdown` does nothing without `--execute`, and `DRY_RUN=1`
  overrides `--execute`.
- The killpower path runs only after a UPS-initiated shutdown. Only upsmon's
  power-fail path writes `/etc/killpower`, and upsmon removes the flag when it
  starts, so a normal `reboot` or `shutdown` never touches the UPS. The PowerNet
  MIB also documents that the card rejects `upsBasicControlConserveBattery`
  (badValue) unless the UPS is on battery.

### Settings

`ansible/roles/configure-homelab-services/defaults/main.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `ups_onbatt_shutdown_minutes` | 5 | forced shutdown after this long on battery |
| `ups_runtime_shutdown_seconds` | 600 | ... or when `battery.runtime` stays below this |
| `ups_onbatt_confirm_seconds` | 30 | on battery this long before the runtime watch starts |
| `ups_poweroff_wait` | 10m | `nutshutdown` reboots after this if the UPS did not cut power |
| `ups_selftest_enabled` | false | monthly self-test timer (Battery health) |
| `ups_battery_install_date` | "" | `MM/DD/YYYY` when known |

### Testing the logic without triggering it

```bash
python3 -m unittest discover -s base/nut -p 'test_*.py' -v   # also in CI (ups:test)
ups-power status                      # what the guards see now (read-only)
DRY_RUN=1 ups-power trigger test      # logs why it would / would not request FSD
ups-orderly-shutdown                  # no --execute: prints the plan only
```

## Restart behaviour (return of power)

1. **UPS:** `shutdown.return` puts it into "sleep", turning its outlets off
   after its shutdown delay. When mains returns, it turns them back on after
   its return delay, subject to its minimum-return-runtime setting. On this
   SMT-family unit these delays are per outlet group, not the
   `ups.delay.shutdown` / `ups.delay.start` variables. NUT 2.8.1's `apcc` MIB
   maps those to `upsAdvConfigShutoffDelay` / `upsAdvConfigReturnDelay`, which
   this card does not have, so `upsrw` does not list them and nothing was
   written. Values read over SNMP (`upsOutletGroupConfigTable`):

   | Group | Name | Columns 3 / 4 / 5 / 6 (power-on delay / power-off delay / reboot duration / min return runtime) |
   |---|---|---|
   | 1 | UPS Outlets | 0 / 0 / 8 / 0 |
   | 2 | Outlet Group 1 | 0 / 90 / 8 / 0 |

   Check these in the card UI (UPS > Configuration > Outlet Groups) and note
   which devices are on which group. Recommended, set by hand on the card later
   and then captured by `apcctl.py export --write`: power-off delay 60-90 s on
   the group that feeds the server (so the host has finished halting), power-on
   delay 0-30 s, and min return runtime 0 or a small value, so the server
   restarts on a partly charged battery.
2. **Server:** the BMC's power-restore policy is **`always-on`** (it was
   `always-off` until 2026-11-06; set with `ipmitool chassis policy always-on`,
   which on the X9DR3-F is the same setting as BIOS "Restore on AC Power
   Loss"). The server boots when the UPS restores its outlets.
   Source of truth: `ipmi_power_restore_policy` in
   `ansible/roles/ipmi/defaults/main.yml`. The daily `ipmi:drift` job opens an
   issue if the BMC ever differs ([IPMI.md](IPMI.md)).
   Check: `ipmictl check` or `ipmitool chassis status | grep Restore`.
3. **Boot stops at the root LUKS passphrase.** The root volume
   (`sda5_crypt`, in `/etc/crypttab` with key file `none`) has no automatic
   unlock: no keyfile, clevis/tang, TPM, or dropbear-initramfs. After every
   power cycle the server waits at the initramfs prompt until someone types the
   passphrase, at the console or over the IPMI KVM (10.4.128.7, reachable only
   from the LAN while the router is down). The pool disks (`dev*`,
   `persistent_data`) unlock from `/etc/password_file.txt` on the root fs, so
   they follow automatically. **So today "automatic restart" ends at the
   passphrase prompt; services return only once someone at home unlocks it.**
   Options, none of them done here because each needs a reboot to verify:
   dropbear-initramfs (unlock over SSH from the LAN), or clevis with a tang
   server on another always-on host (unattended unlock while that host is
   reachable).
4. **Power race:** if mains returns *during* the shutdown, the UPS ignores
   `shutdown.return` (it only accepts it on battery), so it never cycles
   the outlets. `nutshutdown` then sleeps `POWEROFF_WAIT` (10 min) and reboots
   the host instead of leaving it halted with power present. The host then
   waits at the LUKS prompt (item 3).
5. **If the command never reaches the card** while still on battery (for
   example the mgmt VLAN path is down), the host reboots after 10 minutes on
   battery and waits at the LUKS prompt, still drawing power, until the battery
   is flat. When mains returns, the UPS powers up and the server boots
   (`always-on`) to the same prompt.

## Battery health

**The battery's age is unknown, and the battery should be considered due
for replacement.**

- The card's battery date (`battery.date`, `upsBasicBatteryLastReplaceDate`)
  reads **12/15/2026**, which is after today, so it is wrong. Most likely it was
  typed in wrong, possibly meant as 12/15/2016. The card's recommended
  replace date of 03/24/2031 is derived from it and is wrong too.
- **Last self-test: 04/19/2017, result InvalidTest.** The card reports a
  self-test schedule (`upsAdvTestDiagnosticSchedule` = 8, one of the
  "since last test" intervals), yet nothing has been recorded since 2017. So
  automatic self-tests are either not running or not being recorded. Check
  UPS > Diagnostics in the card UI.
- The UPS reports its replacement cartridge as **APCRBC133** (PowerNet
  `upsAdvBattery` part-number field). RBC133 is the cartridge for the 2U
  rack-mount SMT1500RM2U family. The tower SMT1500 / SUA1500 use **RBC7**.
  Check the model on the rear label before ordering; the UPS's own answer
  (RBC133) is the one to trust. Battery voltage is 27.1 V, a 24 V pack.
- `battery_replace_indicator` (upsAdvBatteryReplaceIndicator) says "no",
  which is meaningless without a valid self-test.

**Recommendation:** order an APCRBC133 (OEM or a reputable equivalent) and
replace the cartridge. The SMT is hot-swappable, but do it with Noah home,
following APC's procedure. Afterwards set the battery date on the card (UPS >
Configuration > Battery date) and `ups_battery_install_date` in the role
defaults, then run the self-test (step 1 below).

**What watches it:**

| Alert (`hardware-alerts`) | Fires when | State |
|---|---|---|
| `ups-selftest-failed` | `selftest_result` = 2 (failed) | active |
| `ups-selftest-stale` | no passing self-test in 45 days (`selftest_stale`) | **paused** (`isPaused: true`); it would fire immediately today. Unpause after step 1. |
| `ups-battery-old` | `battery_age_days` > 1461 (4 years); unknown (-1) does not fire | active, silent until an install date is known |
| `ups-replace-battery` | the UPS says "batteryNeedsReplacing" | active |

The fields come from PowerNet-MIB OIDs in `docker/telegraf/telegraf.conf`
plus a Starlark processor there: `selftest_age_days`, `selftest_stale`,
`battery_age_days`, and `battery_age_known`. The Grafana dashboard **UPS**
(`docker/grafana/provisioning/dashboards/json/ups.json`) shows them.

**Monthly self-test (shipped disabled).** `ups-selftest.timer` (first Sunday of
the month, 10:30) runs `ups-power selftest`. It refuses unless `ups.status` is
exactly `OL` and the charge is at least 95 %, and then sends
`test.battery.start` as NUT user `selftest`. That user may run only that
command, and it exists only while the self-test is enabled. This UPS does not
offer `test.battery.start.quick`; APC's `test.battery.start` is the short
(about 10 s) self-test. To enable, **after step 1 below has passed**:

1. Vault: add field `selftest_password` to `homelab/infrastructure/nut`.
2. Set `ups_selftest_enabled: true` in the role defaults.
3. Set `isPaused: false` on `ups-selftest-stale`, then merge.

## AP9631 card as code (`networking/ups/`)

`apcctl.py` downloads the card's `config.ini` over SCP. The card has SSH on;
FTP is off. It removes secrets (`<redacted>`) and volatile values, and compares
the result with `networking/ups/apc-config.ini`. CI runs `ups-card:drift`
daily (schedule `SCHEDULED_TASK=ups-card-drift`) and mails root on drift.
`ups:test` runs the offline tests on MRs.

**State: live since 2026-11-07 (#93).** The card login is in Vault at
`homelab/infrastructure/ups-card` (`username`, `password`; the `apc` super
user, rotated to a random 24-character password). `networking/ups/known_hosts`
pins the card's ssh-rsa host key, and `apc-config.ini` is the committed
baseline. To change the card: make the change (web UI or SSH CLI), then
`python3 networking/ups/apcctl.py export --write`, review the diff and merge
it, so the daily drift check stays clean.

Desired state worth knowing (all in `apc-config.ini`):

- SNMPv1 off. SNMPv3 only, one user profile (`apc2`, SHA-1 auth, AES priv),
  access only from the router (10.4.0.1). Profiles 1, 3 and 4 are unused.
- HTTPS and SSH on; HTTP, telnet and FTP off.
- NTP enabled against the router (10.4.0.1). AOS 6.8 sends **NTPv1**
  requests, which the router's ntpsec does not answer, so the card's clock
  is still wrong (it reads 2018). Event log timestamps are unreliable until
  that is fixed: a firmware update, or an NTP server that answers v1.

Card CLI notes (AOS 6.8, SSH): the card allows one session at a time and keeps
an abandoned one for a few minutes. It drops input typed faster than it reads,
so scripts must type slowly. It does not echo password arguments. `snmpv3`
takes one option per command (several at once give `E102`), and a profile's
privacy must be `none` before its authentication can be. SCP downloads finish
without an exit status, so `scp` exits 1 after a complete copy; `apcctl.py`
checks the announced size instead.

Each command makes exactly one login attempt and never retries.
`apcctl.py apply --plan` shows what an upload would send. `apply --upload
--i-understand-this-reconfigures-the-ups` would upload the desired state,
without any secret lines; the card keeps values that are absent. It refuses
under CI and has not been used. Uploading `config.ini` can change UPS
behaviour (outlet-group delays, transfer points, the self-test schedule), so
use it only with someone present.

The card's firmware is in the monthly firmware report (`ci/reports/firmware.py`,
read from the local `upsc`, no credentials). AOS 6.8.2 / sumx 6.8.0 is behind:
se.com lists NMC2 v7.2.2 (2025-09) for AP9630/31. Read the release notes
before flashing. A card firmware update reboots only the card; afterwards run
`apcctl.py drift` and re-check SNMP (`snmpv3-check.sh`, `upsc`). Never update
the UPS's own firmware remotely.

## SNMP credentials

Telegraf and NUT poll the card as SNMPv3 user **`apc2`**: SHA-1
authentication (the strongest NMC2 offers; SHA-2 needs an NMC3/AP9641) and AES
privacy, with separate passphrases in `homelab/infrastructure/snmp`
(`v3_user`, `auth_password`, `priv_password`), rendered by
`fetch-vault-secrets` as `vault_snmp_v3_user`, `vault_snmp_auth_password`
and `vault_snmp_priv_password`. The old `apc1` user (MD5, one password for
both) was removed on 2026-11-07 (#93; !1071 Telegraf, !1074 NUT).

Check it, read-only: `networking/ups/snmpv3-check.sh` (defaults to `apc2` and
reads the passphrases from Vault when `SNMP_VAULT_PATH` is set).

**Rotating the passphrases** without a monitoring gap: put the new pair in a
spare profile (for example `apc3`) with its own access-control entry for
10.4.0.1, check it with `SNMP_USER=apc3 ... snmpv3-check.sh`, update the Vault
fields (`v3_user` too), deploy Telegraf and NUT, check `upsc apc@localhost
ups.status` and the Grafana UPS panels, then reset the old profile (privacy
`none`, then authentication `none`, access disabled) and re-export
`apc-config.ini`. Passphrases: 15-32 characters, letters and digits.

## Adding a NUT secondary

`upsd` also listens on 10.4.0.1:3493, and the firewall allows that port only
from 10.4.0.0/16 on the mgmt VLAN. Today no other always-on Linux host is on
this UPS: the modem and S2500 are network gear, the Mac mini is macOS, and the
Pis are on the IoT VLAN and probably not on the UPS. To add one (a Linux host
on the mgmt VLAN that is plugged into the UPS):

```bash
apt install nut-client
echo MODE=netclient > /etc/nut/nut.conf
cat > /etc/nut/upsmon.conf <<'EOF'
MONITOR apc@10.4.0.1 1 monitor <vault infrastructure/nut password> secondary
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
EOF
systemctl enable --now nut-monitor
upsc apc@10.4.0.1 ups.status
```

Do it as an Ansible role with the password from Vault, not by hand. When the
primary decides to shut down, it sets FSD. Secondaries see it and shut down,
and the primary waits up to `HOSTSYNC` (15 s) for them to log out. A host on
another VLAN needs an extra `LISTEN` in `upsd.conf` on the router's address in
that VLAN, plus a matching rule in `rules.v4`.

## Supervised test plan (Noah home)

Do this in order; stop at the first surprise. Have a laptop on wired LAN
(Wi-Fi will drop), the card UI (http(s)://10.4.128.8), and the IPMI console
(10.4.128.7) open. Pick a time when a 20-minute outage of every service is OK.

**0. Preflight (read-only)**
- `upsc apc@localhost` shows `ups.status: OL` and `battery.charge: 100`.
- `ipmitool chassis status | grep Restore` shows `always-on` (`ipmictl check` passes).
- `ups-power status`; `ups-orderly-shutdown` (dry plan); Grafana UPS dashboard.
- In the card UI, note the outlet-group delays and which device is plugged into
  which group.

**1. Battery self-test** (first, because a weak battery can drop the load)
- Save work. Expect at worst a hard power cut of everything on the UPS.
- Card UI: UPS > Diagnostics > Self-Test > Start.
- Watch: `watch -n2 'upsc apc@localhost 2>/dev/null | grep -E "ups.status|test|battery.charge"'`.
  It takes about 10 s; the result should become "Done and passed" / `ok`.
- **Failed or the load dropped:** replace the battery (RBC133) before going
  any further. Skip step 3 until then.
- **Passed:** enable the monthly timer and unpause `ups-selftest-stale` (see
  Battery health).

**2. Rehearsal pull-the-plug (nothing shuts down)**
- `touch /etc/nut/ups-power.dry-run` (any deploy removes it again), and
  temporarily set `ups_onbatt_shutdown_minutes: 1` in the role defaults
  (MR + deploy).
- Pull the UPS's mains plug. Within seconds: `ONBATT` in
  `journalctl -t nut-notify -t ups-power -f`, and Wi-Fi drops if the NETGEAR
  switches are not on the UPS. After 30 s, "runtime watch launched". After 1 min,
  "DRY_RUN: would request forced shutdown".
- Plug back in: `ONLINE`, timers cancelled, watch exits.
- `rm /etc/nut/ups-power.dry-run`.

**3. Real pull-the-plug with short timers**
- Keep `ups_onbatt_shutdown_minutes: 1`. Pull the plug and watch the console
  (IPMI KVM) and `tail -f /var/log/ups-shutdown.log`:
  FSD → orderly shutdown steps 1-4 → systemd shutdown → nutshutdown "Commanding
  UPSes (if any) to shutdown" → the UPS turns its outlets off (modem and S2500
  go dark).
- Wait 2-3 minutes, then plug mains back in. The UPS turns the outlets on, and
  the server powers on by itself (always-on) and stops at the root LUKS
  passphrase. Type it (console or IPMI KVM); then the pool unlocks, mpool
  imports, and the containers come back.
- Afterwards: `journalctl -b -1 -u nut-monitor`, `/var/log/ups-shutdown.log`,
  `upsc apc@localhost ups.status` = OL, `ls /etc/killpower` (must be gone),
  and check that the Grafana alerts resolved.
- **If the outlets did not turn off** (the host halted and then rebooted
  after 10 min): the killpower command did not reach or was refused by the
  card. Check the nutshutdown console lines and the card's event log. Do not
  change the delays blind.
- Set `ups_onbatt_shutdown_minutes` back to 5 (MR + deploy).

**Not recommended:** runtime calibration (`calibrate.start`) on this battery.
It deep-discharges the battery and can cut power on a weak one. Revisit after
the replacement.
