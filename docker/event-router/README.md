# event-router

Deterministic first responder for Grafana alerts and GitLab pipeline events
([docs/design/event-routing.md](../../docs/design/event-routing.md)). It
dedupes alerts by fingerprint, correlates them through
`inventory/dependencies.yml`, and runs **playbooks**: one allowlisted action
and a verification step, with escalation if either fails. In phase 3
(`openclaw.forward: true` in `config.yml`) it also hands everything it did
not handle to OpenClaw itself (see "Residue to OpenClaw" below); with
`forward: false`, OpenClaw and email get every alert straight from Grafana
as in phases 1–2 and the router only adds to that.

```
Grafana ──webhook──▶ event-router (container, uid 2470, no Docker socket)
                       │ decision log: JSON on stdout → Loki {container="event-router"}
                       │ SQLite: /persistent_data/application/event-router/data/router.db
                       ▼ Unix socket /run/event-router/executor.sock (0660, group event-router)
                     event-router-executor (host, root, socket-activated)
                       docker restart <service> | GitLab: cancel job
event-router-control (host, root): Telegram kill switch + messages, via the JIT bot
```

## Modes and the kill switch

The effective mode is `notify-only`, `shadow` or `active`. A playbook acts
only when both the router and the playbook are `active`. In `shadow`, the
router logs `decision: shadow` with the action it would have run and never
calls the executor.

| Source | Where | Notes |
|---|---|---|
| Repo | `config.yml` `mode` | the deployed default |
| Host override | `/etc/event-router/override/mode` | wins over the repo, either direction; `echo notify-only > ...` stops everything |
| Telegram | "Stop automation" / "Resume" buttons on the JIT bot's "Event router" message | Stop writes the host override; Resume removes it (and any router override) |
| Router override | `data/shared/runtime_mode` | set by the router itself on an automation storm (more than 6 actions/hour); can only lower the mode |

Every change of the effective mode is sent to Noah on Telegram, whoever made
it, and again every 24h while an override is set (decision 13). The executor
re-reads all three sources on every request.

## Playbooks

`playbooks/*.yml`, one per file; the schema is in `router/config.py`
(`Playbook`), and CI rejects anything else (`python -m router validate`):

- `match`: `source` (`grafana`, `reconciler`, `gitlab`), `kind`, `rule_uid`
  (the Grafana rule uid), optional exact `labels`. One event matches at most
  one playbook.
- `node`: a `dependencies.yml` node, or `{from_label: container_name}`.
- `action`: `restart_container`, `cancel_ci_job` or `retry_ci_job` only; the target is fixed or
  comes from a label or event field validated as a plain name. Hard-denied
  actions and targets (power, locks, HA, router reboot, firewall, deletes,
  zpool, Vault, GitLab/Vault/Pi-hole restarts, ...) are constants in the code
  and in the executor, and are not expressible.
- `preconditions` (optional): `no_deploy_touching: 2h`, checked by the executor.
- `verify` (mandatory): `alert_resolved: 10m` (Grafana alerts),
  `ci_job_finished: 2m` or `ci_job_succeeded: 75m` (the retried job).
- `limits` (mandatory): `max_runs` per `per` for each target, plus `cooldown`.
  When a limit is exceeded, the playbook's breaker opens: the incident pages
  and the playbook stops until 24h pass with no triggers.
- `mode`: `shadow` (default) or `active`. Promotion to `active` is its own MR
  (decision 10).
- `on_failure`: `page` (default), `agent` (residue for OpenClaw) or `digest`.

| Playbook | Alert | Action |
|---|---|---|
| `zigbee-silence-restart` | `zigbee-radio-silence` | restart zigbee2mqtt (executor first probes coordinator, MQTT, DNS) |
| `restart-once` | `container-health-failure` | restart the unhealthy container once, not on the denylist, not ephemeral (`runner-*`, `canary-*`, `e2e-*`), not within 2h of a deploy |
| `ci-orphan-cancel` | reconciler, every 5 min | cancel a `deploy-router` job running past its timeout or started before GitLab came back |
| `ci-orphan-retry` | after `ci-orphan-cancel` verified | retry the cancelled job if nothing newer superseded it (shadow) |
| `ci-system-retry` | GitLab job event: runner/system failure on `main` | retry the job once (shadow) |

The router sends the playbook id with every request; the executor acts only
for a playbook in its own list (`executor.json`, rendered from these files at
deploy time) that is `active` and allowed that action and target. That is
what lets an `assist` maintenance window allow its listed playbooks. Host-side
preconditions are checked there too: `no_deploy_touching: 2h` refuses a
restart of a container (re)created less than 2h ago, since compose recreates
exactly the services a deploy changed.

## Residue to OpenClaw (phase 3)

What reaches OpenClaw, through its `/hooks/event-router` mapping
(`docker/openclaw/hooks/transforms/event-router.js`), one item per incident:

| Kind | When |
|---|---|
| residue | no playbook matched; the kill switch is on; a shadow playbook would have acted; the executor refused (a deploy recreated the container, the job is not orphaned or was superseded); a burst; a failed retry of a CI job; a failed `main` or scheduled pipeline |
| page | a page class (`page_classes` in `config.yml`), or a failed remediation: verification failed, a breaker opened, the action failed, a dependency was down |

Never: an incident a playbook handled (decision B; it is in the decision log
and the weekly summary), a digest-only event (`digest_only`), or one matching
an active suppression (`suppressions.yml`).

- **OpenClaw down.** Items stay queued and are retried (oldest first, backing
  off to 5 minutes). A page that OpenClaw has not taken after 2 minutes also
  goes to Noah through the JIT bot (`event-router-control`, decision 3).
  Items older than 6h expire into the digest (doc 7.4).
- **Quiet hours** (23:00–08:00, decision 6). Non-urgent pages are held until
  08:00, and dropped to the digest if the alert cleared overnight. Residue
  is delivered at once, flagged `quiet_hours`: OpenClaw works on it but does
  not message Noah.
- **Router down (L4).** Grafana still sends every alert to OpenClaw's
  `/hooks/grafana`. That transform asks the router `GET /ack?fp=...`; it
  stays silent only for alerts the router has accepted, so an alert the
  router never saw (it was down, or its delivery to OpenClaw is being
  rejected) reaches OpenClaw that way.

OpenClaw files an **outcome record** for every escalation it handles
(`POST /outcome`, schema in `router/outcomes.py`): root cause, fix (an
allowlist entry or nothing), fix kind, whether it is playbook-able. The weekly
`routing-review` cron reads `/summary?days=7` and `/decisions?days=7`.

## GitLab events

A project webhook on `root/homelab` (4) and `root/homeassistant` (14), pipeline
and job events, to `http://event-router:8080/gitlab` with the secret token
from Vault (`homelab/docker/event-router`, `gitlab_webhook_token`). GitLab
reaches the container because `event-router` is on its outbound local-request
allowlist (Admin, Network, "Local IP addresses and domain names that hooks
and integrations can access"). Both hooks and the allowlist entry are enforced and
drift-checked by `gitlab:settings` ([docs/GITLAB_SETTINGS.md](../../docs/GITLAB_SETTINGS.md)).

| Event | Becomes |
|---|---|
| pipeline on `main` failed | `pipeline_failed`, one incident per project, source and scheduled task while red; residue (OpenClaw starts the main-pipeline-fix procedure at once) |
| only `<host>:deploy-service` jobs failed | digest (the auto-merge sweep already rolled back) |
| pipeline on `main` succeeded | resolves the matching `pipeline_failed` incident |
| job on `main` failed with a runner/system reason | `ci_job_failed`: `ci-system-retry` retries it once (never a script failure) |

## Operating it

```bash
scripts/event-router mode show
scripts/event-router maint open --scope zwave_js --for 2h --reason "kernel update"
scripts/event-router maint list
scripts/event-router maint close maint-1234abcd
# From Telegram, through OpenClaw (parsed by the router, router/maint_text.py):
#   /maint zwave_js 2h kernel update  |  /maint list  |  /maint close maint-1234abcd
curl -s -H "Authorization: Bearer $CONTROL_TOKEN" http://event-router:8080/summary?days=7
```

The decision log, in Grafana Explore (Loki):
`{container="event-router"} | json | decision != "deduped"`.

Executor requests and refusals go to the journal:
`journalctl -u 'event-router-executor@*' -o cat`.

Maintenance windows (doc section 13) last at most 12h and need a reason. Alerts for nodes
in scope (and for their dependents) are recorded but not acted on. When the
window closes, anything still failing re-enters normal handling. Page classes
still page unless the window names them.

## Tests

```bash
cd docker/event-router
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
python -m router validate --config config.yml --playbooks playbooks \
  --deps ../../inventory/dependencies.yml \
  --rules ../grafana/provisioning/alerting --compose ../docker-compose.yml \
  --suppressions suppressions.yml
```

`tests/test_properties.py` checks the TLA+ properties
([docs/design/tla](../../docs/design/tla/README.md)) against the real engine
with Hypothesis: S1–S11 when each action would run, and L1, L5, L7 and L8
once the environment calms down. A second machine adds OpenClaw going up and
down with forwarding on: a handled incident is never forwarded (decision B),
one forward per incident, and every escalation is delivered or, after 6h,
expired into the digest, with a page always sent through the JIT bot first.
