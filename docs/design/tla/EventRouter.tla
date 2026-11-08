---------------------------- MODULE EventRouter ----------------------------
(***************************************************************************)
(* Model of the homelab event router (docs/design/event-routing.md).       *)
(*                                                                         *)
(* Actors: Grafana (delivers alerts, possibly duplicated, possibly while   *)
(* the router is down), the router (dedupe, correlation, matching,         *)
(* maintenance, escalation), the executor (re-checks mode, runs the one    *)
(* allowed action), OpenClaw (may be down), and the environment (kill      *)
(* switch, deploys, maintenance windows, rate-limit windows, restarts).    *)
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
          Fps            \* alerts in this instance, a subset of AllFps

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
Terminal  == {"resolved", "escalated", "paged"}
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
  pages,      \* fps paged to Noah
  agentq,     \* fps escalated to OpenClaw
  budget      \* remaining environment events

vars == <<inc, deliv, execN, lock, runs, breaker, viol, mode, ocUp,
          routerUp, deploying, maintOn, pages, agentq, budget>>

\* The window covers the radio stack and, to exercise S11, the node of a
\* page-class alert (the window does not name the page class explicitly).
MaintScope == {"zigbee", "mqtt", "sec"}

\* Ground truth (used by the properties) vs. what the router believes
\* (the IgnoreMaint mutation blinds only the latter).
InMaintRaw(f) == maintOn /\ NodeOf[f] \in MaintScope
InMaint(f)    == InMaintRaw(f) /\ MUT # "IgnoreMaint"
DepOpen(f)  == \E g \in Fps : NodeOf[g] \in Needs[NodeOf[f]] /\ inc[g] \in Active

\* Hand an incident to OpenClaw, or page Noah when OpenClaw is down.
Escalate(f) ==
  IF MUT = "DropEscalation" /\ ~ocUp
  THEN /\ inc' = [inc EXCEPT ![f] = "escalated"]      \* bug: goes nowhere
       /\ UNCHANGED <<agentq, pages>>
  ELSE IF ocUp
       THEN /\ inc' = [inc EXCEPT ![f] = "escalated"]
            /\ agentq' = agentq \cup {f}
            /\ UNCHANGED pages
       ELSE /\ inc' = [inc EXCEPT ![f] = "paged"]
            /\ pages' = pages \cup {f}
            /\ UNCHANGED agentq

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
  /\ maintOn = FALSE
  /\ pages = {} /\ agentq = {}
  /\ budget = [kill |-> 1, oc |-> 1, restart |-> 1, deploy |-> 1,
               maint |-> 1, window |-> 1, resolve |-> 1]

-----------------------------------------------------------------------------
\* ---- Grafana: deliver (or re-deliver) an alert --------------------------
Deliver(f) ==
  /\ deliv[f] < (IF f \in Redeliver THEN MaxDeliveries ELSE 1)
  /\ deliv' = [deliv EXCEPT ![f] = @ + 1]
  /\ IF ~routerUp
     THEN UNCHANGED <<inc, execN, pages>>   \* Grafana's fallback route has it (L4)
     ELSE IF inc[f] \in Active                       \* duplicate: same incident
          THEN UNCHANGED <<inc, execN, pages>>
          ELSE /\ execN' = IF inc[f] \in Terminal THEN [execN EXCEPT ![f] = 0]
                                                  ELSE execN
               /\ IF f \in PageClass /\ ~(MUT = "MaintSilencesPage" /\ InMaint(f))
                  THEN /\ inc' = [inc EXCEPT ![f] = "paged"]  \* even in maint
                       /\ pages' = pages \cup {f}
                  ELSE /\ inc' = [inc EXCEPT ![f] =
                              IF InMaint(f) THEN "maint"
                              ELSE IF DepOpen(f) /\ MUT # "NoCorrelation"
                                   THEN "symptom" ELSE "open"]
                       /\ UNCHANGED pages
  /\ UNCHANGED <<lock, runs, breaker, viol, mode, ocUp, routerUp,
                 deploying, maintOn, agentq, budget>>

\* ---- Router ---------------------------------------------------------------
\* A symptom becomes an incident of its own once its dependency is handled.
ReEvaluate(f) ==
  /\ routerUp /\ inc[f] = "symptom" /\ ~DepOpen(f)
  /\ inc' = [inc EXCEPT ![f] = "open"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, maintOn, pages, agentq, budget>>

\* A dependency failed after this incident opened: it becomes a symptom.
\* (TLC found that correlating only on arrival lets the symptom's playbook
\* run when the root-cause alert arrives second; see doc 12.6.)
Correlate(f) ==
  /\ routerUp /\ inc[f] = "open" /\ DepOpen(f) /\ MUT # "NoCorrelation"
  /\ MUT # "CorrelateOnArrivalOnly"
  /\ inc' = [inc EXCEPT ![f] = "symptom"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, maintOn, pages, agentq, budget>>

\* No playbook, or the kill switch is on: hand to OpenClaw (or page).
Forward(f) ==
  /\ routerUp /\ inc[f] = "open"
  /\ PbOf[f] = NoPb \/ mode # "active"
  /\ Escalate(f)
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode, ocUp,
                 routerUp, deploying, maintOn, budget>>

\* Rate limit exhausted or breaker open: trip (or keep) the breaker and page.
Trip(f) ==
  LET p == PbOf[f] IN
  /\ routerUp /\ inc[f] = "open" /\ p # NoPb /\ mode = "active"
  /\ (breaker[p] /\ MUT # "IgnoreBreaker") \/ (runs[p] >= MaxRuns /\ MUT # "NoRateLimit")
  /\ breaker' = [breaker EXCEPT ![p] = TRUE]
  /\ inc' = [inc EXCEPT ![f] = "paged"]
  /\ pages' = pages \cup {f}
  /\ UNCHANGED <<deliv, execN, lock, runs, viol, mode, ocUp, routerUp,
                 deploying, maintOn, agentq, budget>>

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
                 deploying, maintOn, pages, agentq, budget>>

\* ---- Executor (host helper) ---------------------------------------------
\* Re-checks, at execution time and independently of the router: the kill
\* switch, the host's deploy lock and maintenance windows. (TLC found that
\* checking deploy/maintenance only when the playbook starts lets a deploy
\* or window begin in between; see doc 12.6.)
ExecOk(f)   == (mode = "active" \/ MUT = "KillAtStartOnly")
DeployOk    == ~deploying \/ MUT \in {"IgnoreDeploy", "DeployAtStartOnly"}
MaintOk(f)  == ~InMaint(f) \/ MUT = "MaintAtStartOnly"
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
                   \/ v = "S6"  /\ DepOpen(f)
                   \/ v = "S7"  /\ deploying
                   \/ v = "S10" /\ InMaintRaw(f) }
          /\ execN' = [execN EXCEPT ![f] = @ + 1]
          /\ inc' = [inc EXCEPT ![f] = "verifying"]
          /\ UNCHANGED <<lock, pages, agentq>>
     ELSE /\ lock' = [lock EXCEPT ![t] = IF @ = f THEN Free ELSE @]
          /\ IF ~ExecOk(f)
             THEN Escalate(f)                       \* kill switch: hand off
             ELSE /\ inc' = [inc EXCEPT ![f] =
                          IF ~MaintOk(f) THEN "maint" ELSE "symptom"]  \* park
                  /\ UNCHANGED <<pages, agentq>>
          /\ UNCHANGED <<viol, execN>>
  /\ UNCHANGED <<deliv, runs, breaker, mode, ocUp, routerUp, deploying,
                 maintOn, budget>>

Release(t, f) == [lock EXCEPT ![t] = IF @ = f /\ MUT # "NoLockRelease"
                                     THEN Free ELSE @]

VerifyOk(f) ==
  /\ routerUp /\ inc[f] = "verifying"
  /\ inc' = [inc EXCEPT ![f] = "resolved"]
  /\ lock' = Release(TargetOf[PbOf[f]], f)
  /\ UNCHANGED <<deliv, execN, runs, breaker, viol, mode, ocUp, routerUp,
                 deploying, maintOn, pages, agentq, budget>>

VerifyFail(f) ==
  /\ routerUp /\ inc[f] = "verifying"
  /\ lock' = Release(TargetOf[PbOf[f]], f)
  /\ Escalate(f)
  /\ UNCHANGED <<deliv, execN, runs, breaker, viol, mode, ocUp, routerUp,
                 deploying, maintOn, budget>>

\* ---- Environment (budgeted) --------------------------------------------
Spend(k) ==
  /\ budget[k] > 0
  /\ budget' = [budget EXCEPT ![k] = @ - 1]

FlipMode ==
  /\ Spend("kill")
  /\ mode' = (IF mode = "active" THEN "notify" ELSE "active")
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 ocUp, routerUp, deploying, maintOn, pages, agentq>>
OpenClawDown ==
  /\ Spend("oc")
  /\ ocUp' = ~ocUp
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 mode, routerUp, deploying, maintOn, pages, agentq>>
RouterDown ==
  /\ Spend("restart")
  /\ routerUp
  /\ routerUp' = FALSE
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 mode, ocUp, deploying, maintOn, pages, agentq>>
\* Restart: state is persisted (SQLite). The bug variant loses open incidents.
RouterUp ==
  /\ ~routerUp
  /\ routerUp' = TRUE
  /\ inc' = (IF MUT = "LoseState"
             THEN [f \in Fps |-> IF inc[f] = "verifying" THEN "none" ELSE inc[f]]
             ELSE inc)
  /\ lock' = (IF MUT = "LoseState" THEN [t \in Targets |-> Free] ELSE lock)
  /\ UNCHANGED <<deliv, execN, runs, breaker, viol, mode,
                 ocUp, deploying, maintOn, pages, agentq, budget>>
DeployStart ==
  /\ Spend("deploy")
  /\ ~deploying
  /\ deploying' = TRUE
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 mode, ocUp, routerUp, maintOn, pages, agentq>>
DeployEnd ==
  /\ deploying
  /\ deploying' = FALSE
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 mode, ocUp, routerUp, maintOn, pages, agentq, budget>>
MaintOpen ==
  /\ Spend("maint")
  /\ ~maintOn
  /\ maintOn' = TRUE
  /\ UNCHANGED <<inc, deliv, execN, lock, runs, breaker, viol,
                 mode, ocUp, routerUp, deploying, pages, agentq>>
\* Window closes at expiry: recovery check puts every in-scope incident back
\* into normal handling. The bug variant just drops them.
MaintClose ==
  /\ maintOn
  /\ maintOn' = FALSE
  /\ inc' = [f \in Fps |-> IF inc[f] = "maint"
                           THEN (IF MUT = "NoRecovery" THEN "dropped" ELSE "open")
                           ELSE inc[f]]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode,
                 ocUp, routerUp, deploying, pages, agentq, budget>>
WindowElapse ==
  /\ Spend("window")
  /\ runs' = [p \in Playbooks |-> 0]
  /\ UNCHANGED <<inc, deliv, execN, lock, breaker, viol, mode,
                 ocUp, routerUp, deploying, maintOn, pages, agentq>>
\* The alert clears by itself (or a human fixed it) before the router acts.
AlertResolves(f) ==
  /\ Spend("resolve")
  /\ inc[f] \in {"open", "symptom", "maint"}
  /\ inc' = [inc EXCEPT ![f] = "resolved"]
  /\ UNCHANGED <<deliv, execN, lock, runs, breaker, viol, mode,
                 ocUp, routerUp, deploying, maintOn, pages, agentq>>

Env == FlipMode \/ OpenClawDown \/ RouterDown \/ RouterUp \/ DeployStart
       \/ DeployEnd \/ MaintOpen \/ MaintClose \/ WindowElapse
       \/ \E f \in Fps : Deliver(f) \/ AlertResolves(f)

Router == \E f \in Fps : ReEvaluate(f) \/ Correlate(f) \/ Forward(f) \/ Trip(f) \/ StartPb(f)
                         \/ Execute(f) \/ VerifyOk(f) \/ VerifyFail(f)

Next == Router \/ Env

\* Fairness: the router, executor and verification always make progress;
\* the router comes back after a restart, deploys end, and maintenance
\* windows expire (except in the NoExpiry bug variant).
Fairness ==
  /\ \A f \in Fps : WF_vars(ReEvaluate(f)) /\ WF_vars(Correlate(f))
                    /\ WF_vars(Forward(f))
                    /\ WF_vars(Trip(f)) /\ WF_vars(StartPb(f))
                    /\ WF_vars(Execute(f)) /\ WF_vars(VerifyOk(f) \/ VerifyFail(f))
  /\ WF_vars(RouterUp) /\ WF_vars(DeployEnd)
  /\ IF MUT = "NoExpiry" THEN TRUE ELSE WF_vars(MaintClose)

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
S9_PageClassPaged     == \A f \in PageClass \cap Fps : inc[f] \in {"none", "paged"}
S10_NoActInMaint      == "S10" \notin viol
S11_PageInMaint       == \A f \in PageClass \cap Fps : inc[f] # "maint"
\* L4 (fallback while the router is down) is Grafana's routing, not the
\* router's: Deliver while ~routerUp leaves the router state untouched.

Safety == S1_OneActionPerTarget /\ S2_OnlyAllowedActions /\ S3_KillSwitchStops
          /\ S4_RateLimit /\ S5_BreakerHolds /\ S6_OneRootCause
          /\ S7_NoActDuringDeploy /\ S8_NoDoubleAction /\ S9_PageClassPaged
          /\ S10_NoActInMaint /\ S11_PageInMaint

\* ---- Liveness (section 12.2, 13.4) --------------------------------------
\* L1/L2/L8: every incident ends handled, and "escalated"/"paged" really
\* reached OpenClaw or Noah (never silently lost, never dropped by a window).
Handled(f) == \/ inc[f] = "resolved"
              \/ inc[f] = "escalated" /\ f \in agentq
              \/ inc[f] = "paged"     /\ f \in pages
L1_EveryIncidentHandled == \A f \in Fps : (inc[f] \in Active) ~> Handled(f)
L3_BreakerPages  == \A p \in Playbooks :
                      breaker[p] ~> (\E f \in Fps : PbOf[f] = p /\ f \in pages)
L5_LocksReleased == \A t \in Targets : (lock[t] # Free) ~> (lock[t] = Free)
L7_WindowsClose  == maintOn ~> ~maintOn
\* L8: an incident parked by a window re-enters normal handling (or is
\* handled) after the window closes; it is never dropped.
Normal(f) == inc[f] \in {"open", "symptom", "acting", "verifying"}
L8_RecoveryAfterWindow == \A f \in Fps : (inc[f] = "maint") ~> (Normal(f) \/ Handled(f))
=============================================================================
