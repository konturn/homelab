# Event router: TLA+ model

`EventRouter.tla` models the event router of
[`../event-routing.md`](../event-routing.md) (sections 4–7 and 13) and is
checked with TLC against the properties of section 12. Noah reviews the
property list in the doc, not the TLA+; this file explains how the two
connect and how to run the checker.

## Run it

```bash
docs/design/tla/run-tlc.sh             # CI profile: seed instances + every mutation (~10 min on 2 threads)
docs/design/tla/run-tlc.sh full        # bigger instances + every mutation (by hand; did not finish in 15 min on the router since phase 3, see 12.6)
docs/design/tla/run-tlc.sh NoLock      # one cfg
```

The script downloads `tla2tools.jar` v1.7.4 (sha256-checked) unless
`TLA2TOOLS_JAR` points at one, and needs Java 11 or newer. It runs TLC at
`nice 19` and `ionice -c3` with `TLC_WORKERS` (default 4) threads and
`TLC_XMX` (default 2g) heap; on the router, keep it at 4 workers or fewer.
CI runs the CI profile (`event-router:tlc`, 2 workers) when anything here
changes, and weekly.

Every check prints `PASS`/`FAIL` with the number of distinct states and the
time; the script exits non-zero if any check fails.

## What is checked

| cfg | Profile | Instance | Checks | Must |
|---|---|---|---|---|
| `ci-safety.cfg` | ci | 4 alerts (`mqtt`, `zig` which needs it, `app` and `appdup` sharing one container), one delivery each | S1–S12 | pass |
| `ci-pages.cfg` | ci | `mqtt`, `zig` and page-class `sec`, a suppression matching `zig` and `sec` | S1–S12 | pass |
| `ci-liveness.cfg` | ci | 2 alerts (`mqtt` without a playbook, `app` with one); OpenClaw may stay down for good | S1–S12 and L1, L3, L5, L7, L8 | pass |
| `ci-fallback.cfg` | ci | the same 2 alerts with Grafana's fallback route modelled; OpenClaw outages end | S1–S12 and L1, L3, L4, L5 | pass |
| `ci-suppress.cfg` | ci | `app` under a suppression | S1–S12 and L1, L3, L5, L6, L7, L8 | pass |
| `safety.cfg` | full | 5 alerts (`ci-safety` plus `sec`), `app` delivered up to twice, suppression on `app` and `sec`, fallback on both | S1–S12 | pass |
| `liveness.cfg` | full | 3 alerts (`mqtt`, `zig`, `app`) | S1–S12 and L1, L3, L5, L7, L8 | pass |
| `mutations/*.cfg` | both | a seed instance; re-delivery bugs use a one-alert instance | the one property named on the `\* expect:` line | fail |

All instances share: 1 run per playbook per rate-limit window, and an
environment budget of one each of: kill-switch flip, OpenClaw outage, router
restart, deploy, maintenance window (quiet or assist), rate-window reset,
suppression and spontaneous alert resolution. Router, executor, forward-queue,
JIT-bot and Grafana steps are weakly fair; deploys end, the router comes back
after a restart, windows and suppressions expire. Three constants select the
phase-3 parts per instance, to keep the state space small:

- `OcRecovers`: OpenClaw outages end (needed for L4). With `FALSE` OpenClaw
  may stay down for good, which is what forces the JIT-bot page and the
  digest expiry (L1).
- `SuppFps`: the alerts a suppression matches (`{}`: no suppressions).
- `FallbackFps`: the alerts whose Grafana fallback route to OpenClaw (the
  OpenClaw transform asking the router's `/ack`) is modelled.

Liveness is checked on smaller instances because TLC's liveness checking
grows with states times fairness conditions (11 per alert plus 5 here). Safety on the larger
ones covers lock contention and re-delivery. Numbers from the last run are
in section 12.6 of the doc.

`-deadlock` is passed because the bounded environment eventually runs out of
events: a quiescent end state is expected, not a bug.

## Reading the spec

| Spec | Doc |
|---|---|
| `Deliver` (dedupe by fingerprint, page class, maintenance, correlation on arrival) | 4.3, 4.7, 13.2 |
| `Correlate`, `ReEvaluate` (continuous correlation; handed-off root causes count while firing) | 4.7, 12.6 (b, e) |
| `Forward`, `Trip`, `StartPb` | 4.4, 7.2, 7.3 |
| `Escalate`, `Queue`, `DeliverFwd`, `JitPage`, `Expire` (forward queue to OpenClaw, JIT-bot page while it is down, digest after 6h) | 7.4, decisions 3 and B |
| `GrafanaOC` (Grafana's OpenClaw route, gated on the router's per-delivery ack) | 7.4, L4 |
| `SuppAdd`, `SuppExpire` | 8.3, L6 |
| `Execute` (executor re-checks kill switch, deploy, window with the playbook's identity, dependencies) | 4.6, 12.6 (a), 13.1 |
| `VerifyOk`, `VerifyFail`, `Escalate` | 4.4, 7.4 |
| `RouterUp` (state persisted) | 4.6 |
| `MaintClose` (recovery check) | 13.3 |

Safety properties over actions (S1–S3, S5–S7, S10, S12) are evaluated when
the action happens and recorded in `viol`; in a correct design `viol`
stays empty, so the check costs no extra states.

## Mutations

Each mutation is one deliberate design bug, selected by `MUT`. If a mutation
stops failing, the spec has become too weak to catch that bug: fix the spec
before trusting a passing base run.

| Mutation | Bug | Fails |
|---|---|---|
| NoLock | no per-target lock | S1 |
| ActionFromEvent | action taken from the event | S2 |
| KillAtStartOnly | kill switch checked only at playbook start | S3 |
| NoRateLimit | no rate limit | S4 |
| IgnoreBreaker | open breaker ignored after the window resets | S5 |
| NoCorrelation | no dependency correlation | S6 |
| CorrelateOnArrivalOnly | correlation only when an event arrives | S6 |
| CorrelateWhileActiveOnly | a root cause handed to OpenClaw stops suppressing its dependents | S6 |
| IgnoreDeploy | deploy lock ignored | S7 |
| DeployAtStartOnly | deploy lock checked only at playbook start | S7 |
| LoseState | incident state not persisted across restarts | S8 |
| IgnoreMaint | maintenance windows ignored | S10 |
| MaintAtStartOnly | window checked only at playbook start | S10 |
| MaintSilencesPage | a window silences a page-class alert | S11 |
| DropEscalation | escalation dropped while OpenClaw is down | L1 |
| DropQueue | queued residue discarded while OpenClaw is down, no JIT page | L1 |
| NoJitPage | pages wait for OpenClaw instead of going out through the JIT bot | L1 |
| NoFallback | Grafana's OpenClaw route skipped although the router never saw the alert | L4 |
| AckByFingerprint | the router acknowledges a fingerprint, not this delivery | L4 |
| ForwardHandled | a playbook-verified incident is also forwarded to OpenClaw | S12 |
| ExecutorIgnoresPlaybook | the executor lets any playbook act in an assist window | S10 |
| NoSuppressionExpiry | suppressions never expire | L6 |
| SuppressPage | a suppression silences a page-class alert | S9 |
| NoLockRelease | lock never released | L5 |
| NoRecovery | no recovery check when a window closes | L8 (and L1) |
| NoExpiry | windows never expire | L7 |
