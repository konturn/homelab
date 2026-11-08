# Aruba S2500 core switch

The S2500-48P (ArubaOS-MAS 7.4) is the core switch: the router's uplinks
(port-channel 0) and both NETGEAR switches hang off it. Its own address is
10.100.0.1 on the out-of-band `interface mgmt` port, cabled straight to the
router's `enp2s0f1` (10.100.0.2/24). This directory covers its configuration
(desired state, a committed snapshot, drift checks and a narrow `apply`),
inventory and firmware.

| File | Purpose |
|---|---|
| `s2500ctl.py` | Read-only inventory over the SSH CLI; firmware status, check-latest, manual upgrade |
| `s2500cfg.py` | Configuration: `export`, `plan`, `drift`, `apply` (allow-listed edge-port changes only), `check` |
| `s2500.yml` | Desired state: access switching-profiles and edge ports (profile, PoE, description) |
| `s2500.running-config.txt` | Sanitized `show running-config` (secrets replaced by `<redacted>`), the drift baseline |
| `firmware.yml` | Desired firmware: release, image file name, SHA-256 |
| `known_hosts` | The switch's SSH host key (pinned; ssh-rsa is the only type it offers) |
| `test_s2500ctl.py`, `test_s2500cfg.py`, `testdata/` | Offline unit tests |

`networking/switches/aruba-config` is the same configuration as an Ansible
template (`enable secret` from `ARUBA_CONFIG_SECRET`, the other redacted lines
left out). `s2500cfg.py render-template` writes it from the snapshot, and
`s2500cfg.py check` (in `aruba-s2500:test`) fails when the two disagree.

**Rebooting the S2500 takes down the entire LAN**: every VLAN, the APs, the
NETGEAR switches, and the router's own uplinks. A bad image can leave it
unbootable until someone uses the serial console. Nothing here reboots it
unless a person asks for it explicitly (below).

## Credentials

`homelab/network/aruba-s2500` in Vault is the canonical entry:

| Field | Value |
|---|---|
| `host` | `10.100.0.1` (optional; the default) |
| `username` | `admin` (`mgmt-user`, root role) |
| `password` | its password |
| `enable_secret` | the `enable` secret (plain text) |

`admin` logs in to `>` (user) mode, where almost nothing works: `show
running-config`, `show interface` and `configure terminal` all need `enable`.
The tools send `enable` and the secret on login. Two ArubaOS quirks:

- The enable secret is at most **15 characters**. A longer one is not accepted.
- Interactive password prompts (the `enable` prompt, `enable secret`) take the
  input up to `\r`. A `\n` after the secret becomes part of it, and `enable` fails.

The switch accepts password logins only (no public key) and offers only an
`ssh-rsa` (SHA-1) host key, which paramiko 4+ rejects: use `paramiko<4` (CI pins it).
The older entries
`homelab/infrastructure/aruba` (`password`, `config_secret`) and
`homelab/infrastructure/switch` (`admin_password`) predate this directory and are
not used; `config_secret` is the hashed form from a config export, not the
enable secret. Keep `network/aruba-s2500` current; the old entries are left in
place for history only.

Locally: `S2500_HOST`, `S2500_USER`, `S2500_PASS`, `S2500_ENABLE` instead of Vault.

## Configuration

`s2500.yml` manages the 48 edge copper ports (0/0/0-0/0/47): each port's access
switching-profile (`vlan<N>`, or `default` = VLAN 1), PoE (on by default through
the `default` interface-group) and description. A port not listed is expected to
have no switching-profile, PoE on and no description. `access_profiles` lists the
`vlan<N>` access profiles; a missing one is created, an existing one is never
changed.

```sh
python3 s2500cfg.py check               # offline: s2500.yml, snapshot and template agree
python3 s2500cfg.py plan --offline      # commands for s2500.yml against the snapshot
python3 s2500cfg.py plan                # ... against the live switch
python3 s2500cfg.py drift [--report f]  # live vs snapshot and s2500.yml (exit 2 on drift)
python3 s2500cfg.py export              # rewrite the snapshot from the live switch
python3 s2500cfg.py apply [--yes]       # dry run without --yes
```

### What `apply` may change

Every command is checked against an allow-list before anything is sent:

- `interface gigabitethernet 0/0/<0-47>` with `switching-profile "vlan<N>"` /
  `no switching-profile`, `poe-profile "poe-factory-initial"` / `no poe-profile`,
  `description "..."` / `no description`;
- `interface-profile switching-profile "vlan<N>"` with `access-vlan <N>` (new profiles only).

It refuses anything touching port-channels, the uplinks 0/1/x, `interface mgmt`,
VLANs and VLAN interfaces, trunk or LACP settings, spanning tree, users, AAA,
SNMP, logging, the enable secret, firmware or boot settings, `write erase`,
`reload` and interface-groups. `protected_ports` (the management-VLAN access
ports) are never changed. Turning PoE off is not supported (the switch has no
PoE-off profile); do it by hand and record it.

`apply` reads the running-config, sends the planned commands, reads it again
and checks that only the planned blocks changed and nothing is left to do. Only
then does it `write memory`. If either check fails it stops without saving;
look at the switch by hand. It never runs in CI. To change a port: edit
`s2500.yml`, `plan`, `apply --yes`, then `export` and commit the snapshot (and
`render-template`) in the same MR.

### Drift check

`aruba-s2500:drift` runs daily on `main` on the Aruba AP drift schedule
(`IAP_TASK=drift`, so it needs no pipeline schedule of its own), or by hand with
Run pipeline and `SCHEDULED_TASK=s2500-drift`. It runs `s2500cfg.py drift` and
opens, updates or closes the `s2500-drift` issue (`ci/ipmi/drift_issue.py`). The
issue shows the edge ports that differ from `s2500.yml` and a diff of the live
running-config against `s2500.running-config.txt`, which also covers everything
the tool never writes. Its artifact `s2500-live.running-config.txt` is the
sanitized live config: to accept a change made by hand, copy it over the
snapshot, update `s2500.yml`, run `render-template`, and commit.

`controller config <N>` is a revision counter that every `write memory` bumps,
so the snapshot stores it as `controller config <revision>`. The
`# ArubaOS Version` header line comes from the running firmware, so an upgrade
shows up as drift until the snapshot is refreshed.

## SNMP

The switch answers SNMPv2c (community `public`, read-only view, see
`s2500.running-config.txt`) from the router: Telegraf polls it and the
firmware report reads `sysDescr` (`ArubaOS (MODEL: ArubaS2500-48P-US), Version
7.4.1.12 (72393)`). That gives the running version only; the image partitions
and the boot partition need the CLI. No switch configuration change is needed.

## Time

The clock is EST/EDT (`clock timezone`, recurring `clock summer-time`) and
`ntp server 10.100.0.2` syncs it from the router's ntpd over the management
link; `rules.v4` allows UDP 123 from 10.100.0.1 only.

## Usage

```sh
python3 s2500ctl.py inventory              # show version / show image version / show boot
python3 s2500ctl.py firmware status        # running, both partitions, boot partition (SNMP without credentials)
python3 s2500ctl.py firmware check-latest
python3 s2500ctl.py firmware upgrade --dry-run
```

## Firmware

ArubaOS-MAS 7.4 is the last release train for the S1500/S2500/S3500, which are
end of life. HPE lists no newer release for them; 7.4.1.12 (build 72393, the
running one) is the newest release known (checked 2026-11-06, from community
reports; the HPE portal cannot be read without an account). So
`check-latest` prints "CANNOT CHECK" for the vendor and "nothing newer known",
and looks for a newer image staged at
`/cache/gitlab-runner/aruba-s2500-firmware/ArubaOS_MAS_<version>_<build>` on the
router. `KNOWN_LATEST["S2500"]` in `ci/reports/firmware.py` holds 7.4.1.12.

`firmware.yml` declares 7.4.1.12_72393 with no image (it was installed before
this file existed). To declare another release, download the image, stage it,
and set `version`, `image` and `sha256` (`sha256sum` of the file) in an MR.

### `firmware upgrade` (manual only)

The S2500 upgrade never runs on a schedule. The CI job `aruba-s2500:firmware`
is a manual job on `main`, and the command refuses unless:

- `S2500_FIRMWARE_CONFIRM=I-AM-HOME` is set (Run job with variables), and
- it is outside the backup window (`02:15-04:00` local, `S2500_BACKUP_WINDOW`).

`S2500_FIRMWARE_DRY_RUN=1` runs the checks without the confirmation and sends
nothing. **Be at home, with the serial console cable and a laptop at hand.**
Expect every network service, Wi-Fi and the internet to drop for about 5-10
minutes (the switch takes several minutes to boot), longer if it has to be
recovered.

Steps:

1. Read `show version`, `show image version` and `show boot`. Nothing to do
   when the switch runs the declared release.
2. Refuse if the running release is newer, or the boot partition does not hold
   the running release.
3. The target is the other partition. If it already holds the declared
   release, skip the copy. Otherwise check the staged image's SHA-256 against
   `firmware.yml`, serve it from a read-only TFTP server on the router's
   management-link address (`--tftp-server`, default 10.100.0.2), and send
   `copy tftp: <server> <image> system: partition <n>`; then check that the
   partition holds the release. The boot partition is not touched until then.
4. `boot system partition <n>`, `write memory`, `reload`.
5. Wait for the switch to answer SSH again (up to about 25 minutes) and check
   the running release. If it came back on the old release, point the boot
   partition back at the old one and fail. If it does not come back, fail with
   the console steps: `boot system partition <old>` and `reload` on the console
   go back to the previous release, which stays on the old partition.

The job holds the shared firmware lock (`networking/firmware_night.py`), so it
never runs alongside a NETGEAR or Aruba Instant rollout.

**Before the first real upgrade**, the router's firewall must let the switch
fetch the image: INPUT allows TFTP only from the management VLAN (`bond0.4`),
and the switch reaches the router over `enp2s0f1`. Add, in
`networking/iptables/rules.v4` next to the TFTP rule, in its own MR:

```
# TFTP from the S2500 management port (firmware loads, networking/aruba-s2500)
-A INPUT -i enp2s0f1 -s 10.100.0.1 -p udp -m udp --dport 69 -j ACCEPT
```

The TFTP server only runs during the transfer and serves only the one file.
The `copy tftp:` / `boot system partition` syntax above follows the ArubaOS 7.x
CLI reference and has not been run against this switch yet; check it with a dry
run first.
