# Definition of done

The homelab is **done** when it maintains itself. Done does not mean nothing changes. It means every change is either automated, a one-off that needs hands or money, or a project picked on purpose for fun. Software work that doesn't move one of the criteria below is a hobby project, and it gets labelled as one.

## Criteria

| # | Criterion | Mechanism | Status (2026-11-19) |
|---|---|---|---|
| 1 | **Everything is in inventory and watched.** Each host and device is in `inventory/hardware.yml`, and the drift jobs find nothing unexplained. | `ci/facts/` (facts-drift), iot-device-drift, ipmi-drift, s2500-drift, ups-card-drift | Met |
| 2 | **Every alert leads somewhere.** Each Grafana rule either has an event-router playbook that fixes it, or a page class with an email route. Nothing just fires into the void. | `docker/grafana/provisioning/alerting/`, `docker/event-router/playbooks/`, `page_classes` in `docker/event-router/config.yml`, `policies.yml` | Mostly met: `docker/event-router/tests/test_repo.py` checks that page classes match the email routes, but nothing checks that every rule has a playbook or page class |
| 3 | **Someone is watching the watcher.** If the router, the disk or Grafana dies, an alert still comes from outside the lab. | `docs/DEADMAN.md` | Met |
| 4 | **Backups are proven restorable, not just taken.** restic runs on a schedule with staleness and error alerts, and a scheduled restore drill restores a sample and checks it. | `docs/backups.md`, Restic alerts in `infrastructure.yml` | **Gap:** no automated restore drill |
| 5 | **Secrets rotate themselves.** Every rotatable credential is on the monthly rotation. The exceptions are written down along with the reason. | `docs/SECRET_ROTATION.md`, `secrets:rotate-proxied` (monthly) | Met; exceptions listed there |
| 6 | **Updates close their own loop.** Renovate and HA maintenance either merge or install the safe classes themselves, and hold the risky ones with a reason. A held update doesn't sit unanswered. | `docs/RENOVATE.md`, `docs/AUTOMERGE.md`, `ci/ha-maintenance/`, firmware/os/app reports | Mostly met |
| 7 | **Config lives in git.** HA entities, amp inputs, Grafana and the event router are reconciled from the repo, and UI drift comes back as an MR. | `ci/ha-entities/` (ha-entities-drift), provisioning, ansible | Met for the reconciled surfaces |
| 8 | **No pileup.** No open MR is older than 7 days, and an MR opened by an automation or agent is merged or closed by whoever opened it. | automerge, review habits | **Gap:** nothing checks MR age |
| 9 | **Open issues need hands or money.** Every open issue is blocked on a physical task, a purchase or a decision by Noah. If software alone could close it, it shouldn't be sitting open. | GitLab issues | Mostly met |
| 10 | **Recovery is written down.** From bare metal, the lab can be rebuilt from git + Vault + B2 by following a doc. | `docs/DISASTER_RECOVERY.md` | Met on paper; never drilled end to end |

## Rules for new work

1. **Name the criterion.** Every new project or MR says which criterion it serves or closes. If it serves none, it gets the `hobby` label. Hobby work is fine, but it isn't maintenance, and it doesn't count as progress toward done.
2. **New things pay their way in.** A new device or service lands with: an inventory entry, monitoring, an alert route (a playbook or a page class), a backup decision (backed up, or "no state" written down) and an update path. Without all of these it isn't finished, even if it works.
3. **Retiring counts as progress.** Removing a device or service, and every trace of it in inventory, DNS/DHCP, HA, dashboards and alerts, reduces surface area. Prefer that to keeping dead entries marked "offline".
4. **A gap becomes automation, not a chore.** When a criterion slips, the fix is a job or playbook that keeps it from slipping again, not a reminder for Noah.

## When it's done

When every row says Met, the lab's steady state is: automations merge and install the routine changes, pages arrive only for things that need hands or money, and the only work left is what Noah chooses to take on.
