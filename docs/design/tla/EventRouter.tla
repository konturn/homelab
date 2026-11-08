---------------------------- MODULE EventRouter ----------------------------
(***************************************************************************)
(* Model of the homelab event router (docs/design/event-routing.md).       *)
(*                                                                         *)
(* Actors: Grafana (delivers alerts, possibly duplicated, possibly while   *)
(* the router is down; its email route and its fallback route to OpenClaw  *)
(* do not depend on the router), the router (dedupe, correlation,          *)
(* matching, maintenance, suppressions, escalation through a forward       *)
(* queue), the executor (re-checks mode, windows and the playbook's        *)
(* identity, runs the one allowed action), OpenClaw (may be down), the     *)
(* JIT bot (independent paging path) and the environment (kill switch,     *)
(* deploys, maintenance windows, suppressions, rate-limit windows,         *)
(* restarts).                                                              *)
(*                                                                         *)
(* Environment actions have small budgets so the state space is finite;    *)
(* router and executor actions are weakly fair.                            *)
(*                                                                         *)
(* MUT selects one deliberate design bug (section 12.4 of the doc); with   *)
(* MUT = "none" every property must hold, with any other value at least    *)
(* the named property must fail. run-tlc.sh checks both; README.md maps    *)
(* the spec to the doc and to the implementation.                          *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS MUT,           \* "none" or one deliberate design bug (section 12.4)
          MaxRuns,       \* playbook runs per rate-limit window
          MaxDeliveries, \* deliveries of an alert in Redeliver (others: 1)
          Redeliver,     \* alerts Grafana may re-send / re-fire
          Fps,           \* alerts in this instance, a subset of AllFps
          OcRecovers,    \* TRUE: an OpenClaw outage always ends (fairness)
          SuppFps,       \* alerts a suppression (phase 3, L6) would match
          FallbackFps,   \* alerts whose Grafana fallback route (L4) is modelled
          Delivery       \* model the forward queue, OpenClaw outages and the
                         \* delivery history (liveness); FALSE for the safety
                         \* checks, which none of it can affect

\* ---- Topology (a small instance of inventory/dependencies.yml) ---------
\* Alerts (fingerprints). "app" and "appdup" are different alerts about the
\* same container, each with its own playbook: they contend for one target.
AllFps    == {"mqtt", "zig", "app", "appdup", "sec"}
NodeOf    == [f \in AllFps |-> CASE f = "mqtt"   -> "mqtt"
                              [] f = "zig"    -> "zigbee"
                              [] f = "app"    -> "app"
                              [] f = "appdup" -> "app"
                              [] f = "sec"    -> "sec"]
Needs     == [n \in {"mqtt", "zigbee", "app", "sec"} |->
                 IF n = "zigbee" THEN {"mqtt"} ELSE {}]
PageClass == {"sec"}                      \* always pages (security etc.)
Playbooks == {"pz", "pa", "pa2"}
NoPb      == "none"
PbOf      == [f \in AllFps |-> CASE f = "zig"    -> "pz"
                              [] f = "app"    -> "pa"
                              [] f = "appdup" -> "pa2"
                              [] OTHER        -> NoPb]
TargetOf  == [p \in Playbooks |-> IF p = "pz" THEN "zigbee2mqtt" ELSE "app"]
Targets   == {"zigbee2mqtt", "app"}
Allowed   == {"restart_container"}
Denied    == {"power_off"}

Active    == {"open", "symptom", "acting", "verifying", "maint"}
Terminal  == {"resolved", "escalated", "paged", "suppressed"}
Free      == "free"

VARIABLES
  inc,        \* fp -> incident status ("none" or Active or Terminal or "dropped")
  deliv,      \* fp -> deliveries so far (bounded)
  execN,      \* fp -> executions in the current episode
  lock,       \* target -> fp holding it, or Free
  runs,       \* playbook -> runs in the current rate-limit window
  breaker,    \* playbook -> TRUE when its circuit breaker is open
  viol,       \* safety properties violated by some executed action
              \* (evaluated at the moment the action runs; stays {}
              \* in a correct design, which keeps the state space small)
  mode,       \* kill switch: "active" | "notify"
  ocUp,       \* OpenClaw reachable
  routerUp,   \* router process up
  deploying,  \* a deploy holds the host's resource group
  maintOn,    \* a maintenance window is open
  assist,     \* the open window is an `assist` window (lists AssistListed)
  pages,      \* fps paged to Noah through the JIT bot (decision 3)
  agentq,     \* fps delivered to OpenClaw (by the router or Grafana's fallback)
  fwdq,       \* the router's forward queue to OpenClaw (residue, page context)
  pageq,      \* queued items that are pages (kept with the item: a resolve
              \* must not turn a queued page into residue that can expire)
  digest,     \* queued residue that expired into the digest (7.4)
  email,      \* page-class alerts Grafana emailed (decision 4)
  acked,      \* fps whose latest delivery the router acknowledged (/ack)
  unseen,     \* ground truth: fps whose latest delivery the router missed
  gq,         \* Grafana's pending fallback notifications to OpenClaw
  suppOn,     \* a suppression (reason + expiry) is active
  budget      \* remaining environment events

vars == <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
          routerUp, deploying, maintOn, assist, pages, agentq, fwdq, pageq, digest,
          email, acked, unseen, gq, suppOn, budget>>
qv == <<fwdq, pageq, digest>>             \* the forward queue
gv == <<email, acked, unseen, gq>>        \* Grafana's side
wv == <<maintOn, assist>>                 \* the maintenance window

\* The window covers the radio stack, the app container and, to exercise
\* S11, the node of a page-class alert (the window does not name the page
\* class explicitly). An `assist` window lists playbook "pa" only.
MaintScope   == {"zigbee", "mqtt", "sec", "app"}
AssistListed == {"pa"}

\* Ground truth (used by the properties) vs. what the router believes
\* (the IgnoreMaint mutation blinds only the latter). An assist window lets
\* the playbooks it lists act (13.1).
InMaintRaw(f) == maintOn /\ NodeOf[f] \in MaintScope
               /\ ~(assist /\ PbOf[f] \in AssistListed)
InMaint(f)    == InMaintRaw(f) /\ MUT # "IgnoreMaint"
\* A root cause stays open for correlation while its alert still fires, also
\* after the router handed it off (escalated or paged): shadow testing found
\* that an escalated MQTT outage stopped suppressing Zigbee silence (12.6 e).
Handed      == {"escalated", "paged"}
DepOpenRaw(f) == \E g \in Fps : NodeOf[g] \in Needs[NodeOf[f]] /\ inc[g] \in Active \cup Handed
DepOpen(f)  == \E g \in Fps : NodeOf[g] \in Needs[NodeOf[f]] /\
                  inc[g] \in Active \cup (IF MUT = "CorrelateWhileActiveOnly" THEN {} ELSE Handed)

\* Phase 3: hand an incident to OpenClaw through the router's forward queue
\* (residue, or a page with context). The queue is persisted; DeliverFwd
\* sends when OpenClaw is up, JitPage pages Noah directly while it is down.
\* k = "paged" for page-class events, failed remediations and breakers.
Queue(f, k) == IF (MUT = "DropEscalation" /\ ~ocUp) \/ ~Delivery
               THEN UNCHANGED <<fwdq, pageq>>          \* bug: goes nowhere
               ELSE /\ fwdq' = fwdq \cup {f}
                    /\ pageq' = IF k = "paged" THEN pageq \cup {f} ELSE pageq
Escalate(f, k) ==
  /\ inc' = [inc EXCEPT ![f] = k]
  /\ Queue(f, k)
  /\ UNCHANGED <<agentq, pages, digest>>

Init ==
  /\ inc = [f \in Fps |-> "none"]
  /\ deliv = [f \in Fps |-> 0]
  /\ execN = [f \in Fps |-> 0]
  /\ lock = [t \in Targets |-> Free]
  /\ runs = [p \in Playbooks |-> 0]
  /\ breaker = [p \in Playbooks |-> FALSE]
  /\ viol = {}
  /\ mode = "active"
  /\ ocUp = TRUE
  /\ routerUp = TRUE
  /\ deploying = FALSE
  /\ maintOn = FALSE /\ assist = FALSE
  /\ pages = {} /\ agentq = {} /\ fwdq = {} /\ pageq = {} /\ digest = {}
  /\ email = {} /\ acked = {} /\ unseen = {} /\ gq = {}
  /\ suppOn = FALSE
  /\ budget = [kill |-> 1, oc |-> 1, restart |-> 1, deploy |-> 1,
               maint |-> 1, window |-> 1, resolve |-> 1, supp |-> 1]

-----------------------------------------------------------------------------
\* ---- Grafana: deliver (or re-deliver) an alert --------------------------
\* Grafana's email route takes page classes whatever the router does
\* (decision 4). Its OpenClaw route is the fallback (L4): it queues every
\* delivery the router may have missed; the OpenClaw transform runs the
\* agent unless the router acknowledged *this* delivery (GrafanaOC).
Suppressed(f) == suppOn /\ f \in SuppFps
Deliver(f) ==
  /\ deliv[f] < (IF f \in Redeliver THEN MaxDeliveries ELSE 1)
  /\ deliv' = [deliv EXCEPT ![f] = @ + 1]
  /\ email' = IF f \in PageClass THEN email \cup {f} ELSE email
  /\ IF ~routerUp
     THEN /\ UNCHANGED <<inc, execN, fwdq, pageq>>
          /\ IF f \in FallbackFps
             THEN /\ gq' = gq \cup {f}
                  /\ unseen' = unseen \cup {f}
                  \* The ack is per delivery: a router that saw an earlier
                  \* delivery of the same fingerprint has not seen this one.
                  /\ acked' = IF MUT = "AckByFingerprint" THEN acked ELSE acked \ {f}
             ELSE UNCHANGED <<gq, unseen, acked>>
     ELSE /\ acked' = IF f \in FallbackFps THEN acked \cup {f} ELSE acked
          /\ unseen' = unseen \ {f}
          /\ UNCHANGED gq
          /\ IF inc[f] \in Active                       \* duplicate: same incident
             THEN UNCHANGED <<inc, execN, fwdq, pageq>>
             ELSE /\ execN' = IF inc[f] \in Terminal THEN [execN EXCEPT ![f] = 0]
                                                     ELSE execN
                  /\ IF f \in PageClass /\ ~(MUT = "MaintSilencesPage" /\ InMaint(f))
                        /\ ~(MUT = "SuppressPage" /\ Suppressed(f))
                     THEN /\ inc' = [inc EXCEPT ![f] = "paged"]  \* even in maint
                          /\ Queue(f, "paged")
                     ELSE /\ inc' = [inc EXCEPT ![f] =
                                 IF Suppressed(f) THEN "suppressed"
                                 ELSE IF InMaint(f) THEN "maint"
                                 ELSE IF DepOpen(f) /\ MUT # "NoCorrelation"
                                      THEN "symptom" ELSE "open"]
                          /\ UNCHANGED <<fwdq, pageq>>
  /\ UNCHANGED <<lock, runs, breaker, viol, mode, ocUp, routerUp,
                 deploying, wv, pages, agentq, digest, suppOn, budget>>

\* Grafana's OpenClaw route (retried while OpenClaw is down). The transform
\* asks the router's /ack; if the router is down or did not acknowledge
\* this delivery, it runs the agent.
GrafanaOC(f) ==
  /\ f \in gq /\ ocUp
  /\ gq' = gq \ {f}
  /\ agentq' = IF MUT = "NoFallback" \/ (routerUp /\ f \in acked) \/ ~Delivery
               THEN agentq ELSE agentq \cup {f}
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, pages, qv, email, acked, unseen,
                 suppOn, budget>>

\* ---- Router ---------------------------------------------------------------
\* A symptom becomes an incident of its own once its dependency is handled.
ReEvaluate(f) ==
  /\ routerUp /\ inc[f] = "symptom" /\ ~DepOpen(f)
  /\ inc' = [inc EXCEPT ![f] = "open"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, pages, agentq, qv, gv, suppOn, budget>>

\* A dependency failed after this incident opened: it becomes a symptom.
\* (TLC found that correlating only on arrival lets the symptom's playbook
\* run when the root-cause alert arrives second; see doc 12.6.)
Correlate(f) ==
  /\ routerUp /\ inc[f] = "open" /\ DepOpen(f) /\ MUT # "NoCorrelation"
  /\ MUT # "CorrelateOnArrivalOnly"
  /\ inc' = [inc EXCEPT ![f] = "symptom"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, pages, agentq, qv, gv, suppOn, budget>>

\* No playbook, or the kill switch is on: hand to OpenClaw (residue).
Forward(f) ==
  /\ routerUp /\ inc[f] = "open"
  /\ PbOf[f] = NoPb \/ mode # "active"
  /\ Escalate(f, "escalated")
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, gv, suppOn, budget>>

\* Rate limit exhausted or breaker open: trip (or keep) the breaker and page.
Trip(f) ==
  LET p == PbOf[f] IN
  /\ routerUp /\ inc[f] = "open" /\ p # NoPb /\ mode = "active"
  /\ (breaker[p] /\ MUT # "IgnoreBreaker") \/ (runs[p] >= MaxRuns /\ MUT # "NoRateLimit")
  /\ breaker' = [breaker EXCEPT ![p] = TRUE]
  /\ Escalate(f, "paged")
  /\ UNCHANGED <<deliv, execN, lock, runs, viol, mode, ocUp, routerUp,
                 deploying, wv, gv, suppOn, budget>>

\* Start the matched playbook: take the target lock and count a run.
StartPb(f) ==
  LET p == PbOf[f]  t == TargetOf[p] IN
  /\ routerUp /\ inc[f] = "open" /\ p # NoPb /\ mode = "active"
  /\ ~breaker[p] \/ MUT = "IgnoreBreaker"
  /\ runs[p] < MaxRuns \/ MUT = "NoRateLimit"
  /\ lock[t] = Free \/ MUT = "NoLock"
  /\ ~deploying \/ MUT = "IgnoreDeploy"
  /\ ~InMaint(f)
  /\ ~DepOpen(f) \/ MUT \in {"NoCorrelation", "CorrelateOnArrivalOnly"}
  /\ lock' = [lock EXCEPT ![t] = f]
  /\ runs' = [runs EXCEPT ![p] = @ + 1]
  /\ inc' = [inc EXCEPT ![f] = "acting"]
  /\ UNCHANGED <<deliv, execN, breaker, viol, mode, ocUp, routerUp,
                 deploying, wv, pages, agentq, qv, gv, suppOn, budget>>

\* Deliver the forward queue to OpenClaw (the hook /hooks/event-router).
DeliverFwd(f) ==
  /\ routerUp /\ ocUp /\ f \in fwdq
  /\ fwdq' = fwdq \ {f}
  /\ pageq' = pageq \ {f}
  /\ agentq' = agentq \cup {f}
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, pages, digest, gv, suppOn, budget>>

\* Second, independent paging path (decision 3): a page-kind item waiting
\* for OpenClaw reaches Noah through the JIT bot. It stays queued for
\* OpenClaw's diagnosis. (event-router-control sends it from the host, so
\* this works with the router container down.)
JitPage(f) ==
  /\ f \in pageq /\ ~ocUp /\ f \notin pages
  /\ MUT \notin {"NoJitPage", "DropQueue"}
  /\ pages' = pages \cup {f}
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, agentq, qv, gv, suppOn, budget>>

\* Residue queued longer than its lifetime (6h) while OpenClaw stays down
\* goes to the digest. A page-kind item expires only after it was paged.
Expire(f) ==
  /\ routerUp /\ f \in fwdq /\ ~ocUp
  /\ MUT = "DropQueue" \/ f \notin pageq \/ f \in pages
  /\ fwdq' = fwdq \ {f}
  /\ pageq' = pageq \ {f}
  /\ digest' = IF MUT = "DropQueue" THEN digest ELSE digest \cup {f}
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, wv, pages, agentq, gv, suppOn, budget>>

\* ---- Executor (host helper) ---------------------------------------------
\* Re-checks, at execution time and independently of the router: the kill
\* switch, the host's deploy lock and maintenance windows. (TLC found that
\* checking deploy/maintenance only when the playbook starts lets a deploy
\* or window begin in between; see doc 12.6.) The router passes the
\* playbook id; the executor checks it against its own allowlist and lets
\* an assist window through only for a playbook that window lists.
ExecOk(f)   == (mode = "active" \/ MUT = "KillAtStartOnly")
DeployOk    == ~deploying \/ MUT \in {"IgnoreDeploy", "DeployAtStartOnly"}
MaintOk(f)  == IF MUT = "ExecutorIgnoresPlaybook"
               THEN ~(maintOn /\ NodeOf[f] \in MaintScope) \/ assist
               ELSE ~InMaint(f) \/ MUT = "MaintAtStartOnly"
DepOk(f)    == ~DepOpen(f) \/ MUT \in {"NoCorrelation", "CorrelateOnArrivalOnly"}

Execute(f) ==
  LET p == PbOf[f]  t == TargetOf[p] IN
  /\ inc[f] = "acting"
  /\ DeployOk                                   \* wait while a deploy runs
  /\ IF ExecOk(f) /\ MaintOk(f) /\ DepOk(f)
     THEN \E a \in (IF MUT = "ActionFromEvent" THEN Allowed \cup Denied
                                               ELSE Allowed) :
          /\ viol' = viol \cup
               { v \in {"S1", "S2", "S3", "S5", "S6", "S7", "S10"} :
                   \/ v = "S1"  /\ \E g \in Fps \ {f} :
                                   inc[g] = "acting" /\ PbOf[g] # NoPb
                                   /\ TargetOf[PbOf[g]] = t
                   \/ v = "S2"  /\ a \notin Allowed
                   \/ v = "S3"  /\ mode # "active"
                   \/ v = "S5"  /\ breaker[p]
                   \/ v = "S6"  /\ DepOpenRaw(f)
                   \/ v = "S7"  /\ deploying
                   \/ v = "S10" /\ InMaintRaw(f) }
          /\ execN' = [execN EXCEPT ![f] = @ + 1]
          /\ inc' = [inc EXCEPT ![f] = "verifying"]
          /\ UNCHANGED <<lock, pages, agentq, qv>>
     ELSE /\ lock' = [lock EXCEPT ![t] = IF @ = f THEN Free ELSE @]
          /\ IF ~ExecOk(f)
             THEN Escalate(f, "escalated")          \* kill switch: hand off
             ELSE /\ inc' = [inc EXCEPT ![f] =
                          IF ~MaintOk(f) THEN "maint" ELSE "symptom"]  \* park
                  /\ UNCHANGED <<pages, agentq, qv>>
          /\ UNCHANGED <<viol, execN>>
  /\ UNCHANGED <<deliv, runs, breaker, mode, ocUp, routerUp, deploying,
                 wv, gv, suppOn, budget>>

Release(t, f) == [lock EXCEPT ![t] = IF @ = f /\ MUT # "NoLockRelease"
                                     THEN Free ELSE @]

\* Decision B: a playbook-verified incident reaches OpenClaw only through
\* the batch decision log, never through the forward queue.
VerifyOk(f) ==
  /\ routerUp /\ inc[f] = "verifying"
  /\ inc' = [inc EXCEPT ![f] = "resolved"]
  /\ lock' = Release(TargetOf[PbOf[f]], f)
  /\ IF MUT = "ForwardHandled"
     THEN /\ fwdq' = fwdq \cup {f}
          /\ viol' = viol \cup {"S12"}
     ELSE UNCHANGED <<fwdq, viol>>
  /\ UNCHANGED pageq
  /\ UNCHANGED <<deliv, execN, runs, breaker, mode, ocUp, routerUp,
                 deploying, wv, pages, agentq, digest, gv, suppOn, budget>>

\* Failed remediation is a page class (decision 5).
VerifyFail(f) ==
  /\ routerUp /\ inc[f] = "verifying"
  /\ lock' = Release(TargetOf[PbOf[f]], f)
  /\ Escalate(f, "paged")
  /\ UNCHANGED <<deliv, execN, runs, breaker, viol, mode, ocUp, routerUp,
                 deploying, wv, gv, suppOn, budget>>

\* ---- Environment (budgeted) --------------------------------------------
Spend(k) ==
  /\ budget[k] > 0
  /\ budget' = [budget EXCEPT ![k] = @ - 1]

Others == <<inc, deliv, execN, lock, runs, breaker, viol, pages, agentq, qv, gv>>

FlipMode ==
  /\ Spend("kill")
  /\ mode' = (IF mode = "active" THEN "notify" ELSE "active")
  /\ UNCHANGED <<Others, ocUp, routerUp, deploying, wv, suppOn>>
OpenClawDown ==
  /\ Delivery
  /\ Spend("oc")
  /\ ocUp
  /\ ocUp' = FALSE
  /\ UNCHANGED <<Others, mode, routerUp, deploying, wv, suppOn>>
OpenClawUp ==
  /\ ~ocUp
  /\ ocUp' = TRUE
  /\ UNCHANGED <<Others, mode, routerUp, deploying, wv, suppOn, budget>>
RouterDown ==
  /\ Spend("restart")
  /\ routerUp
  /\ routerUp' = FALSE
  /\ UNCHANGED <<Others, mode, ocUp, deploying, wv, suppOn>>
\* Restart: state is persisted (SQLite). The bug variant loses open incidents.
RouterUp ==
  /\ ~routerUp
  /\ routerUp' = TRUE
  /\ inc' = (IF MUT = "LoseState"
             THEN [f \in Fps |-> IF inc[f] = "verifying" THEN "none" ELSE inc[f]]
             ELSE inc)
  /\ lock' = (IF MUT = "LoseState" THEN [t \in Targets |-> Free] ELSE lock)
  /\ UNCHANGED <<deliv, execN, runs, breaker, viol, mode, ocUp, deploying,
                 wv, pages, agentq, qv, gv, suppOn, budget>>
DeployStart ==
  /\ Spend("deploy")
  /\ ~deploying
  /\ deploying' = TRUE
  /\ UNCHANGED <<Others, mode, ocUp, routerUp, wv, suppOn>>
DeployEnd ==
  /\ deploying
  /\ deploying' = FALSE
  /\ UNCHANGED <<Others, mode, ocUp, routerUp, wv, suppOn, budget>>
MaintOpen ==
  /\ Spend("maint")
  /\ ~maintOn
  /\ maintOn' = TRUE
  /\ assist' \in BOOLEAN                     \* quiet or assist
  /\ UNCHANGED <<Others, mode, ocUp, routerUp, deploying, suppOn>>
\* Window closes at expiry: recovery check puts every in-scope incident back
\* into normal handling. The bug variant just drops them.
MaintClose ==
  /\ maintOn
  /\ maintOn' = FALSE
  /\ assist' = FALSE
  /\ inc' = [f \in Fps |-> IF inc[f] = "maint"
                           THEN (IF MUT = "NoRecovery" THEN "dropped" ELSE "open")
                           ELSE inc[f]]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, pages, agentq, qv, gv, suppOn, budget>>
WindowElapse ==
  /\ Spend("window")
  /\ runs' = [p \in Playbooks |-> 0]
  /\ UNCHANGED <<inc, deliv, execN, lock, breaker, viol, pages, agentq, qv, gv,
                 mode, ocUp, routerUp, deploying, wv, suppOn>>
\* A suppression (docker/event-router/suppressions.yml: reason and expiry
\* are mandatory) starts, and ends at its expiry.
SuppAdd ==
  /\ SuppFps # {}
  /\ Spend("supp")
  /\ ~suppOn
  /\ suppOn' = TRUE
  /\ UNCHANGED <<Others, mode, ocUp, routerUp, deploying, wv>>
SuppExpire ==
  /\ suppOn
  /\ suppOn' = FALSE
  /\ UNCHANGED <<Others, mode, ocUp, routerUp, deploying, wv, budget>>
\* The alert clears by itself (or a human fixed it) before the router acts.
AlertResolves(f) ==
  /\ Spend("resolve")
  /\ inc[f] \in {"open", "symptom", "maint"} \cup Handed
  /\ inc' = [inc EXCEPT ![f] = "resolved"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, pages, agentq, qv, gv,
                 mode, ocUp, routerUp, deploying, wv, suppOn>>

Env == FlipMode \/ OpenClawDown \/ OpenClawUp \/ RouterDown \/ RouterUp
       \/ DeployStart \/ DeployEnd \/ MaintOpen \/ MaintClose \/ WindowElapse
       \/ SuppAdd \/ SuppExpire
       \/ \E f \in Fps : Deliver(f) \/ AlertResolves(f) \/ GrafanaOC(f)

Router == \E f \in Fps : ReEvaluate(f) \/ Correlate(f) \/ Forward(f) \/ Trip(f) \/ StartPb(f)
                         \/ Execute(f) \/ VerifyOk(f) \/ VerifyFail(f)
                         \/ DeliverFwd(f) \/ JitPage(f) \/ Expire(f)

Next == Router \/ Env

\* Fairness: the router, executor, verification, the forward queue, the JIT
\* bot and Grafana's fallback route always make progress; the router comes
\* back after a restart, deploys end, maintenance windows and suppressions
\* expire (except in the bug variants), and, where OcRecovers, OpenClaw
\* comes back.
Fairness ==
  /\ \A f \in Fps : WF_vars(ReEvaluate(f)) /\ WF_vars(Correlate(f))
                    /\ WF_vars(Forward(f))
                    /\ WF_vars(Trip(f)) /\ WF_vars(StartPb(f))
                    /\ WF_vars(Execute(f)) /\ WF_vars(VerifyOk(f) \/ VerifyFail(f))
                    /\ WF_vars(DeliverFwd(f)) /\ WF_vars(JitPage(f))
                    /\ WF_vars(Expire(f)) /\ WF_vars(GrafanaOC(f))
  /\ WF_vars(RouterUp) /\ WF_vars(DeployEnd)
  /\ IF MUT = "NoExpiry" THEN TRUE ELSE WF_vars(MaintClose)
  /\ IF MUT = "NoSuppressionExpiry" THEN TRUE ELSE WF_vars(SuppExpire)
  /\ IF OcRecovers THEN WF_vars(OpenClawUp) ELSE TRUE

Spec == Init /\ [][Next]_vars /\ Fairness

-----------------------------------------------------------------------------
\* ---- Safety (section 12.1, 13.4) ----------------------------------------
S1_OneActionPerTarget == "S1" \notin viol
S2_OnlyAllowedActions == "S2" \notin viol
S3_KillSwitchStops    == "S3" \notin viol
S4_RateLimit          == \A p \in Playbooks : runs[p] <= MaxRuns
S5_BreakerHolds       == "S5" \notin viol
S6_OneRootCause       == "S6" \notin viol
S7_NoActDuringDeploy  == "S7" \notin viol
S8_NoDoubleAction     == \A f \in Fps : execN[f] <= 1
\* Page classes are never suppressed, parked or acted on, and Grafana
\* emails every delivered one, whatever the router's state.
S9_PageClassPaged     == \A f \in PageClass \cap Fps :
                           /\ inc[f] \in {"none", "paged", "resolved"}
                           /\ deliv[f] > 0 => f \in email
S10_NoActInMaint      == "S10" \notin viol
S11_PageInMaint       == \A f \in PageClass \cap Fps : inc[f] # "maint"
\* Decision B: handled events never wake OpenClaw per event.
S12_HandledNotForwarded == "S12" \notin viol

Safety == S1_OneActionPerTarget /\ S2_OnlyAllowedActions /\ S3_KillSwitchStops
          /\ S4_RateLimit /\ S5_BreakerHolds /\ S6_OneRootCause
          /\ S7_NoActDuringDeploy /\ S8_NoDoubleAction /\ S9_PageClassPaged
          /\ S10_NoActInMaint /\ S11_PageInMaint /\ S12_HandledNotForwarded

\* ---- Liveness (section 12.2, 13.4) --------------------------------------
\* L1/L2/L8: every incident ends handled, and "escalated"/"paged" really
\* reached OpenClaw or Noah (never silently lost, never dropped by a window).
\* Residue that expired from the queue while OpenClaw stayed down is in the
\* digest; a page must have reached Noah (JIT bot) or OpenClaw (which pages).
Reached(f) == \/ inc[f] = "escalated" /\ (f \in agentq \/ f \in digest)
              \/ inc[f] = "paged"     /\ (f \in agentq \/ f \in pages)
\* A symptom attached to a root cause that reached OpenClaw or Noah is handled
\* with it (4.7: one root cause, one incident).
Handled(f) == \/ inc[f] \in {"resolved", "suppressed"}
              \/ Reached(f)
              \/ inc[f] = "symptom" /\ \E g \in Fps : NodeOf[g] \in Needs[NodeOf[f]] /\ Reached(g)
L1_EveryIncidentHandled == \A f \in Fps : (inc[f] \in Active) ~> Handled(f)
L3_BreakerPages  == \A p \in Playbooks :
                      breaker[p] ~> (\E f \in Fps : PbOf[f] = p /\ (f \in pages \/ f \in agentq))
\* L4: a delivery the router missed (it was down) reaches OpenClaw through
\* Grafana's fallback route, unless a later delivery reached the router.
L4_FallbackReachesOpenClaw == \A f \in Fps : (f \in unseen) ~> (f \in agentq \/ f \notin unseen)
L5_LocksReleased == \A t \in Targets : (lock[t] # Free) ~> (lock[t] = Free)
L6_SuppressionsExpire == suppOn ~> ~suppOn
L7_WindowsClose  == maintOn ~> ~maintOn
\* L8: an incident parked by a window re-enters normal handling (or is
\* handled) after the window closes; it is never dropped.
Normal(f) == inc[f] \in {"open", "symptom", "acting", "verifying"}
L8_RecoveryAfterWindow == \A f \in Fps : (inc[f] = "maint") ~> (Normal(f) \/ Handled(f))
=============================================================================
