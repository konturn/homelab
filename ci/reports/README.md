# Scheduled reports

Read-only reports that feed the OpenClaw Morning Digest. Each runs as a
scheduled CI job on `main` (protected runner), selected by `SCHEDULED_TASK`:

| `SCHEDULED_TASK` | Job | Script | Output |
|---|---|---|---|
| `trivy` (daily) | `report:cves` | `trivy_scan.py` on the router, then `trivy_label.py` | `cves` |
| `app-report` (weekly) | `report:app-updates` | `app_updates.py` on the router | `app-updates` |
| `os-report` (weekly) | `report:os-patches` | `ansible/os-patch-report.yml` (`os_facts.py` on each host), then `os_patches.py` | `os-patches` |
| `firmware-report` (monthly) | `report:firmware` | `firmware.py` on the router | `firmware` |

`run_remote.py` ships `ci/reports/` to a private temp dir on the router and
runs the script there. Secrets requested with `--secret` are read from Vault
with the job's ID token (`ci-deploy` role) and passed on stdin, never in argv
or logs.

Each report writes `<name>.md` and `<name>.json` to
`/persistent_data/application/openclaw/workspace/reports/updates/` on the
router (owner 1000). The markdown has a fixed header the digest relies on:

```
# <Title>
generated: <UTC ISO8601>
summary: <one line, at most 120 characters>

- <at most 15 detail lines>
```

Report contents are host-specific and stay on the router; they never go into git.

`report:cves` scans each image once at every severity and also exports the
result, one compact row per finding (id, package, installed/fixed version,
severity, CVSS vector, CWEs), to `/var/lib/trivy-reports/images/<image-id>.json`
on the router (root-only, 0700/0600; exports of images that stopped running are
removed). Other local tools read these instead of scanning the images again.
`SCAN_EXPORT_DIR=""` turns the export off.

`report:cves` also adds the `security` label to open image-update MRs (Renovate
or moltbot) for images with fixable HIGH/CRITICAL findings, using
`AUTOMERGE_TOKEN`. It only adds labels.

Nothing here changes a host or device: no upgrades, restarts, reboots or
configuration writes. The firmware report reads the NETGEAR configuration
download header, the Aruba Instant `show version` (REST), the S2500 SNMP
`sysDescr`, `dmidecode`, `ipmitool mc info` and `smartctl -i`. Latest versions
are compared only where `KNOWN_LATEST` in `firmware.py` has an entry.

Run a report by hand: CI/CD > Pipelines > Run pipeline on `main` with
`SCHEDULED_TASK=<value>`. Tests: `python3 -m unittest discover -s ci/reports -v`.
