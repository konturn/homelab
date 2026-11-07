# Aruba Instant AP cluster

The Wi-Fi cluster (two AP-515s running Aruba Instant, one acting as virtual
controller) is configured from `running-config.conf` in this directory.

The cluster has a static virtual controller address, 10.4.128.10 on the
management VLAN (`virtual-controller-ip`, outside the DHCP pool). It always
answers on the current conductor, so the live tools and CI connect to it
(Vault `homelab/network/aruba-iap#host`), never to an AP's own DHCP address.

| File | Purpose |
|---|---|
| `running-config.conf` | Desired state: the cluster's `show running-config`, normalised |
| `iapctl.py` | Export, plan, apply and drift check over the controller's SSH CLI; `firmware` subcommands |
| `iapfw.py` | Firmware logic behind `iapctl.py firmware` |
| `firmware.yml` | Desired firmware: release, image file name, SHA-256 |
| `firmware_ci.py` | CI side of firmware updates: bump MR, rollout, issue and mail |
| `test_*.py`, `testdata/` | Offline unit tests; captured CLI output (serials and MACs replaced) |
| `known_hosts` | SSH host keys of the APs and of the virtual controller address, which is listed with both APs' keys (either can become the conductor) |

## Secrets

Secret values never appear in git. `export` replaces them with placeholders
of the form `{{vault:<path>#<field>}}`:

| Placeholder path | Fields |
|---|---|
| `homelab/network/aruba-iap` | `host`, `username`, `password` (CLI login). The controller key and admin hash placeholders are not stored; those lines are protected and never sent |
| `homelab/network/aruba-iap-ssids` | one field per SSID profile name, holding the WPA passphrase in clear text |

Placeholders match any live value, so they never show up as a difference.
They are only resolved from Vault when a change has to send that line, for
example a new SSID profile. If the field is missing, `apply` refuses.

The controller stores passphrases encrypted (with a random IV per export),
so a passphrase changed outside git is not detected by the drift check.

## Usage

```sh
# offline: what would change between two files
python3 iapctl.py plan --current old.conf --desired running-config.conf

# live (credentials from IAP_HOST/IAP_USER/IAP_PASS, or VAULT_ADDR + VAULT_TOKEN)
python3 iapctl.py export -o running-config.conf
python3 iapctl.py plan
python3 iapctl.py apply [--dry-run] [--allow-protected]
python3 iapctl.py drift
```

`apply`:

1. exports the live config and diffs it against the desired file;
2. sends only the changed blocks or lines (`configure terminal`, the block
   header, the changed lines, `exit`, then `end` and `commit apply`);
3. re-exports and fails if any difference remains.

An empty diff sends nothing. Changes to the virtual controller address or
VLAN, the uplink and the wired profile assigned to the uplink port, mesh and
extended-SSID, cluster membership, and management access are refused unless
`--allow-protected` is given. Changes to an access rule are refused if the
resulting rule order cannot be reproduced; edit those on the controller and
re-export.

Not managed by `apply`: firmware (the `version` line is ignored; see
"Firmware"), per-AP settings (`show ap-env`: AP names, per-AP uplink VLAN, radio
overrides) and secrets as described above.

## CI

| Job | When | Credentials |
|---|---|---|
| `aruba-iap:test` | MRs and main, on changes here | none: unit tests and an offline plan against the file on the target branch |
| `aruba-iap:apply` | main, on changes here | Vault (`ci-deploy`), protected runner |
| `aruba-iap:apply-protected` | main, manual | as above, with `--allow-protected` |
| `aruba-iap:drift` | daily schedule on main with `IAP_TASK=drift` | as above; mails root on drift |
| `aruba-iap:firmware-check` | weekly schedule on main with `IAP_TASK=firmware-check` | Vault; opens a bump MR |
| `aruba-iap:firmware` | daily schedule on main with `IAP_TASK=firmware` (05:20), or started by hand | Vault; upgrades the cluster |

To change the Wi-Fi config, edit `running-config.conf` (or change it on the
controller and run `export`), open an MR, and review the plan in
`aruba-iap:test`.

## Firmware

`firmware.yml` declares the release the cluster runs: `version`
(`8.13.1.0_93688`), the Instant image class of the AP-515 (`Draco`), the release
train to stay on (`branch: "8.13"`, LSR), the image file name and its SHA-256,
and how many APs the cluster has. Instant runs **one release per cluster**: the
virtual controller downloads the image, hands it to the other APs, and every AP
reboots together. **A cluster upgrade takes the whole-house Wi-Fi down for about
5-10 minutes** (download over the LAN, then about 3-4 minutes of AP boot and
cluster formation).

Each AP has two partitions. `show image version` lists the running (primary)
one and the backup; an upgrade writes the backup partition and boots it, so the
release that ran before stays on the backup partition. `switch-partition-reboot`
reboots an AP into its other partition (`switch-partition-reboot all` on the
controller does every AP); that is the rollback.

```sh
python3 iapctl.py firmware status            # per AP: running, backup, class; upgrade status
python3 iapctl.py firmware check-latest      # vendor (see below) and staged images
python3 iapctl.py firmware bump --image /cache/gitlab-runner/aruba-iap-firmware/ArubaInstant_Draco_8.13.1.1_94000
python3 iapctl.py firmware bump --version 8.13.1.1_94000 --sha256 <from the download page>
python3 iapctl.py firmware upgrade --tftp-server 10.4.0.1 --dry-run
python3 iapctl.py firmware upgrade --tftp-server 10.4.0.1     # as root (UDP 69), from the router
```

`status` reads `show aps` and `show upgrade info` on the controller and `show
image version` on each AP over its own address (all in `known_hosts`).

### Finding a release

There is no public source that can be read reliably:

- The HPE Networking Support Portal (networkingsupport.hpe.com, formerly
  asp.arubanetworks.com) is an app whose software listing comes from a GraphQL
  API that answers 401 without an HPE SSO session; downloads also require
  accepting the license terms per file.
- The release notes on arubanetworking.hpe.com/techdocs answer 403 to
  scripts, and HPE publishes no feed of Instant releases.
- The APs' own cloud image check is not exposed in the CLI and needs Activate
  provisioning, which this cluster does not use.

So `check-latest` reports **CANNOT CHECK** for the vendor, unless a portal
listing gets implemented (below), and in any case looks at the staging
directory: an image there whose name is on the declared branch and class and
newer than `firmware.yml` counts as a newer release. Exit codes with
`--detailed-exitcode`: 2 newer release, 3 cannot check (nothing newer staged).

`check-latest` reads `homelab/network/hpe-portal` from Vault, but only to tell
"no credentials" from "credentials but no portal client". Listing releases
with an HPE account is not implemented: the portal's API cannot be inspected
without an account, and its login is HPE SSO. Building it would need an HPE
Networking Support Portal account (an HPE SSO login without an MFA prompt, or
with a method that can be scripted) stored as `username` and `password` at that
path, and one session in a browser to record the requests the software page
makes. Until then, Noah checks the portal by hand.

### Staging an image

1. On the portal: Software > Instant (AOS-8) > the 8.13.x LSR release > the
   **Draco** image (AP-515), file name `ArubaInstant_Draco_<version>_<build>`.
   Accept the terms and download it; note the SHA-256 shown on the page if any.
2. Copy it to the router, unchanged:
   `scp ArubaInstant_Draco_8.13.1.1_94000 root@10.4.0.1:/cache/gitlab-runner/aruba-iap-firmware/`
   (the CI jobs see that directory as `/cache/aruba-iap-firmware`).
3. Either wait for the weekly `aruba-iap:firmware-check`, or run it now (Run
   pipeline on `main` with `IAP_TASK=firmware-check`). It computes the SHA-256
   and opens the bump MR. If the portal shows a checksum, compare it with the
   one in the MR before merging.

Images are never committed.

### `firmware upgrade`

The command can be run again at any point; it stops when every AP runs the
declared release.

1. Read the cluster (as `status`). Nothing to do when every AP runs the
   declared release.
2. Refuse if fewer APs than `aps:` are in the cluster, an AP already runs a
   newer release, the AP class differs from the image class, an upgrade is in
   process, or auto reboot is off.
3. Check the staged image's size and SHA-256 against `firmware.yml`; refuse a
   file that is missing or differs.
4. Refuse if the configuration differs from `running-config.conf` (drift), so
   the drift check after the upgrade means something. Write the live
   configuration to `--backup-dir`.
5. Start a read-only TFTP server on `--tftp-server`:69 that serves only that
   file (the same server as the NETGEAR upgrade) and send `upgrade-image
   tftp://<server>/<image>` to the controller.
6. Watch `show upgrade info`. An AP reporting a failure before any reboot
   stops the run (the APs keep the old release). When the controller session
   drops, the APs are rebooting.
7. Wait for the controller to answer again, then for every AP to report a
   version (up to about 25 minutes in all).
8. Every AP on the new release: run `drift`. Exit 3 if the configuration
   differs (new defaults, for instance); the cluster keeps the new release.
   Every AP back on its old release: the image did not take; fail. **Mixed**
   releases: run `switch-partition-reboot` on each upgraded AP that holds its
   previous release on the backup partition, wait until every AP runs the old
   release, and fail. An AP that does not come back is not rolled back blind;
   the run fails and says so.

`--dry-run` does steps 1 to 4 and prints the `upgrade-image` command. The TFTP
path is allowed by the router's firewall (UDP 69 from the management VLAN).
`switch-partition-reboot` and the `upgrade-image` syntax are taken from the
cluster's CLI help on 8.13.1.0; the transfer and the rollback path have not been
exercised against the live cluster yet (it runs the newest 8.13 LSR known).

### Firmware updates in CI

- **`aruba-iap:firmware-check`** (weekly pipeline schedule with
  `IAP_TASK=firmware-check`) runs `firmware_ci.py bump-mr`: `check-latest`,
  and if a newer image is staged and no MR for it was opened before, it updates
  `firmware.yml` and `KNOWN_LATEST` in `ci/reports/firmware.py` in an MR
  (`aruba-iap-firmware/<version>`, label `aruba-iap-firmware`) and mails root.
  "Cannot check" with nothing staged exits 3, which the job allows: it shows
  as a warning, every week, until a portal listing exists.
- **Merging the MR is the approval.** `aruba-iap:firmware` runs from a daily
  pipeline schedule with `IAP_TASK=firmware` at 05:20 and only acts inside
  `IAP_FIRMWARE_WINDOW` (default `04:45-06:00` local), outside the 02:15-04:00
  backup window and before the 06:20 drift check. When the cluster is current
  it does nothing. It mails root when it upgraded.
- **One rollout per night.** The NETGEAR, Aruba and S2500 firmware jobs share
  a lock in the runner's cache (`networking/firmware_night.py`). The Aruba
  rollout never runs while another rollout runs, and a scheduled run does
  nothing on a night when `netgear:firmware` already upgraded (or tried to
  upgrade) a switch; it carries on the next night. NETGEAR runs at 04:45, so
  on a night when both have something to do the switches go first.
- **On failure** (refused, image missing or different, upgrade error, fell
  back, rolled back, an AP missing, drift after the upgrade), the job opens an
  issue labelled `aruba-iap-firmware-failed` with the log and mails root. While
  such an issue is open, `aruba-iap:firmware` does nothing. Sort out the
  cluster, export the configuration if the difference is new defaults, and close
  the issue. A timeout or crash that the script could not report is mailed by
  the job's `after_script`.
- The job runs on the `router-firmware` runner (tag `firmware-hostnet`, the
  only runner with host networking; see `base/gitlab-runner/config.toml`), as
  root, so its TFTP server can bind 10.4.0.1:69 (`IAP_TFTP_SERVER`).

### Starting it by hand, or from OpenClaw

OpenClaw gets no AP credentials. It starts the CI job, which reads them from
Vault with the `ci-deploy` role:

```sh
# needs a token allowed to run pipelines on main
curl -sS -X POST -H "PRIVATE-TOKEN: $TOKEN" "https://gitlab.lab.nkontur.com/api/v4/projects/4/pipeline" \
  -H 'Content-Type: application/json' -d '{"ref": "main", "variables": [
    {"key": "IAP_TASK", "value": "firmware"},
    {"key": "IAP_FIRMWARE_DRY_RUN", "value": "1"}]}'
```

The same works from Build > Pipelines > Run pipeline. A pipeline that a person
or OpenClaw starts runs at once, ignoring the window and the one-per-night rule
(it still does nothing while another firmware rollout is running). `IAP_FIRMWARE_DRY_RUN=1` checks
everything without upgrading and opens no issue. Before a real run:

- `main` declares the wanted release (the bump MR is merged) and the image is staged
- no issue labelled `aruba-iap-firmware-failed` is open
- `aruba-iap:drift` is clean: the upgrade refuses a cluster whose configuration differs
- a 5-10 minute Wi-Fi outage for the whole house is acceptable now
