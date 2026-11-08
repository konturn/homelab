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
router over SSH (read-only: lsblk, `/dev/disk/by-path`, `zpool status`,
`/sys/class/sound`, `docker inspect`, `systemctl is-active`, TCP/DNS probes)
and reports:

- disks: each slot holds the declared model and serial suffix, its LUKS
  mapper is open, data disks are ONLINE in the declared vdev, no undeclared
  disk or pool member, pools ONLINE;
- audio zones: a card named after each zone on the zone's port, no unnamed
  USB sound card, each connected amp's USB-serial adapter present;
- containers: every service in `docker/docker-compose.yml` is running and
  not unhealthy (one-shot init services: exited 0), except services behind
  a compose profile and `snapclient_disabled_zones`;
- services: every node with a probe is up, listed with what depends on it.

Drift fails the job (and the pipeline-failure alerting with it) and opens or
updates the `facts-drift` issue; the issue is closed when a run is clean.
`known_issue` on a disk is printed next to its drift but does not silence it.
NOTE lines (for example a LUKS device opened under another mapper name) are
shown but don't fail the job.

On the router itself: `python3 ci/facts/drift.py --local`.

## Changing a fact

Hardware moved: fix the hardware, or change the data, run
`python3 ci/facts/render.py`, commit both. A changed udev rule takes effect
for a card when it is next plugged in (or on `udevadm trigger`); a changed
crypttab at the next boot.
