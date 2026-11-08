# event-router

Deterministic first responder for Grafana alerts
([docs/design/event-routing.md](../../docs/design/event-routing.md)). It
dedupes alerts by fingerprint, correlates them through
`inventory/dependencies.yml`, and runs **playbooks**: one allowlisted action
and a verification step, with escalation if either fails. OpenClaw and
email still get every alert straight from Grafana (phases 1–2). The router
only adds to that.

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

- `match`: `source` (`grafana`, `reconciler`), `kind`, `rule_uid` (the Grafana
  rule uid), optional exact `labels`. One event matches at most one playbook.
- `node`: a `dependencies.yml` node, or `{from_label: container_name}`.
- `action`: `restart_container` or `cancel_ci_job` only; the target is fixed or
  comes from a label or event field validated as a plain name. Hard-denied
  actions and targets (power, locks, HA, router reboot, firewall, deletes,
  zpool, Vault, GitLab/Vault/Pi-hole restarts, ...) are constants in the code
  and in the executor, and are not expressible.
- `verify` (mandatory): `alert_resolved: 10m` or `ci_job_finished: 2m`.
- `limits` (mandatory): `max_runs` per `per` for each target, plus `cooldown`.
  When a limit is exceeded, the playbook's breaker opens: the incident pages
  and the playbook stops until 24h pass with no triggers.
- `mode`: `shadow` (default) or `active`. Promotion to `active` is its own MR
  (decision 10).

| Playbook | Alert | Action |
|---|---|---|
| `zigbee-silence-restart` | `zigbee-radio-silence` | restart zigbee2mqtt (executor first probes coordinator, MQTT, DNS) |
| `restart-once` | `container-health-failure` | restart the unhealthy container once, not on the denylist, not within 2h of a deploy |
| `ci-orphan-cancel` | reconciler, every 5 min | cancel a `deploy-router` job running past its timeout or started before GitLab came back |

## Operating it

```bash
scripts/event-router mode show
scripts/event-router maint open --scope zwave_js --for 2h --reason "kernel update"
scripts/event-router maint list
scripts/event-router maint close maint-1234abcd
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
  --rules ../grafana/provisioning/alerting --compose ../docker-compose.yml
```

`tests/test_properties.py` checks the TLA+ properties
([docs/design/tla](../../docs/design/tla/README.md)) against the real engine
with Hypothesis: S1–S11 when each action would run, and L1, L5, L7 and L8
once the environment calms down.
