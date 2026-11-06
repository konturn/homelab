# NETGEAR switches

Desired state for the NETGEAR MS510TXPP switches. These switches have no CLI or
API, only a web UI, so `ngswitch.py` talks to the same XML endpoints the UI uses.
It writes one setting at a time and never uses the whole-config restore, which
reboots the switch.

| File | Purpose |
|---|---|
| `<name>.yml` | Desired state: VLANs, per-port membership, PVID, PoE, protected ports |
| `<name>.running-config.txt` | Sanitized export of the switch configuration, used as the offline baseline and for drift checks |
| `ngswitch.py` | Tool (Python standard library + PyYAML) |
| `firmware_ci.py` | CI side of firmware updates: bump MR, rollout report, issue and mail |
| `test_ngswitch.py`, `test_firmware_ci.py` | Unit tests (offline) |
| `testdata/` | Fixtures for the tests (captured download-server answers) |

## Managed settings

- VLANs: id and name. VLAN 1 is the default VLAN and is not renamed or removed.
- Per port: untagged and tagged VLANs, PVID.
- Per PoE port: `enabled`, `priority` (`low` / `medium` / `high`, as in the UI), `limit_mw`.

Everything else in the configuration is not written by the tool. Drift checks
compare it with the committed export.

## Protected ports

`management.protected_ports` lists the uplink and every port that carries the
management VLAN. The tool refuses to:

- change management-VLAN membership or tagging on a protected port
- change the PVID of a protected port
- turn off PoE on a protected port
- delete the management VLAN
- apply anything if a port carrying the management VLAN is missing from the
  list, or if the switch's management IP interface is not on `management.vlan`

## Making a change

1. Edit `<name>.yml`.
2. Run `python3 ngswitch.py render-snapshot <name>.yml` to update the managed
   lines of the export, then commit both files.
3. Open an MR. CI runs the unit tests, `check` (schema and file consistency),
   and `plan --offline` against the export on the target branch. MR pipelines
   get no switch credentials.
4. After the merge, `netgear:apply` runs on `main`. It reads the live switch,
   writes only the differences (no writes when there are none), then reads the
   switch again and compares both the live state and a fresh export with the
   desired state. The job fails if anything differs.

## Drift check

A daily pipeline schedule on `main` with `NETGEAR_TASK=drift` runs
`netgear:drift`. It fails, and mails the report to root through the router,
when either of these is true:

- the live state differs from `<name>.yml`
- the export differs from `<name>.running-config.txt`

To accept a change made in the web UI, copy the job's `netgear-export/`
artifact over the snapshot and update `<name>.yml` (`plan` shows the differences).

## Firmware

`firmware` in `<name>.yml` declares the version the switch should run, with the
vendor download URL, the SHA-256 of the zip and of the `.bin` image inside it.
NETGEAR does not publish checksums, so the hashes are those of the file
downloaded from the URL when the version was declared.

- `ngswitch.py firmware status` shows both image slots, the running and
  next-boot image, and whether a switch is behind the desired version. A slot
  whose stored file name is not a complete `flash://system/images/<name>.bin`
  is marked `[INVALID]`, and a next-boot image that is invalid is flagged in
  the line. `netgear:drift` adds the same line to its report as a note; a
  switch that is behind does not fail the drift job.
- `ngswitch.py firmware set-next-boot <name>.yml <1|2>` picks the image slot for
  the next boot, the same as "Activate Image" under Maintenance > File
  Management > Dual Image. It does not reboot, and it refuses a slot that has
  no version or an invalid file name.
- `ngswitch.py firmware upgrade [--tftp-server <local IPv4>] <name>.yml`
  brings one switch to the declared firmware (below).
- `ngswitch.py firmware rollout [--window HH:MM-HH:MM] <file>...` runs
  `upgrade` for each switch in the order given and stops at the first one that
  does not finish clean. It starts no switch outside the window.
- `ngswitch.py firmware check-latest` and `firmware bump --version <v>` find
  and declare a newer release (see "Firmware updates in CI").

The firmware version is also the header line of the export (`v6.7.0.52 / ...`).
Drift leaves that line out of the comparison with the snapshot.

Drift also leaves out `snmp-server set rndImageInfoTable ...`. The switch adds
that line when its image table is read or written (Dual Image page, firmware
load). It only selects a stack unit for the next query, so it is runtime
state, not configuration.

### How the switch takes new firmware

Found while upgrading both switches from 6.7.0.42 to 6.7.0.52 (2026-11):

- **Reboot with unit 0.** Maintenance > Reset > Device Reboot posts `Reload`
  with unit 0 on a standalone switch. Unit 1 also restarts the switch, but it
  comes back on the image it was running and ignores the next-boot setting,
  so the new image is never tried. The tool sends unit 0.
- **Load the image by TFTP.** The HTTP upload is unreliable on the MS510TXPP:
  it can report "done" with the full byte count while storing the image under
  a truncated file name (e.g. `flash://system/images/rs`). A TFTP load stores
  it under the vendor file name (`MS510TX_TXPP_V6.7.0.52.bin`), which boots
  normally. The switch writes the inactive slot and makes it the next-boot
  image by itself.
- **The web session dies during the flash write.** After about 18 MB of the
  transfer, every read of the session answers HTTP 404. The tool logs in again
  and keeps polling; the transfer finishes on its own.
- **Slots are renumbered at boot.** The running image is always reported as
  image1, so after the upgrade the old version shows up as image2.
- **Verify by MD5.** Maintenance > Tech Support Info (`show bootvar`) lists each
  stored image's MD5, which is the MD5 of the whole `.bin` file. The tool
  compares it with the local image.
- Boot code stays 1.0.05; no intermediate release is needed between 6.7.0.42
  and 6.7.0.52. 6.7.0.52 stops emitting the default lines
  `snmp-server group default v3 noauth ...` and `snmp-server user admin default v3`,
  so the snapshots were refreshed after the upgrade.

### `firmware upgrade`

The command can be run again at any point. It does what is still missing and
stops when the switch already runs the declared version:

1. Download the zip (or use the cache), check both hashes and the version in
   the image header.
2. Log in. If the switch already runs the declared version: set the next boot
   to the running image if needed, and stop.
3. Refuse if VLANs, ports or PoE differ from `<name>.yml` or the management
   interface is not on the management VLAN. Write a sanitized export to
   `--backup-dir` (not committed). Point the next boot at the running image.
4. If the inactive slot already holds the declared version with the local
   image's MD5, keep it. Otherwise load the image into the inactive slot (the
   running slot is never written): with `--tftp-server <IP>` the tool serves
   the verified image from a built-in, read-only TFTP server on `<IP>`:69 that
   answers only for that file and runs only during the transfer; without it,
   the HTTP upload. Then check the byte count, the slot's file name and
   version, and the MD5. On any mismatch, point the next boot back at the
   running image and stop without rebooting.
5. Set the new slot as the next boot and reboot (unit 0). Wait for the web UI to
   go down and come back, and log in again.
6. If the switch came back on the old image (it falls back by itself when an
   image does not boot), point the next boot at it and fail.
7. If the new image runs but VLANs, ports or PoE differ, or the management
   interface is missing, **roll back**: boot the old image again, confirm it
   runs, and fail.
8. Run `drift`. Exit 3 if the configuration export differs from the snapshot
   (new firmware defaults, for instance). The switch keeps the new firmware,
   since its managed state was already verified. The message says how to fall
   back.

`--dry-run` does steps 1 to 4 without changing anything (no next-boot change,
no load, no reboot) and prints each step it would take. Each switch is down
for about 95 seconds; the APs behind them are back after about 3 minutes.

Run it from the router (the switches must reach the TFTP address), as root
for UDP 69:

```sh
python3 ngswitch.py firmware upgrade --tftp-server 10.4.0.1 --dry-run netgear-movie.yml
python3 ngswitch.py firmware upgrade --tftp-server 10.4.0.1 netgear-movie.yml
```

### Firmware updates in CI

- **Finding a release.** NETGEAR's support pages are rendered by scripts and
  have no listing that can be read without running them. The download server
  is plain: every release is a zip at the same URL except for the version, and
  it answers 200 for a release and 403 for a missing file. `firmware
  check-latest` probes the versions that could follow the declared one: the
  next 40 builds, and the first 40 builds of the next maintenance, minor and
  major release. It fails loudly if the declared release is no longer found,
  if a version that cannot exist is reported as present, or if a hit is not a
  zip of plausible size. Any of those means the server changed.
- **`netgear:firmware-check`** (weekly pipeline schedule with
  `NETGEAR_TASK=firmware-check`) runs `firmware_ci.py bump-mr`. If a newer
  release exists and no MR for it was opened before, it downloads the zip,
  computes both SHA-256s, checks the image header, and opens an MR
  (`netgear-firmware/<version>`, label `netgear-firmware`). The MR updates
  `firmware:` in both `netgear-*.yml` and `KNOWN_LATEST` in
  `ci/reports/firmware.py`, and carries the release notes from the zip. It
  also mails root. A closed MR is not reopened.
- **Merging the MR is the approval.** `netgear:firmware` runs from a daily
  pipeline schedule with `NETGEAR_TASK=firmware` at 04:45 and only acts inside
  `NETGEAR_FIRMWARE_WINDOW` (default `04:45-06:00` local). That keeps it clear of
  the 02:15-04:00 backup window and before the 06:20 drift checks. When both
  switches run the declared version it does nothing. Otherwise it upgrades
  netgear-movie, and only after movie passes its checks and drift (plus a
  two-minute settle) it upgrades netgear-office. It mails root when something
  was upgraded.
- **On failure** (did not verify, did not boot, rolled back, drift after the
  upgrade, error), the job stops before the next switch. It opens an issue
  labelled `netgear-firmware-failed` with the log and mails root. While such an
  issue is open, `netgear:firmware` does nothing, so a bad release is not
  retried every night. Sort out the switch, refresh the snapshot if the
  difference is new firmware defaults, and close the issue. The next window
  carries on with the remaining switch. A timeout or crash that the script
  could not report is mailed by the job's `after_script`.
- The job runs on the protected runner (host network), as root, so its TFTP
  server can bind 10.4.0.1:69. `NETGEAR_TFTP_SERVER` sets the address.
- **One firmware rollout per night.** The NETGEAR, Aruba Instant and S2500
  firmware jobs share a lock in the runner's cache
  (`networking/firmware_night.py`, `/cache/firmware-night`). A rollout never
  starts while another one runs, and a scheduled rollout does nothing on a
  night when another family already changed devices. `netgear:firmware` runs
  at 04:45; `aruba-iap:firmware` runs at 05:20 and stays off on a night when
  the switches were upgraded. A NETGEAR run that upgrades nothing leaves the
  night free.

### Starting it by hand, or from OpenClaw

OpenClaw gets no switch credentials. It starts the CI job, which reads them
from Vault with the `ci-deploy` role:

```sh
# needs a token allowed to run pipelines on main
curl -sS -X POST -H "PRIVATE-TOKEN: $TOKEN" "https://gitlab.lab.nkontur.com/api/v4/projects/4/pipeline" \
  -H 'Content-Type: application/json' -d '{"ref": "main", "variables": [
    {"key": "NETGEAR_TASK", "value": "firmware"},
    {"key": "NETGEAR_FIRMWARE_TARGET", "value": "netgear-movie"},
    {"key": "NETGEAR_FIRMWARE_DRY_RUN", "value": "1"}]}'
```

The same works from Build > Pipelines > Run pipeline. A pipeline that a person
or OpenClaw starts runs at once, ignoring the window. Without
`NETGEAR_FIRMWARE_TARGET` it does both switches, movie first.
`NETGEAR_FIRMWARE_DRY_RUN=1` checks everything without loading or rebooting,
and opens no issue. Before starting a real run:

- `main` declares the wanted firmware (the bump MR is merged)
- no issue labelled `netgear-firmware-failed` is open (the job does nothing while one is)
- `netgear:drift` is clean for the switch: the upgrade refuses a switch whose state differs
- the reboot is acceptable now: the movie switch carries movie-ap, and the office
  switch carries office-ap and the admin Wi-Fi

## Local use

```sh
export NETGEAR_HOST=... NETGEAR_USERNAME=... NETGEAR_PASSWORD=...   # or VAULT_ADDR + VAULT_TOKEN
python3 ngswitch.py plan netgear-movie.yml           # live
python3 ngswitch.py plan --offline                   # all files, against the snapshots
python3 ngswitch.py apply --dry-run netgear-movie.yml
python3 ngswitch.py drift
python3 ngswitch.py export netgear-movie.yml -o /tmp/movie.txt
python3 ngswitch.py firmware status
python3 ngswitch.py firmware set-next-boot netgear-movie.yml 1           # no reboot
python3 ngswitch.py firmware upgrade --dry-run netgear-movie.yml   # checks + backup, no load/reboot
python3 ngswitch.py firmware check-latest                         # newer release on the download server?
python3 -m unittest discover -s . -p 'test_*.py'
```

Credentials come from Vault at the path in `vault_path` (fields `username`,
`password`, `host`). CI logs in with the `ci-deploy` JWT role.

The export is sanitized before it is written anywhere. Sanitizing removes the
device header line, user credential lines, certificate and key blocks, and the
SNMP engine ID. The tool stops if anything key-like is left.

Each run logs in once and always logs out, because the switch has only a few
web sessions.
