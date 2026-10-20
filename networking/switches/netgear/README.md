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
| `test_ngswitch.py` | Unit tests (offline) |

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
  whose stored file name is not `flash://system/images/image<n>.bin` is marked
  `[INVALID]`, and a next-boot image that is invalid is flagged in the line.
  `netgear:drift` adds the same line to its report as a note; a switch that is
  behind does not fail the drift job.
- `ngswitch.py firmware set-next-boot <name>.yml <1|2>` picks the image slot for
  the next boot, the same as "Activate Image" under Maintenance > File
  Management > Dual Image. It does not reboot, and it refuses a slot that has
  no version or an invalid file name.
- `ngswitch.py firmware upgrade <name>.yml` upgrades one switch. It reboots the
  switch, so everything attached to it is down for a few minutes. Steps:
  1. Download the zip (or use the cache) and check both hashes and the version
     in the image header.
  2. Log in. Refuse if the switch already runs the desired version, if the live
     state differs from `<name>.yml`, if the management interface is not on
     the management VLAN, or if the next-boot image is not the running image.
  3. Write a sanitized export to `--backup-dir` (not committed).
  4. Upload the image through Maintenance > Download > HTTP File Download
     (file type Software) and wait for the flash write.
  5. Verify the upload: the switch must report exactly the image's size as
     transferred, and the new slot must hold the desired version under its
     normal file name (`image<n>.bin`). If any check fails, set the next-boot
     image back to the running one and stop without rebooting.
  6. Make sure the new image is the next-boot image and the other slot still
     holds the old version, then reboot.
  7. Wait for the web UI to go down and come back (network errors are expected
     while it reboots), log in again and check the running version.
  8. Run `drift` for the switch. If it reports drift, the command fails and
     prints how to boot the old image again.

**The HTTP upload is unreliable on the MS510TXPP.** An upload can report
"done" with the full byte count and a plausible version in the slot, while
the switch stored the image under a truncated file name (e.g.
`flash://system/images/rs`) and then refuses to boot it, coming back on the
old image. The switch also makes the uploaded slot the next-boot image on its
own, so a failed upload leaves the next boot pointing at a broken image. The
upgrade command checks for this and resets the next boot, but before trying
again, check `firmware status` and consider uploading through the web UI
(or TFTP) instead, then verifying the slot with `firmware status`.

In CI, `netgear:firmware` on `main` is manual only and never runs from a
schedule. Start it with `NETGEAR_FIRMWARE_TARGET=<name>`, one switch at a time,
and check what hangs off that switch before starting the next one.

The firmware version is also the header line of the export (`v6.7.0.42 / ...`).
Drift leaves that line out of the comparison with the snapshot.

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
python3 ngswitch.py firmware upgrade --dry-run netgear-movie.yml   # checks + backup, no upload/reboot
python3 -m unittest discover -s . -p 'test_*.py'
```

Credentials come from Vault at the path in `vault_path` (fields `username`,
`password`, `host`). CI logs in with the `ci-deploy` JWT role.

The export is sanitized before it is written anywhere. Sanitizing removes the
device header line, user credential lines, certificate and key blocks, and the
SNMP engine ID. The tool stops if anything key-like is left.

Each run logs in once and always logs out, because the switch has only a few
web sessions.
