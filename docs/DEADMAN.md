# Router heartbeat (dead-man's switch)

Grafana alerting runs in Docker on `/persistent_data`. If that disk, dockerd
or the whole router goes away, the alerting goes with it, and nothing reports
the outage. The heartbeat closes that gap from outside: the router pings a
[healthchecks.io](https://healthchecks.io) check every 5 minutes, and
healthchecks.io emails (or calls) when the pings stop or a failure ping
arrives. Power loss, WAN loss, a hung disk and a dead dockerd all end up as
an alert, without depending on anything in the homelab.

Ansible role: `ansible/roles/deadman` (in `ansible/router.yml`).

## What it checks

`/usr/local/sbin/deadman-check`, run by `deadman-check.timer` every 5 min.
It lives on the root filesystem and runs no container.

| Check | Passes when |
|---|---|
| `persistent_data` | `/persistent_data` is a mountpoint and a temp file can be created and removed |
| `dockerd` | `docker info` answers |
| `container:<name>` | `nginx`, `lab_nginx`, `grafana`, `homeassistant`, `vault`, `gitlab` are running and healthy |
| `grafana` | `/api/health` through lab_nginx reports `database: ok` |
| `vault` | `sys/health` reports `sealed: false` |
| `zpool:mpool` | `zpool list mpool` works and the pool is ONLINE or DEGRADED |

Every check runs under `timeout 20`. A check stuck in uninterruptible I/O is
abandoned after 30s and counts as failed, so the script always finishes.

All pass: success ping. Anything fails: `<ping url>/fail`, with the check
results in the body (shown in the healthchecks.io event log and the email).
The ping is retried 3 times. If the router cannot reach healthchecks.io at
all, no ping arrives, and the check goes down after the grace time.

## Setup in healthchecks.io

Noah creates the account (free plan).

1. Add a check named `router`: **Period 5 minutes, Grace 10 minutes**
   (simple schedule).
2. Integrations: email is on by default for the account address. Optionally
   add a phone integration (Pushover, ntfy, Signal, Telegram, or the mobile
   app) so a 3 a.m. outage is noticed, and enable it for the check.
3. Copy the check's ping URL (`https://hc-ping.com/<uuid>`). It is a
   credential: anyone with it can send pings. Do not commit or paste it.
4. On the router, from the repo checkout, run
   `scripts/set-deadman-ping-url.sh` and paste the URL at the prompt. It
   stores the URL in Vault (`homelab/monitoring/healthchecks`, field
   `ping_url`), writes `/etc/deadman-check.env` (0600), enables the timer and
   runs one check. Later deploys render the same file from Vault.

Until the Vault entry exists, deploys install the script and units but leave
the timer disabled, and print a note saying so.

## Testing

```bash
deadman-check --dry-run                     # run the checks, print results, no ping
systemctl start deadman-check               # one real run (success ping if healthy)
journalctl -u deadman-check -n 20 -o cat    # what it found and sent
DEADMAN_FORCE_FAIL=1 deadman-check          # adds a failing check: sends /fail
deadman-check --force-fail                  # same
systemctl list-timers deadman-check.timer   # next run
```

A forced fail should mark the check down in healthchecks.io and send the
alert. The next normal run (within 5 minutes) sends a success ping and
healthchecks.io reports it back up.

To test the missing-ping path, `systemctl stop deadman-check.timer` and wait
period + grace (15 min); start the timer again afterwards.

## No second, LAN-side watcher

A second heartbeat from another host on the LAN (Mac mini, satellite Pi) was
considered and left out. Those hosts reach the internet through the router,
so they lose their path to healthchecks.io in exactly the cases where the
router's own heartbeat stops (WAN down, router down), and they cannot report
"router up but WAN down" any sooner. Missing pings already alert in that
case. The remaining case, the script itself broken, also shows up as missing
pings. A second check would add setup and noise without new coverage.
