# OpenClaw identities: `noah` and `ops`

> **Status:** Implemented (rollout in progress, see [Rollout](#rollout))
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
  `ops`.
- No other setuid or setgid file is left in the image, so `node` can only
  ever step down to `ops`, and `ops` (with no-new-privileges) cannot step
  anywhere.

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

## Rollout

1. Broker: per-requester keys, the matrix and tier 3, accepting the old
   shared key as requester `prometheus` (matrix row auto/approve/approve,
   the old behaviour) during the switch.
2. OpenClaw: the two agents, the `ops` user and its key, cron re-pointing,
   the hook bound to `ops`.
3. Remove the shared key and the `prometheus` row.
