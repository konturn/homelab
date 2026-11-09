# inventory/: physical and dependency facts

The goal: nothing true about the infrastructure that isn't in this repo.
Facts are stored as data, config is generated from that data (so the two
can't disagree), and a scheduled job compares the live router with it.

| File | Holds |
|---|---|
| `hardware.yml` | Disks by controller/slot (model, last 4 of the serial, LUKS mapper and UUID, pool vdev, role), USB controllers, the USB port of each Snapcast zone's sound card, the Monoprice amps (USB-serial adapter, Home Assistant port, inputs and the stream meant to feed each, speaker zones and their HA entities), network devices with no health signal of their own. |
| `dependencies.yml` | The critical chain: per node, what it `needs`, how to probe it, operational `rules`. Addresses are `{{ var }}` references to `ansible/inventory.yml`. |

Hosts, VLAN addresses and the compose services stay where they were
(`ansible/inventory.yml`, `docker/docker-compose*.yml`); these files add only
what had no home.

## Generated from the data

`python3 ci/facts/render.py` writes, and `render.py --check` (CI job
`facts:test`) fails when the committed copy is stale:

| File | From |
|---|---|
| `base/udev/85-my-usb-audio.rules` | `audio_zones` + `usb_controllers` |
| `base/disk/crypttab` | `disks[].luks` (order: boot, persistent_data, then devN by number) |
| `docker/openclaw/workspace/infra/TOOLS.md` | OpenClaw's standing context: the prose in `TOOLS.md.in` next to it, with hosts, VLANs and services (`ansible/inventory.yml`, compose files), these fact files, the auto-merge limits and the `bot_config` rules (`ci/automerge-policy.yml`, read through `botconfig.config()`, the sweep's own parser) and the docs list filled in (`ci/facts/agent_context.py`) |

The generated files are committed and deployed by the existing tasks in
`configure-base` (a plain copy), so a data change shows the exact config diff
in its MR. Don't edit them by hand. (`base/disk/crypttab.py` builds a crypttab
from `lsblk` on the host; use it only to cross-check UUIDs.)

Checked rather than generated (the compose file is a hand-written Ansible
template): `facts:test` fails unless the `homeassistant` service maps exactly
the connected amps' `/dev/serial/by-id` adapters to their `ha_port`.

## Drift check

`facts:drift` runs daily with the BMC drift check (schedule
`SCHEDULED_TASK=ipmi-drift`; by hand: Run pipeline on `main` with
`SCHEDULED_TASK=facts-drift`). `ci/facts/drift.py` sends `collect.py` to the
router over SSH (read-only: lsblk with LUKS UUIDs, `/dev/disk/by-path`, `zpool status`,
`/sys/class/sound`, `docker ps`/`inspect`, `systemctl is-active`, `openclaw cron list`, TCP/DNS
probes) and compares:

- disks: each slot holds the declared model and serial suffix, its LUKS
  mapper is open, data disks are ONLINE in the declared vdev, no undeclared
  disk or pool member, pools ONLINE;
- audio zones: a card named after each zone on the zone's port, no unnamed
  USB sound card, each connected amp's USB-serial adapter present;
- containers: every service in `docker/docker-compose.yml` is running and
  not unhealthy (one-shot init services: exited 0), except services behind
  a compose profile and `snapclient_disabled_zones`;
- services: every node with a probe is up, listed with what depends on it;
- openclaw crons: OpenClaw's cron jobs (`openclaw cron list --all --json` in
  the `openclaw-gateway` container; they live in OpenClaw's own store) match
  `inventory/openclaw-crons.yml`: none missing, none undeclared, and the same
  schedule, enabled flag, agent, session, wake mode, delivery and payload
  settings. Prompt text is never collected or compared. A one-shot (`at`) cron
  that has run and gone is a NOTE. A cron changed on purpose in OpenClaw:
  update its entry in an MR. Drift here always needs hands (living issue);
  nothing changes OpenClaw's crons.

### What happens to drift

Drift is resolved, not filed (`ci/facts/resolve.py`, run with
`drift.py --resolve`):

| Kind | Example | What the job does |
|---|---|---|
| Software: the repo is right | a declared container stopped, a declared systemd unit inactive | Starts it the way the deploy does (`docker compose up -d --no-deps <service>` on the compose file the last deploy rendered, `systemctl start <unit>`), waits, checks again. Only a failed fix is reported. Never for `vault` (comes up sealed) or `gitlab`, nor for an unhealthy container that is still running. |
| A physical change that explains itself | a disk replaced in a bay, a disk moved to another bay, a new disk that is already a healthy pool member or spare, a disk gone from both the bays and the pool | Opens (or refreshes) one MR on `facts/drift-record`, label `facts-record`, assigned to root: `inventory/hardware.yml` updated (untouched entries keep their text) plus every file rendered from it. It is a record, so it is reviewed and merged by hand, never auto-merged. |
| Needs hands | faulted disk, missing pool member, card unplugged or on the wrong port, device or endpoint down, a fix that did not work | ONE living issue, labels `facts-drift` + `needs-hands` (the monthly decision queue): its description is rewritten in place on each run (no notes, so no notification per run), each item keeps the date it was first seen, and the issue is closed when nothing is left. |

Drift does not fail the job, so nothing pages per run; the job fails only
when the check or a GitLab write could not run. It runs in the
`deploy-router` resource group, so it never starts a container while a deploy
is changing them.

`known_issue` on a disk or node is printed next to its drift but does not
silence it. NOTE lines (for example a LUKS device opened under another mapper
name) are shown but are not drift.

On the router itself: `python3 ci/facts/drift.py --local` (report only), or
`--local --resolve --dry-run` to see what the job would do.

## Changing a fact

Hardware moved: fix the hardware, or change the data, run
`python3 ci/facts/render.py`, commit both. A changed udev rule takes effect
for a card when it is next plugged in (or on `udevadm trigger`); a changed
crypttab at the next boot.
