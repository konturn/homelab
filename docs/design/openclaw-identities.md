# OpenClaw identities: `noah` and `ops`

> **Status:** Implemented (2026-11-16)
> **Date:** 2026-11-16
> **Related:** `docker/jit-approval-svc/README.md`, `docker/openclaw/`,
> [event-routing.md](./event-routing.md), [jit-credential-proxy.md](./jit-credential-proxy.md),
> [AUTOMERGE.md](../AUTOMERGE.md) (bot-config class)

## Summary

OpenClaw runs two agents instead of one, and each has its own identity at the
JIT broker:

| Agent | Bound to | Runs as | JIT requester |
|---|---|---|---|
| `noah` | Noah's Telegram DMs only | `node` (the gateway's user), full tools | `openclaw-noah` |
| `ops` | every cron and the event-router hook | `ops`, a separate OS user in the gateway container | `openclaw-ops` |

The broker identifies the requester from the key it presents, never from a
field the agent writes, and decides each request from a requester × tier
matrix. Tiers stay resource-based; the old tier 2 is split so that a root
shell on the router, elevated SSH and IPMI are a new tier 3.

## Why

The crons and the event-router hook read untrusted input all day: alert text,
mail, web pages, job listings, MR and issue text, iMessages, advisories. Any
of it can carry instructions. Before the split those runs had the same
identity, tools and broker privileges as Noah's own conversation, so a
successful injection in a 3 a.m. cron run could ask for anything Noah's agent
could.

A Telegram binding authenticates the **requester**, not the **content**: a
message in Noah's DM is from Noah, so the `noah` agent may act on it with
more trust. A cron prompt is from the scheduler, and what the cron then reads
is from anyone. So the agent that reads untrusted input on a schedule gets a
smaller identity, and anything bigger has to go through Noah.

## Approval matrix

| Requester | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| `openclaw-noah` | auto | auto, plus one FYI Telegram line per grant (no buttons) | approve (tap) |
| `openclaw-ops` | auto | approve (tap) | **deny** |
| anyone else | deny | deny | deny |

- Tier 3 is never minted without a tap, for anyone. The broker refuses a
  configuration that says otherwise, and a property test checks it.
- `deny` means a 403 before anything is stored: there is no pending request a
  button could approve.
- The matrix is broker config (`JIT_APPROVAL_MATRIX` in
  `docker/docker-compose.yml`, default in `internal/config/requesters.go`).
- TTLs, rate limits (per resource per requester), the tier-1
  `gitlab-moltbot` entry and the broker-only paths are unchanged.

## Tiers

| Tier | Resources |
|---|---|
| 1 | grafana, influxdb, plex, radarr, sonarr, prowlarr, ombi, nzbget, deluge, tautulli, mqtt, gmail-read, ssh-macmini, gitlab-moltbot |
| 2 | gitlab, homeassistant, paperless, gmail-send, tailscale, pihole, vault (scoped paths), ssh-satellite, ssh-zwave, ssh-nkontur, ssh-konoahko, ssh-konturn |
| 3 | ssh-router, ssh-router-elevated, ssh-zwave-elevated, ssh-satellite-elevated, ssh-macmini-elevated, ipmi |

### Deny list for `ops` (tier 3)

- `ssh-router`: a shell on the host that routes every VLAN and runs every
  container. The restricted `claude` user there is defence in depth, not a
  boundary.
- every `ssh-*-elevated`: sudo on that host.
- `ipmi`: power and console of the server, below the OS.
- Vault admin or policy paths: none is reachable through the `vault`
  resource at any tier. It grants read/list/create/update under
  `homelab/data/` only, and refuses the broker-only paths (which now include
  each agent's own key). If a policy-writing path is ever added, it belongs
  in tier 3.

A request below a resource's tier is raised to it, so asking for
`ssh-router` at tier 2 is a tier-3 request.

`ssh-macmini` stays tier 1 (unchanged): the iMessage cron uses it every 30
minutes.

## Identities and credentials

Each requester has its own random key at `homelab/agents/openclaw-<name>`
(field `api_key`). The broker holds only their SHA-256 hashes
(`JIT_REQUESTER_KEYS`). A request may still name its requester, but only its
own: naming another one is refused. `/status/<id>` answers only the requester
that made the request.

Where each key lives:

- `openclaw-noah`: read from Vault at use time with the gateway's AppRole,
  whose credentials are in the gateway process environment (user `node`).
- `openclaw-ops`: a file deployed to the `ops` user's home, mode 0600, owned
  by `ops`. The `ops` agent has no Vault credential at all.

The key paths are broker-only for the `vault` resource, so a tier-2 grant can
never hand one agent another's key.

### How `ops` is kept apart

OpenClaw's own sandbox and tool policy do not cover this deployment's model
backend: every turn runs the Claude Code CLI, and the CLI's built-in tools
(shell, file reads) run in the CLI's process. So the boundary is the OS user:

- `cliBackends.claude-cli.command` is `openclaw-claude`, a small dispatcher.
  For any agent except `ops` it runs `claude` unchanged. For `ops` it starts
  `claude` as user `ops` through a setuid-`ops` copy of `setpriv`
  (`--nnp`, parent-death signal, environment rebuilt from an allowlist), so
  nothing of the gateway's environment (gateway, hook and Telegram tokens,
  the AppRole) reaches it.
- `/home/node/.openclaw` (config, sessions, credentials, the `noah`
  workspace) is mode 0700 and owned by `node`; process environments are
  readable only by their own user. `ops` has its own home and workspace,
  readable by the gateway so OpenClaw can load its bootstrap files.
- `ops` does not get OpenClaw's gateway tools over the loopback MCP bridge.
  Those tools run inside the gateway as `node`, so giving them to `ops` would
  undo the separation. The `ops` agent works with the CLI's own tools, as
  `ops`. Its OpenClaw tool policy denies the gateway tools as well, except
  `exec`: cron trigger scripts run with the job's agent policy, and the
  `imessage-check` trigger (written through the `noah` agent's `cron` tool;
  `ops` has none) runs a node-owned check script with it.
- No other setuid or setgid file is left in the image, so `node` can only
  ever step down to `ops`, and `ops` (with no-new-privileges) cannot step
  anywhere.
- The direction of trust is one way. `node` may read and write `ops`'s
  workspace (it is the more trusted side), but nothing running as `node`
  executes a file `ops` can write: the noah agent, the cron trigger and the
  llm-shadow trial run their own (node-owned) copies of shared scripts and
  only read `ops`'s data files.

Both agents use the same GitLab identity (`moltbot`, tier 1), so both can
open MRs. What can merge without review is decided by the bot-config rules on
the diff (AUTOMERGE.md), which do not depend on which agent wrote it; any
path with `openclaw` in it is on that class's never-list.

## Talking to Noah, and handing off

`ops` reaches Noah only through the existing paths: a cron's announce
delivery to his Telegram, the morning digest, and the event router. It has
no tool to message the `noah` agent or start a run as `noah`.

When an `ops` run needs more than it has (a tier-2 grant Noah does not
approve, any tier-3 resource, or anything that needs the `noah` workspace),
it says so in its reply as a visible request: what it needs, why, and the
exact command or request it would make. Noah decides and, if he agrees, asks
his own agent to do it. It never retries with a different resource or tier to
get around a refusal.

## Shared state

Some state moves with the jobs that write it, into the `ops` workspace:

- `skills/imessage-plex/state.json` and the iMessage checkpoint. Noah's
  "approve <title>" replies still go to `noah`, whose copies of the Plex
  scripts point at that state file.
- the job-hunting queues, `memory/main-pipeline-state.json`,
  `memory/major-research-state.json`, `memory/routing-review-latest.md`, the
  red-team reports.
- `reports/updates/` (host-written reports) is mounted into both
  workspaces; the vulnwatch outbox and context live in `ops`'s workspace
  (host override in `/etc/vulnwatch/local.yml`).
- The llm-shadow trial reads the job's state and transcripts from `ops`'s
  home and still executes the node-owned scripts; it writes a copy of its
  summary for `ops`'s digest.

## Crons

Every agent cron runs as `ops` (`inventory/openclaw-crons.yml`) except:

| Cron | Stays on | Why |
|---|---|---|
| `nightly-meditation` | `noah` | Writes the agent's own identity notes (`SOUL.md`, `IDENTITY.md`, `memory/meditations.md`), which load into every `noah` session. |
| `fact-extraction` | `noah` | Reads Noah's conversation memory and writes his knowledge base (`life/`), both `noah`-only data. |
| `weekly-knowledge-synthesis` | `noah` | Rewrites `MEMORY.md` and `life/` summaries, which load into every `noah` session. |
| `cron-config-sync` | `noah` | Exports the gateway's cron store, which is in `node`'s state directory. |
| `skill-review` | `noah` | Edits the shared skills that `noah` loads. |
| `schedule-failures` | none (command job) | Runs two fixed scripts in the gateway; no model, no untrusted input. |

These read only `noah`'s own files, so they do not widen what untrusted input
can reach. Letting `ops` write those files would let a poisoned cron run
plant instructions for the more trusted agent.

## Keeping tier 3 rare: router chores

In the 30 days before the split (2026-10-17 to 2026-11-16) the agent asked
for `ssh-router` or `ssh-router-elevated` 28 times; Noah approved 2 (both
were things he had asked for), the rest lapsed. Almost all came from alert
triage:

| Chore | Requests | Better path |
|---|---|---|
| Read a container's state, health or logs (pihole, tautulli, grafana, zigbee2mqtt, influxdb, gitlab, deluge) | 11 | Loki (`container` label) and InfluxDB (`docker_container_*`), tier 1 |
| Read host state: `zpool status`, kernel OOM lines, a systemd unit's status, I/O load | 9 | Loki (`syslog_identifier`), InfluxDB (`systemd_units`, `zfs*`, `diskio`), tier 1; `zpool status` itself is not exported |
| Restart a hung container (plex, 3 times for one incident) | 3 | executor `restart_container` |
| Diff a live config file against the repo | 2 | read-only collect |
| Clear a failed systemd unit (`reset-failed`) | 1 | executor action |
| One-off host edits (stray cron entry, emergency nginx 503) | 2 | stay tier 3 |

Done now: `ops`'s `AGENTS.md` says reading is tier 1 and where to read.

Proposed, not built (each is a new path from an agent into the root
executor and wants its own reviewed design, `docs/design/event-routing.md`
4.5 already lists the types):

1. `collect` on request: `POST /collect {incident, target}` on the event
   router, allowed clients only, runs fixed read-only commands through the
   executor (container state and health without the environment, last 200
   log lines, `zpool status`, `systemctl status` of an allowlisted unit) and
   returns the text. Covers about 20 of the 28.
2. `restart_container` on request: the same endpoint, same executor
   re-checks as a playbook (denylist, once per incident, rate limit, deploy
   lock), tied to an open incident. Covers the restarts.
3. `reset_failed_unit` for an allowlist of units (`nut-driver@*`,
   `snapclient@*`).

## Rollout

1. Broker: per-requester keys, the matrix and tier 3, accepting the old
   shared key as requester `prometheus` (matrix row auto/approve/approve,
   the old behaviour) during the switch.
2. OpenClaw: the two agents, the `ops` user and its key, cron re-pointing,
   the hook bound to `ops`.
3. Remove the shared key and the `prometheus` row. The broker now refuses
   to start if `JIT_API_KEY` is set.
