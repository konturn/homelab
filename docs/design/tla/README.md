# Event router: TLA+ model

`EventRouter.tla` models the event router of
[`../event-routing.md`](../event-routing.md) (sections 4–7 and 13) and is
checked with TLC against the properties of section 12. Noah reviews the
property list in the doc, not the TLA+; this file explains how the two
connect and how to run the checker.

## Run it

```bash
docs/design/tla/run-tlc.sh             # CI profile: seed instances + every mutation (~2-4 min)
docs/design/tla/run-tlc.sh full        # bigger instances + every mutation (~10 min, by hand)
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
| `ci-safety.cfg` | ci | 5 alerts (`mqtt`, `zig` which needs it, `app` and `appdup` sharing one container, page-class `sec`), one delivery each | S1–S11 | pass |
| `ci-liveness.cfg` | ci | 2 alerts (`mqtt` without a playbook, `app` with one) | S1–S11 and L1, L3, L5, L7, L8 | pass |
| `safety.cfg` | full | as `ci-safety`, `app` delivered up to twice | S1–S11 | pass |
| `liveness.cfg` | full | 3 alerts (`mqtt`, `zig`, `app`) | S1–S11 and L1, L3, L5, L7, L8 | pass |
| `mutations/*.cfg` | both | a seed instance; re-delivery bugs use a one-alert instance | the one property named on the `\* expect:` line | fail |

Both instances share: 1 run per playbook per rate-limit window, and an
environment budget of one each of: kill-switch flip, OpenClaw outage, router
restart, deploy, maintenance window, rate-window reset and spontaneous alert
resolution. Router and executor steps are weakly fair; deploys end, the router
comes back after a restart and windows expire.

Liveness is checked on smaller instances because TLC's liveness checking
grows with states times fairness conditions (35 here). Safety on the larger
ones covers lock contention and re-delivery. Numbers from the last run are
in section 12.6 of the doc.

`-deadlock` is passed because the bounded environment eventually runs out of
events: a quiescent end state is expected, not a bug.

## Reading the spec

| Spec | Doc |
|---|---|
| `Deliver` (dedupe by fingerprint, page class, maintenance, correlation on arrival) | 4.3, 4.7, 13.2 |
| `Correlate`, `ReEvaluate` (continuous correlation) | 4.7, 12.6 (b) |
| `Forward`, `Trip`, `StartPb` | 4.4, 7.2, 7.3 |
| `Execute` (executor re-checks kill switch, deploy, window, dependencies) | 4.6, 12.6 (a) |
| `VerifyOk`, `VerifyFail`, `Escalate` | 4.4, 7.4 |
| `RouterUp` (state persisted) | 4.6 |
| `MaintClose` (recovery check) | 13.3 |

Safety properties over actions (S1–S3, S5–S7, S10) are evaluated when the
executor runs an action and recorded in `viol`; in a correct design `viol`
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
| IgnoreDeploy | deploy lock ignored | S7 |
| DeployAtStartOnly | deploy lock checked only at playbook start | S7 |
| LoseState | incident state not persisted across restarts | S8 |
| IgnoreMaint | maintenance windows ignored | S10 |
| MaintAtStartOnly | window checked only at playbook start | S10 |
| MaintSilencesPage | a window silences a page-class alert | S11 |
| DropEscalation | escalation dropped while OpenClaw is down | L1 |
| NoLockRelease | lock never released | L5 |
| NoRecovery | no recovery check when a window closes | L8 (and L1) |
| NoExpiry | windows never expire | L7 |
