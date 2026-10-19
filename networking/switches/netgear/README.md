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

## Local use

```sh
export NETGEAR_HOST=... NETGEAR_USERNAME=... NETGEAR_PASSWORD=...   # or VAULT_ADDR + VAULT_TOKEN
python3 ngswitch.py plan netgear-movie.yml           # live
python3 ngswitch.py plan --offline                   # all files, against the snapshots
python3 ngswitch.py apply --dry-run netgear-movie.yml
python3 ngswitch.py drift
python3 ngswitch.py export netgear-movie.yml -o /tmp/movie.txt
python3 -m unittest discover -s . -p 'test_*.py'
```

Credentials come from Vault at the path in `vault_path` (fields `username`,
`password`, `host`). CI logs in with the `ci-deploy` JWT role.

The export is sanitized before it is written anywhere. Sanitizing removes the
device header line, user credential lines, certificate and key blocks, and the
SNMP engine ID. The tool stops if anything key-like is left.

Each run logs in once and always logs out, because the switch has only a few
web sessions.
