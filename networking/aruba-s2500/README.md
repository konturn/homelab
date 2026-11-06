# Aruba S2500 core switch

The S2500-48P (ArubaOS-MAS 7.4) is the core switch: the router's uplinks
(port-channel 0) and both NETGEAR switches hang off it. Its own address is
10.100.0.1 on the out-of-band `interface mgmt` port, cabled straight to the
router's `enp2s0f1` (10.100.0.2/24). Its configuration is the template in
`networking/switches/aruba-config`; this directory covers inventory and firmware.

| File | Purpose |
|---|---|
| `s2500ctl.py` | Read-only inventory over the SSH CLI; firmware status, check-latest, manual upgrade |
| `firmware.yml` | Desired firmware: release, image file name, SHA-256 |
| `known_hosts` | The switch's SSH host key (pinned; ssh-rsa is the only type it offers) |
| `test_s2500ctl.py`, `testdata/` | Offline unit tests |

**Rebooting the S2500 takes down the entire LAN**: every VLAN, the APs, the
NETGEAR switches, and the router's own uplinks. A bad image can leave it
unbootable until someone uses the serial console. Nothing here reboots it
unless a person asks for it explicitly (below).

## Credentials

The tool reads Vault `homelab/network/aruba-s2500`:

| Field | Value |
|---|---|
| `host` | `10.100.0.1` (optional; the default) |
| `username` | a management user (`mgmt-user`); a read-only role is enough for `inventory` and `status`, `upgrade` needs root |
| `password` | its password |
| `enable_secret` | the `enable` secret, if the user lands in `>` mode and `enable` asks for one |

The path does not exist yet, so the tool is inert: `inventory` and `upgrade`
exit 4 with "no S2500 credentials", and `firmware status` falls back to SNMP
for the running version. The switch accepts password logins only (no public
key). `homelab/infrastructure/aruba` (`password`, `config_secret`) predates
this tool and is not used by it.

Locally: `S2500_HOST`, `S2500_USER`, `S2500_PASS`, `S2500_ENABLE` instead of Vault.

## SNMP

The switch answers SNMPv2c (community `public`, read-only view, see
`networking/switches/aruba-config`) from the router: Telegraf polls it and the
firmware report reads `sysDescr` (`ArubaOS (MODEL: ArubaS2500-48P-US), Version
7.4.1.12 (72393)`). That gives the running version only; the image partitions
and the boot partition need the CLI. No switch configuration change is needed.

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
The CLI syntax above follows the ArubaOS 7.x CLI reference; it has not been run
against this switch (no credentials yet). Check it with `inventory` and a dry
run once the credentials are in Vault.
