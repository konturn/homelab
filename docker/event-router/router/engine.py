"""The decision engine: dedupe, correlation, matching, limits, execution and
verification (sections 4.4-4.7, 7, 13).

The transitions mirror docs/design/tla/EventRouter.tla, one method per spec
action, so a reviewer can hold the two side by side:

  ingest        Deliver           new episode, duplicate or resolution
  _reevaluate   ReEvaluate        symptom -> open once the root cause is handled
  _correlate    Correlate         open -> symptom when a dependency fails later
  _forward      Forward           no playbook, or kill switch: hand off
  _trip         Trip              rate limit / breaker: open breaker and page
  _start        StartPb           take the target lock and count a run
  _execute      Execute           re-check, then call the executor
  _verify       VerifyOk/Fail     release the lock; resolve or escalate
  _close_windows MaintClose        recovery check after a window

Hand-off (phase 3, `openclaw.forward: true`): "escalated" and "paged"
incidents are queued for OpenClaw's /hooks/event-router with what the router
already did (forward.py semantics below, _handoff and _pump_forwards). Events
a playbook handled reach OpenClaw only through the batch decision log, never
one by one (decision B). While OpenClaw is unreachable, residue stays queued
for up to `openclaw.max_age` (6h) and page items also go to Noah through the
JIT bot (decision 3). Non-urgent pages wait out quiet hours (decision 6).

With `openclaw.forward: false` (phases 1-2) OpenClaw and email receive every
alert straight from Grafana: "escalated" and "paged" are recorded, not sent,
and only facts the router alone knows (a breaker tripped, a remediation
failed) go to Noah through the JIT bot.
"""

import hashlib
import json
import os
import sys
import zoneinfo
from datetime import datetime, timedelta

from . import maint_text, modes, outcomes
from . import suppressions as supp
from .config import CI_ACTIONS, digest_only, match_playbook, page_class
from .events import Event
from .shared import SharedState
from .store import ACTIVE, TERMINAL

DEDUPE_TERMINAL_FOR = 24 * 3600   # a still-firing alert stays one incident this long
OVERRIDE_RENOTIFY = 24 * 3600     # decision 13
DEPLOY_EXEMPT = CI_ACTIONS         # S7: CI actions free the lock or queue behind it
FORWARD_BACKOFF_MAX = 300
PAGE_REFUSALS = ("rate_limit", "dependency")   # the rest of the refusals are residue
DECISIONS_IN_PAYLOAD = 12


def _pb_version(pb):
    return hashlib.sha1(json.dumps(pb.raw, sort_keys=True, default=str).encode()).hexdigest()[:10]


class Engine:
    def __init__(self, cfg, playbooks, graph, store, clock, executor=None,
                 gitlab=None, host_override_path=None, shared=None, out=None,
                 openclaw=None, suppressions=()):
        self.cfg = cfg
        self.playbooks = {p.id: p for p in playbooks}
        self.pb_list = playbooks
        self.graph = graph
        self.store = store
        self.clock = clock
        self.executor = executor
        self.gitlab = gitlab
        self.host_override_path = host_override_path
        self.shared = shared or SharedState(None)
        self.out = out or sys.stdout
        self.openclaw = openclaw
        self.suppressions = list(suppressions)
        self._last_reconcile = 0.0
        self._recover_after_restart()

    # ---- kill switch -----------------------------------------------------
    def host_override(self):
        if not self.host_override_path:
            return None
        return modes.read_host_override(self.host_override_path)

    def runtime_override(self):
        v = self.shared.runtime_mode()
        return v["mode"] if v else None

    def mode(self):
        return modes.effective_mode(self.cfg["mode"], self.host_override(), self.runtime_override())

    @property
    def forwarding(self):
        """Phase 3: the router hands residue to OpenClaw itself."""
        return bool(self.cfg["openclaw"]["forward"] and self.openclaw is not None)

    def set_runtime_mode(self, mode, by):
        """Lower the mode at runtime (automation storm), or clear it. Clearing
        is also done by the Telegram "Resume" button (event-router-control)."""
        now = self.clock.now()
        if mode in (None, "clear"):
            self.shared.set_runtime_mode(None)
        elif mode in modes.RUNTIME_MODES:
            self.shared.set_runtime_mode({"mode": mode, "by": by, "at": now})
        else:
            raise ValueError(f"runtime mode must be one of {modes.RUNTIME_MODES} or clear")
        self.check_mode_change(by=by)
        return self.mode()

    def check_mode_change(self, by="host"):
        """Log mode changes. Telling Noah (on every flip, and again while an
        override is older than 24h) is event-router-control's job on the
        host, so it works even when this container is down."""
        cur = self.mode()
        last = self.store.setting("last_mode")
        if last != cur:
            self.store.set_setting("last_mode", cur)
            self._log({"decision": "mode_changed", "mode": cur, "previous": last, "by": by})

    # ---- logging -----------------------------------------------------------
    def _log(self, record, inc=None):
        now = self.clock.now()
        rec = {"ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")}
        if inc is not None:
            rec.update({"incident": inc["id"], "event": f"{inc['source']}:{inc['rule_uid'] or inc['alertname']}",
                        "node": inc["node"], "fp": inc["fp"]})
            if inc.get("playbook"):
                rec["playbook"] = inc["playbook"]
                pb = self.playbooks.get(inc["playbook"])
                if pb:
                    rec["playbook_version"] = _pb_version(pb)
        rec.update(record)
        rec.setdefault("mode", self.mode())
        self.store.add_decision(now, rec)
        self.out.write(json.dumps(rec, sort_keys=True) + "\n")
        self.out.flush()

    def _notify(self, text, urgent):
        """Queue a message for Noah; event-router-control on the host sends it
        through the JIT bot. Non-urgent messages wait out quiet hours
        (decision 6)."""
        now = self.clock.now()
        due = now if urgent else self.quiet_until(now)
        self.shared.queue_notification(now, text, urgent, due)
        self._log({"decision": "notify", "urgent": urgent, "due_in_s": int(due - now), "text": text})

    # ---- helpers -----------------------------------------------------------
    def _set(self, inc, **fields):
        self.store.update(inc["id"], self.clock.now(), **fields)
        inc.update(fields)

    def _close(self, inc, status, **fields):
        self._set(inc, status=status, closed_at=self.clock.now(), waiting=None, **fields)

    def dep_open(self, inc):
        """DepOpen: an incident on an ancestor node is open: active, or handed
        off (escalated, paged, shadowed) while its alert still fires (doc
        12.6 e). Otherwise an MQTT outage escalated to OpenClaw within one
        step would stop suppressing Zigbee silence."""
        if not inc["node"]:
            return None
        ancestors = self.graph.ancestors(inc["node"])
        since = self.clock.now() - DEDUPE_TERMINAL_FOR
        for other in self.store.active() + self.store.handed_off_firing(since):
            # Only alerts describe a node's health; a failed pipeline or job
            # mapped to `gitlab` must not suppress what depends on GitLab.
            if other["source"] != "grafana":
                continue
            if other["id"] != inc["id"] and other["node"] in ancestors:
                return other
        return None

    def in_maint(self, inc, pb=None, page_cls=None):
        """InMaint: inside an open window's scope (or a dependent of it). An
        `assist` window still lets its listed playbooks run; a page class is
        silenced only by a window that names it (13.2)."""
        now = self.clock.now()
        for w in self.store.windows():
            if w["expires_at"] <= now:
                continue
            if page_cls is not None:
                if page_cls in w["page_classes"]:
                    return w
                continue
            if not inc["node"] or not self.graph.in_scope(inc["node"], w["scope"]):
                continue
            if w["mode"] == "assist" and pb is not None and pb.id in w["playbooks"]:
                continue
            return w
        return None

    def _node_from_labels(self, ev):
        for key in ("container_name", "container"):
            if ev.labels.get(key):
                return self.graph.node_for_container(ev.labels[key])
        if ev.labels.get("node") in self.graph.nodes:
            return ev.labels["node"]
        return ""

    # ---- Deliver -------------------------------------------------------------
    def ingest(self, ev):
        now = self.clock.now()
        inc = self.store.latest_for_fp(ev.fp)
        # GitLab events: the pipeline or job id of this delivery, in every
        # decision, so the safety-net crons can check that the router saw
        # each failed pipeline (8.4, missed incidents).
        ids = {k: ev.labels[k] for k in ("pipeline_id", "job_id") if ev.source == "gitlab" and ev.labels.get(k)}
        if ev.status == "resolved":
            if inc is None:
                return
            self._set(inc, alert_state="resolved")
            if inc["status"] in ("open", "symptom", "maint"):           # AlertResolves
                self._close(inc, "resolved", note="alert resolved before any action")
                self._log({"decision": "resolved", "reason": "alert resolved", **ids}, inc)
            elif inc["status"] in TERMINAL and inc["closed_at"] is None:
                self._set(inc, closed_at=now)
            return

        if inc is not None and (inc["status"] in ACTIVE or (
                inc["alert_state"] == "firing" and now - inc["opened_at"] < DEDUPE_TERMINAL_FOR)):
            self._set(inc, deliveries=inc["deliveries"] + 1)            # same incident
            self._log({"decision": "deduped", "deliveries": inc["deliveries"], **ids}, inc)
            return

        pb = match_playbook(self.pb_list, ev) if ev.kind != "datasource_error" else None
        node = pb.node_for(ev, self.graph) if pb else self._node_from_labels(ev)
        target = pb.target_for(ev) if pb else None
        pcls, urgent = page_class(self.cfg, ev) if ev.kind == "alert" else (None, False)
        fields = dict(fp=ev.fp, source=ev.source, kind=ev.kind, rule_uid=ev.rule_uid,
                      alertname=ev.alertname, node=node, labels=ev.labels,
                      playbook=pb.id if pb else None, target=target,
                      alert_state="firing", page_class=pcls, status="open", summary=ev.summary or None)
        inc_id = self.store.new_incident(now, **fields)
        inc = self.store.get(inc_id)
        extra = dict({"summary": ev.summary} if ev.summary else {}, **ids)

        if pcls and not self.in_maint(inc, pb, page_cls=pcls):           # S9, S11
            self._close(inc, "paged")
            self._log({"decision": "paged", "page_class": pcls, "urgent": urgent,
                       "delivery": self._delivery(inc), **extra}, inc)
            self._handoff(inc, "page", f"page class {pcls}", urgent=urgent)
            return
        sel = digest_only(self.cfg, ev)
        if sel:
            self._close(inc, "digest")
            self._log({"decision": "digest", "reason": sel["reason"], **extra}, inc)
            return
        s = supp.match(self.suppressions, ev, self._today()) if not pcls else None
        if s:                                                            # 8.3, L6
            self._close(inc, "suppressed", note=f"suppression {s['id']}")
            self._log({"decision": "suppressed", "reason": "suppression", "suppression": s["id"],
                       "until": s["until"].isoformat(), **extra}, inc)
            return
        w = self.in_maint(inc, pb)
        if pcls and not w:
            w = self.in_maint(inc, pb, page_cls=pcls)
        if w:
            self._set(inc, status="maint", root=w["id"])
            self._log({"decision": "suppressed", "reason": "maintenance", "window": w["id"], **extra}, inc)
            return
        root = self.dep_open(inc)
        if root:
            self._set(inc, status="symptom", root=root["id"])
            self._log({"decision": "suppressed", "reason": "ancestor incident",
                       "root": root["id"], "root_node": root["node"],
                       "root_status": root["status"], **extra}, inc)
            return
        if self._burst(now, inc):
            self._forward(inc, "escalated", reason="burst: many failures without a common ancestor")
            return
        self._log({"decision": "opened", **extra}, inc)

    def _burst(self, now, inc):
        recent = [i for i in self.store.recent_incidents(now - self.cfg["burst_within"])
                  if i["id"] != inc["id"] and i["source"] == "grafana" and not i["page_class"]
                  and i["status"] != "symptom"]
        return inc["source"] == "grafana" and len(recent) >= self.cfg["burst_events"]

    # ---- router and executor steps -----------------------------------------------
    def step(self):
        """One pass over the active incidents (oldest first). Each incident
        takes at most one transition per pass."""
        self._close_windows()
        for inc in self.store.active():
            inc = self.store.get(inc["id"])
            if inc["status"] not in ACTIVE:
                continue
            pb = self.playbooks.get(inc["playbook"]) if inc["playbook"] else None
            st = inc["status"]
            if st == "symptom":
                self._reevaluate(inc)
            elif st == "open":
                self._open(inc, pb)
            elif st == "acting":
                self._execute(inc, pb)
            elif st == "verifying":
                self._verify(inc, pb)
            # "maint" leaves only through _close_windows (recovery check).

    def _reevaluate(self, inc):
        if inc["alert_state"] == "resolved":
            self._close(inc, "resolved")
            self._log({"decision": "resolved", "reason": "symptom cleared"}, inc)
        elif not self.dep_open(inc):
            self._set(inc, status="open", root=None)
            self._log({"decision": "reevaluated", "reason": "root cause handled"}, inc)

    def _correlate(self, inc):
        root = self.dep_open(inc)
        if root:
            self._set(inc, status="symptom", root=root["id"], waiting=None)
            self._log({"decision": "suppressed", "reason": "ancestor incident (arrived later)",
                       "root": root["id"], "root_node": root["node"]}, inc)
            return True
        return False

    def _open(self, inc, pb):
        if inc["alert_state"] == "resolved":
            self._close(inc, "resolved")
            self._log({"decision": "resolved", "reason": "alert resolved"}, inc)
            return
        if self._correlate(inc):
            return
        w = self.in_maint(inc, pb)
        if w:
            self._set(inc, status="maint", root=w["id"], waiting=None)
            self._log({"decision": "suppressed", "reason": "maintenance", "window": w["id"]}, inc)
            return
        if pb is None:
            return self._forward(inc, "no_match")
        pmode = modes.playbook_mode(self.mode(), pb.mode)
        if pmode == "notify-only":
            return self._forward(inc, "escalated", reason="kill switch: notify-only")
        self._start(inc, pb, pmode)

    def _forward(self, inc, decision, reason=None, **extra):
        """Hand the incident to OpenClaw as residue (no playbook, kill switch,
        a refused or impossible action)."""
        self._close(inc, "escalated")
        rec = {"decision": decision, "delivery": self._delivery(inc), **extra}
        if reason:
            rec["reason"] = reason
        self._log(rec, inc)
        self._handoff(inc, "residue", reason or decision)

    def _page(self, inc, decision, reason, notify_text, **extra):
        """A failed remediation (breaker, verification, action error): Noah is
        paged through OpenClaw, which also gets the incident to diagnose.
        Without forwarding, the router tells Noah itself (JIT bot)."""
        self._close(inc, "paged")
        self._log({"decision": decision, "reason": reason, "page_class": "failed_remediation",
                   "delivery": self._delivery(inc), **extra}, inc)
        if self.forwarding:
            self._handoff(inc, "page", reason, urgent=False, page_cls="failed_remediation")
        else:
            self._notify(notify_text, urgent=False)

    def _delivery(self, inc):
        if self.forwarding:
            return "openclaw"
        return "grafana-direct" if inc["source"] == "grafana" else "log"

    def _trip(self, inc, pb, pmode, decision, reason):
        now = self.clock.now()
        self.store.open_breaker(pb.id, pmode, now)
        if pmode != "active":
            # A shadow playbook's breaker: nothing failed for real. The event
            # is unhandled, so it is residue (L3 is about active playbooks).
            return self._forward(inc, decision, reason=f"shadow playbook {pb.id}: {reason}",
                                 breaker="open", mode=pmode)
        self._page(inc, decision, reason,
                   f"Event router: playbook {pb.id} stopped ({reason}) for {inc['target']}; "
                   f"incident {inc['id']} goes to OpenClaw.", breaker="open", mode=pmode)

    def _wait(self, inc, why):
        if inc.get("waiting") != why:
            self._set(inc, waiting=why)
            self._log({"decision": "waiting", "reason": why}, inc)

    def _start(self, inc, pb, pmode):
        now = self.clock.now()
        target = inc["target"]
        if not target:
            return self._forward(inc, "escalated", reason="no valid target in the event")
        if target in self.cfg["restart_denylist"] and pb.action == "restart_container":
            return self._forward(inc, "denied", reason=f"{target} is on the restart denylist")
        lim = pb.limits
        if self.store.breaker(pb.id, pmode):
            return self._trip(inc, pb, pmode, "breaker_open", "breaker open")
        runs = self.store.runs_since(pb.id, target, pmode, now - lim["per"])
        if len(runs) >= lim["max_runs"]:
            return self._trip(inc, pb, pmode, "rate_limited",
                              f"{len(runs)} run(s) in the last {lim['per'] // 60}m")
        if runs and now - runs[-1] < lim["cooldown"]:
            return self._trip(inc, pb, pmode, "rate_limited", "cooldown")
        if pmode == "active" and self.store.active_runs_since(now - 3600) >= self.cfg["global_actions_per_hour"]:
            self.set_runtime_mode("notify-only", "automation-storm")
            self._notify("Event router: automation storm (global action limit reached); "
                         "switched to notify-only. Resume with the Telegram button.", urgent=True)
            return self._forward(inc, "rate_limited", reason="global action limit: automation storm")
        if pb.action not in DEPLOY_EXEMPT and self._deploy_running():
            return self._wait(inc, "deploy in progress")
        # Preconditions (no_deploy_touching) are checked by the executor, on
        # the host, where the container's creation time is (doc 14).
        holder = self.store.lock_holder(target)
        if holder and holder != inc["id"]:
            return self._wait(inc, f"lock on {target} held by {holder}")
        action = f"{pb.action}:{target}"
        if pmode == "shadow":
            with self.store.tx():
                self.store.add_run(pb.id, target, "shadow", now, inc["id"])
            self._close(inc, "shadowed")
            self._log({"decision": "shadow", "action": action, "mode": "shadow",
                       "would": "run", "delivery": self._delivery(inc)}, inc)
            # Nothing acted: the event is still unhandled, so OpenClaw gets it.
            self._handoff(inc, "residue", f"shadow: playbook {pb.id} would have run {action}")
            return
        with self.store.tx():
            if not self.store.take_lock(target, inc["id"], now):
                return self._wait(inc, f"lock on {target}")
            self.store.add_run(pb.id, target, "active", now, inc["id"])
            self.store.update(inc["id"], now, status="acting", waiting=None)
        inc.update(status="acting", waiting=None)
        self._log({"decision": "run", "action": action, "mode": "active"}, inc)

    def _deploy_running(self):
        if not self.gitlab:
            return False
        return bool(self.gitlab.deploy_running(self.cfg["deploy_jobs"]))

    def _release(self, inc):
        if inc["target"]:
            self.store.release_lock(inc["target"], inc["id"])

    def _execute(self, inc, pb):
        now = self.clock.now()
        if inc["exec_started_at"]:
            # The router stopped while the executor ran: never call it twice
            # for one incident (S8); verification decides.
            self._set(inc, status="verifying", verify_deadline=now + pb.verify_within,
                      exec_count=inc["exec_count"] + 1, note="router restarted during execution")
            self._log({"decision": "verifying", "reason": "restart during execution"}, inc)
            return
        if inc["alert_state"] == "resolved":
            self._release(inc)
            self._close(inc, "resolved", note="alert resolved before the action")
            return self._log({"decision": "resolved", "reason": "alert resolved before the action"}, inc)
        if modes.playbook_mode(self.mode(), pb.mode) != "active":     # kill switch
            self._release(inc)
            return self._forward(inc, "escalated", reason=f"kill switch: {self.mode()}")
        w = self.in_maint(inc, pb)
        if w:                                                           # 12.6 (a)
            self._release(inc)
            self._set(inc, status="maint", root=w["id"], waiting=None)
            return self._log({"decision": "suppressed", "reason": "maintenance opened before the action",
                              "window": w["id"]}, inc)
        if self.dep_open(inc):                                          # 12.6 (b)
            self._release(inc)
            self._correlate(inc)
            return
        if pb.action not in DEPLOY_EXEMPT and self._deploy_running():
            return self._wait(inc, "deploy in progress")
        if self.executor is None:
            self._release(inc)
            return self._forward(inc, "escalated", reason="no executor installed")
        self._set(inc, exec_started_at=now)
        res = self.executor.execute(pb.action, inc["target"], inc["id"], pb.id)
        now = self.clock.now()
        action = f"{pb.action}:{inc['target']}"
        if res.get("ok"):
            self.store.add_action(inc["id"], pb.id, pb.action, inc["target"], now, "ok")
            new_job = str(res.get("new_job_id") or "")
            self._set(inc, status="verifying", exec_count=inc["exec_count"] + 1,
                      verify_deadline=now + pb.verify_within, waiting=None,
                      verify_ref=new_job if new_job.isdigit() else None)
            return self._log({"decision": "executed", "action": action, "result": "ok",
                              "detail": res.get("detail"), **({"new_job_id": new_job} if new_job else {})}, inc)
        if res.get("unknown"):
            # Timeout or lost reply: the action may have run. Count it, keep
            # the lock and let verification decide; never retry it.
            self.store.add_action(inc["id"], pb.id, pb.action, inc["target"], now, "unknown")
            self._set(inc, status="verifying", exec_count=inc["exec_count"] + 1,
                      verify_deadline=now + pb.verify_within)
            return self._log({"decision": "executed", "action": action, "result": "unknown",
                              "detail": res.get("detail")}, inc)
        refused = res.get("refused")
        self._set(inc, exec_started_at=None)
        if refused == "deploy":
            return self._wait(inc, "deploy in progress (executor)")
        self._release(inc)
        if refused == "kill_switch":
            return self._forward(inc, "escalated", reason="kill switch (executor)")
        if refused == "maintenance":
            self._set(inc, status="maint", waiting=None)
            return self._log({"decision": "suppressed", "reason": "maintenance (executor)"}, inc)
        if refused == "dependency" and self._correlate(inc):
            return
        reason = f"executor {('refused: ' + refused) if refused else 'error'}: {res.get('detail') or res.get('error', '')}".strip()
        text = (f"Event router: {action} for {inc['alertname'] or inc['fp']} was not done ({reason}). "
                f"OpenClaw has the alert.")
        if refused and refused not in PAGE_REFUSALS:
            # A precondition that did not hold (a deploy touched the
            # container, the job is not orphaned or was superseded): nothing
            # was attempted and nothing is known to be broken beyond the
            # alert, so it is residue for OpenClaw (I2: a revert, not a
            # restart). Without forwarding the router tells Noah, as in phase 2.
            self._forward(inc, "denied", reason=reason, action=action, refused=refused)
            if not self.forwarding:
                self._notify(text, urgent=False)
            return
        # The action failed, the executor's own limit stopped it, or a
        # dependency the router had no incident for is down (12.6, routing
        # table row 1): a failed remediation, which pages.
        self._page(inc, "denied" if refused else "failed", reason, text, action=action, refused=refused)

    def _verify(self, inc, pb):
        now = self.clock.now()
        ok, failed_now = False, False
        if pb.verify_kind == "alert_resolved":
            ok = inc["alert_state"] == "resolved"
        elif pb.verify_kind == "ci_job_finished":
            status = self.gitlab.job_status(inc["target"]) if self.gitlab else None
            ok = status is not None and status not in ("running", "pending", "created")
        elif pb.verify_kind == "ci_job_succeeded":
            # The retried job (the executor's reply named it).
            ref = inc.get("verify_ref")
            status = self.gitlab.job_status(ref) if (self.gitlab and ref) else None
            ok = status == "success"
            failed_now = status in ("failed", "canceled", "skipped") or (not ref and inc["exec_count"])
        if ok:
            self._release(inc)
            self._close(inc, "resolved")
            self._log({"decision": "verified", "verify": "ok", "notify": pb.on_success,
                       "duration_s": int(now - inc["opened_at"])}, inc)
            if pb.on_success == "page":
                self._notify(f"Event router: {pb.id} fixed {inc['target']}.", urgent=False)
            if pb.action == "cancel_ci_job":
                self._after_orphan_cancel(inc)
        elif failed_now or now >= (inc["verify_deadline"] or now):
            self._release(inc)
            text = (f"Event router: {pb.id} acted on {inc['target']} but verification failed "
                    f"({inc['alertname'] or inc['fp']}). OpenClaw has the alert.")
            rec = {"verify": "failed", "notify": pb.on_failure}
            if pb.on_failure == "page":
                self._page(inc, "verify_failed", f"{pb.id} acted, verification failed", text, **rec)
            elif pb.on_failure == "agent":
                self._forward(inc, "verify_failed", reason=f"{pb.id} acted, verification failed", **rec)
            else:
                self._close(inc, "digest")
                self._log({"decision": "verify_failed", **rec}, inc)

    def _after_orphan_cancel(self, inc):
        """Routing table row 11: after cancelling an orphaned deploy job, the
        newest deploy may never have run (it was the orphan). Raise the
        follow-up event; the ci-orphan-retry playbook retries the job, and
        the executor retries only a job it cancelled itself that nothing
        newer superseded."""
        job = inc["labels"].get("job_id") or inc["target"]
        self.ingest(Event(source="reconciler", kind="ci_orphan_cancelled",
                          fp=f"reconciler:ci-orphan-cancelled:{job}", status="firing",
                          alertname="Orphaned deploy job cancelled: retry the newest deploy",
                          labels={"job_id": str(job), "job_name": inc["labels"].get("job_name", ""),
                                  "cancelled_by": inc["id"]}))

    # ---- maintenance windows ---------------------------------------------------
    def open_window(self, scope, duration, reason, opened_by, mode="quiet", playbooks=(), page_classes=()):
        now = self.clock.now()
        if not reason:
            raise ValueError("a window needs a reason")
        if duration <= 0 or duration > self.cfg["max_window"]:
            raise ValueError(f"a window lasts at most {self.cfg['max_window'] // 3600}h")
        if mode not in ("quiet", "assist"):
            raise ValueError("mode must be quiet or assist")
        unknown = [n for n in scope if n != "site" and n not in self.graph.nodes]
        if not scope or unknown:
            raise ValueError(f"unknown scope nodes: {unknown}" if unknown else "scope is empty")
        bad = [p for p in playbooks if p not in self.playbooks]
        if bad:
            raise ValueError(f"unknown playbooks: {bad}")
        wid = self.store.open_window(now, scope, mode, reason, now + duration, opened_by,
                                     playbooks, page_classes)
        self.sync_shared()
        self._log({"decision": "window_opened", "window": wid, "scope": sorted(scope),
                   "window_mode": mode, "expires_in_s": duration, "by": opened_by, "reason": reason})
        return wid

    def close_window(self, wid, by):
        self.store.close_window(wid, self.clock.now())
        self.sync_shared()
        self._log({"decision": "window_closed", "window": wid, "by": by})
        self._close_windows()

    def sync_shared(self):
        """Publish the open windows for the executor's execution-time re-check."""
        self.shared.write_windows(self.store.windows())

    def _close_windows(self):
        now = self.clock.now()
        for w in self.store.windows():
            if w["expires_at"] <= now:
                self.store.close_window(w["id"], now)
                self._log({"decision": "window_closed", "window": w["id"], "by": "expiry"})
        self.sync_shared()
        for inc in self.store.active():
            if inc["status"] == "maint":
                pb = self.playbooks.get(inc["playbook"]) if inc["playbook"] else None
                if not self.in_maint(inc, pb) and not (
                        inc["page_class"] and self.in_maint(inc, pb, page_cls=inc["page_class"])):
                    self._recover(inc)

    def _recover(self, inc):
        """Recovery check (13.3): anything still failing re-enters normal
        handling as if the alert had just fired; the rest closes."""
        if inc["alert_state"] == "resolved":
            self._close(inc, "resolved", note="healthy after maintenance")
            self._log({"decision": "recovered", "result": "healthy"}, inc)
        elif inc["page_class"]:
            self._close(inc, "paged")
            self._log({"decision": "paged", "page_class": inc["page_class"],
                       "reason": "still failing after maintenance", "delivery": self._delivery(inc)}, inc)
            self._handoff(inc, "page", "page class still failing after maintenance",
                          urgent=self._urgent(inc["page_class"]))
        else:
            self._set(inc, status="open", root=None)
            self._log({"decision": "recovered", "result": "still failing: normal handling"}, inc)

    # ---- restart recovery, reconciler, notifications -------------------------------
    def _recover_after_restart(self):
        # Locks held by incidents that are no longer acting/verifying are stale.
        live = {i["id"] for i in self.store.active() if i["status"] in ("acting", "verifying")}
        for target, holder in self.store.locks().items():
            if holder not in live:
                self.store.release_lock(target, holder)

    def tick(self, drain=True):
        if drain:
            for row_id, ev in self.store.inbox_take():
                try:
                    self.ingest(Event.from_dict(ev))
                finally:
                    self.store.inbox_done(row_id)
        self.check_mode_change()
        self.step()
        self._pump_forwards()
        now = self.clock.now()
        if now - self._last_reconcile >= self.cfg["reconcile_interval"]:
            self._last_reconcile = now
            self.reconcile()

    def reconcile(self):
        now = self.clock.now()
        self.store.prune_seen(now - 2 * self.cfg["openclaw"]["ack_window"])
        for b in self.store.breakers():
            if now - b["last_trigger"] >= self.cfg["breaker_reset_after"]:
                self.store.reset_breaker(b["playbook"], b["mode"])
                self._log({"decision": "breaker_reset", "playbook": b["playbook"], "mode": b["mode"]})
        if self.gitlab:
            self._reconcile_ci(now)

    def _reconcile_ci(self, now):
        """Level-triggered check with no natural edge (I3): a job holding a
        deploy resource group past its timeout, or started before GitLab
        last came back."""
        co = self.cfg["ci_orphan"]
        jobs = self.gitlab.running_jobs()
        if jobs is None:
            self.store.set_setting("gitlab_down_since", self.store.setting("gitlab_down_since") or now)
            return
        if self.store.setting("gitlab_down_since"):
            self.store.set_setting("gitlab_down_since", None)
            self.store.set_setting("gitlab_up_since", now)
        up_since = self.store.setting("gitlab_up_since", 0)
        running = set()
        for j in jobs:
            timeout = co["jobs"].get(j["name"])
            if timeout is None or j.get("started_at") is None:
                continue
            running.add(str(j["id"]))
            age = now - j["started_at"]
            why = None
            if age > timeout + co["grace"]:
                why = "past its timeout"
            elif j["started_at"] < up_since:
                why = "started before GitLab came back"
            if why:
                self.ingest(Event(source="reconciler", kind="ci_orphan",
                                  fp=f"reconciler:ci-orphan:{j['id']}", status="firing",
                                  alertname="CI job holding a resource group",
                                  labels={"job_id": str(j["id"]), "job_name": j["name"], "why": why}))
        for inc in self.store.active():
            if inc["kind"] == "ci_orphan" and inc["labels"].get("job_id") not in running:
                self.ingest(Event(source="reconciler", kind="ci_orphan", fp=inc["fp"], status="resolved"))

    def quiet_until(self, now):
        """End of the current quiet-hours span, or now outside it."""
        q = self.cfg["quiet_hours"]
        if not q:
            return now
        tz = zoneinfo.ZoneInfo(q.get("tz", "UTC"))
        local = datetime.fromtimestamp(now, tz)
        hm = local.strftime("%H:%M")
        start, end = q["start"], q["end"]
        quiet = (start <= hm or hm < end) if start > end else (start <= hm < end)
        if not quiet:
            return now
        eh, em = (int(x) for x in end.split(":"))
        until = local.replace(hour=eh, minute=em, second=0, microsecond=0)
        if until <= local:
            until = until + timedelta(days=1)
        return until.timestamp()

    # ---- hand-off to OpenClaw (phase 3) -------------------------------------------
    def _today(self):
        tz = zoneinfo.ZoneInfo((self.cfg["quiet_hours"] or {}).get("tz", "UTC"))
        return datetime.fromtimestamp(self.clock.now(), tz).date()

    def _urgent(self, pcls):
        return bool((self.cfg["page_classes"].get(pcls) or {}).get("urgent"))

    def _handoff(self, inc, kind, reason, urgent=False, page_cls=None):
        """Queue the incident for OpenClaw (residue or page). A no-op without
        forwarding (phases 1-2: Grafana delivers to OpenClaw itself)."""
        if not self.forwarding:
            return
        now = self.clock.now()
        due = now
        held = False
        if kind == "page" and not urgent:
            due = self.quiet_until(now)                  # decision 6
            held = due > now
        payload = self._payload(inc, kind, reason, urgent, page_cls or inc.get("page_class"), held)
        fid = self.store.add_forward(inc["id"], kind, urgent, now, due, payload)
        self._log({"decision": "queued", "forward": fid, "kind": kind, "urgent": urgent,
                   "due_in_s": int(due - now)}, inc)

    def _payload(self, inc, kind, reason, urgent, pcls, held):
        pb = self.playbooks.get(inc["playbook"]) if inc.get("playbook") else None
        decisions = [{k: d[k] for k in ("ts", "decision", "reason", "action", "result", "detail",
                                        "root", "root_node", "mode", "refused") if k in d}
                     for d in self.store.decisions_for(inc["id"])][-DECISIONS_IN_PAYLOAD:]
        quiet = self.quiet_until(self.clock.now()) > self.clock.now()
        return {
            "version": 1, "kind": kind, "reason": reason,
            "incident": {
                "id": inc["id"], "source": inc["source"], "kind": inc["kind"],
                "rule_uid": inc["rule_uid"], "alertname": inc["alertname"], "node": inc["node"],
                "labels": inc["labels"], "summary": inc.get("summary") or "",
                "opened_at": datetime.fromtimestamp(inc["opened_at"]).astimezone().isoformat(timespec="seconds"),
                "deliveries": inc["deliveries"], "fp": inc["fp"],
            },
            "playbook": ({"id": pb.id, "mode": pb.mode, "action": pb.action, "target": inc["target"]}
                         if pb else None),
            "decisions": decisions,
            "dependencies": sorted(self.graph.ancestors(inc["node"]))[:20] if inc["node"] else [],
            "page": ({"class": pcls, "urgent": urgent, "held_for_quiet_hours": held,
                      "email": inc["source"] == "grafana" and pcls not in (None, "failed_remediation")}
                     if kind == "page" else None),
            "quiet_hours": quiet,
            "outcome": {"url": "http://event-router:8080/outcome", "incident": inc["id"]},
        }

    def _pump_forwards(self):
        """Deliver queued items to OpenClaw, oldest first. While OpenClaw is
        unreachable, try only the oldest item each pass; page items also go
        to Noah through the JIT bot once they have waited `jit_after`; items
        older than `max_age` expire into the digest (7.4)."""
        if not self.forwarding:
            return
        oc = self.cfg["openclaw"]
        tried_and_failed = False
        for f in self.store.pending_forwards():
            now = self.clock.now()
            inc = self.store.get(f["incident"])
            expired = now - f["created"] > oc["max_age"]
            if f["due_at"] > now and not expired:
                continue
            if (f["kind"] == "page" and f["due_at"] > f["created"] and inc["alert_state"] == "resolved"
                    and (f["payload"].get("page") or {}).get("class") != "failed_remediation"):
                # A page-class alert held for quiet hours that cleared before
                # morning: digest only. Never a failed remediation (a breaker,
                # a failed verification): that is a fact about the router, not
                # the alert, and still pages (L3; doc 12.6 g).
                self.store.update_forward(f["id"], state="skipped")
                self._log({"decision": "forward_skipped", "forward": f["id"],
                           "reason": "resolved during quiet hours"}, inc)
                continue
            # A page OpenClaw has not taken goes to Noah through the JIT bot:
            # after a failed try and `jit_after`, and always before it would
            # expire (the router may have been stopped the whole time).
            if (f["kind"] == "page" and f["jit_at"] is None
                    and (expired or (f["attempts"] > 0
                                     and now - max(f["created"], f["due_at"]) >= oc["jit_after"]))):
                p = f["payload"]
                cls = (p.get("page") or {}).get("class")
                self.shared.queue_notification(
                    now, f"Page ({cls}), OpenClaw unreachable: {inc['alertname'] or inc['fp']}"
                         f"{(' on ' + inc['node']) if inc['node'] else ''}"
                         f"{(': ' + inc['summary']) if inc.get('summary') else ''}. {p.get('reason', '')}. "
                         f"Incident {inc['id']}; queued for OpenClaw.", f["urgent"], now)
                self.store.update_forward(f["id"], jit_at=now)
                f["jit_at"] = now
                self._log({"decision": "jit_paged", "forward": f["id"], "page_class": cls,
                           "urgent": f["urgent"], "last_error": f["last_error"]}, inc)
            if expired:
                self.store.update_forward(f["id"], state="expired")
                self._log({"decision": "forward_expired", "forward": f["id"], "kind": f["kind"],
                           "attempts": f["attempts"], "last_error": f["last_error"]}, inc)
                continue
            if f["next_try"] > now or tried_and_failed:
                continue
            payload = dict(f["payload"], queued_s=int(now - f["created"]), attempt=f["attempts"] + 1,
                           jit_paged=bool(f["jit_at"]))
            result, detail = self.openclaw.deliver(payload)
            now = self.clock.now()
            if result == "ok":
                self.store.update_forward(f["id"], state="delivered", delivered_at=now,
                                          attempts=f["attempts"] + 1, last_error=None)
                self.store.set_setting("forward_rejected_since", None)
                self._log({"decision": "forwarded", "forward": f["id"], "kind": f["kind"],
                           "delivery": "openclaw", "attempts": f["attempts"] + 1,
                           "queued_s": int(now - f["created"])}, inc)
                continue
            tried_and_failed = True
            n = f["attempts"] + 1
            self.store.update_forward(f["id"], attempts=n, last_error=f"{result}: {detail}",
                                      next_try=now + min(FORWARD_BACKOFF_MAX, 15 * 2 ** min(n, 5)))
            if result == "rejected" and not self.store.setting("forward_rejected_since"):
                self.store.set_setting("forward_rejected_since", now)
            if f["attempts"] == 0 or result == "rejected":
                self._log({"decision": "forward_failed", "forward": f["id"], "result": result,
                           "detail": detail}, inc)

    def ack(self, fp, status, starts_at):
        """Does the router own this alert episode (doc 7.4, L4)? OpenClaw's
        Grafana transform asks before it stays out of the way, with the
        alert's fingerprint, status and startsAt (12.6 f: per delivery, not
        per fingerprint). No while the router does not forward, or while
        OpenClaw rejects its deliveries (a token or mapping problem): then
        Grafana's direct route is the path."""
        if not self.forwarding or self.store.setting("forward_rejected_since"):
            return False
        if status not in ("firing", "resolved"):
            return False
        # starts_at None: a transform that does not send it yet (per
        # fingerprint, the phase-3 first version); "" is never acked.
        if starts_at is not None and not starts_at:
            return False
        return self.store.seen_since(fp, status, starts_at,
                                     self.clock.now() - self.cfg["openclaw"]["ack_window"])

    # ---- outcome records, /maint from Telegram, incident view ------------------------
    def record_outcome(self, body):
        rec = outcomes.validate(body, self.graph, self.playbooks,
                                lambda i: self.store.get(i) is not None)
        self.store.add_outcome(self.clock.now(), rec)
        inc = self.store.get(rec["incident"]) if rec["incident"] else None
        self._log({"decision": "outcome", **{k: v for k, v in rec.items() if k != "incident"}}, inc)
        return rec

    def maint_command(self, text, by="telegram"):
        cmd = maint_text.parse(text, self.graph, self.cfg["max_window"])
        if cmd[0] == "list":
            ws = self.store.windows()
            if not ws:
                return "No maintenance windows open."
            return "\n".join(f"{w['id']}: {', '.join(w['scope'])} until "
                             f"{datetime.fromtimestamp(w['expires_at']).astimezone():%H:%M} ({w['reason']})"
                             for w in ws)
        if cmd[0] == "close":
            if cmd[1] not in {w["id"] for w in self.store.windows()}:
                raise maint_text.BadCommand(f"No open window {cmd[1]}.")
            self.close_window(cmd[1], by)
            return f"Closed {cmd[1]}. Anything still failing in its scope is handled normally now."
        _, scope, seconds, reason = cmd
        wid = self.open_window(scope, seconds, reason, by)
        until = datetime.fromtimestamp(self.clock.now() + seconds).astimezone()
        return (f"Window {wid} open until {until:%H:%M}: {', '.join(scope)} and what depends on them. "
                f"Playbooks there are held and their alerts are only logged; security, data-at-risk and "
                f"UPS alerts still page. When it closes, anything still failing is handled normally. "
                f"Close early: /maint close {wid}")

    def incident_view(self, inc_id):
        inc = self.store.get(inc_id)
        if inc is None:
            return None
        return {"incident": inc, "decisions": self.store.decisions_for(inc_id),
                "forward": {k: v for k, v in (self.store.forward_for(inc_id) or {}).items() if k != "payload"},
                "outcome": self.store.outcome_for(inc_id)}

    # ---- summary -----------------------------------------------------------------
    def summary(self, days):
        """The weekly numbers for the routing review and the Monday digest
        (8.2, 8.4): decisions, playbook results, residue and pages, MTTR,
        outcomes, suppressions."""
        now = self.clock.now()
        since = now - days * 86400
        decisions = self.store.decisions_since(since)
        incidents = self.store.recent_incidents(since)
        by_decision, unmatched, would_run, residue, pages = {}, {}, {}, {}, {}
        per_pb = {}
        mttr = []
        for d in decisions:
            dec = d["decision"]
            by_decision[dec] = by_decision.get(dec, 0) + 1
            key = d.get("event", "?")
            if dec == "no_match":
                unmatched[key] = unmatched.get(key, 0) + 1
            if dec == "shadow":
                would_run[d.get("playbook", "?")] = would_run.get(d.get("playbook", "?"), 0) + 1
            if dec == "queued":
                bucket = pages if d.get("kind") == "page" else residue
                bucket[key] = bucket.get(key, 0) + 1
            if d.get("playbook") and dec in ("executed", "verified", "verify_failed", "failed", "denied",
                                              "rate_limited", "breaker_open"):
                c = per_pb.setdefault(d["playbook"], {})
                c[dec] = c.get(dec, 0) + 1
            if dec == "verified" and "duration_s" in d:
                mttr.append(d["duration_s"])
        verified = by_decision.get("verified", 0)
        acted = by_decision.get("executed", 0)
        fwd = self.store.forwards_since(since)
        outs = self.store.outcomes_since(since)
        today = self._today()
        return {
            "days": days, "mode": self.mode(), "forwarding": self.forwarding,
            "incidents": len(incidents), "decisions": by_decision, "auto_resolved": verified,
            "playbook_success_rate": (verified / acted) if acted else None,
            "playbooks": per_pb,
            "mttr_playbook_s": {"count": len(mttr), "avg": int(sum(mttr) / len(mttr)) if mttr else None,
                                "max": max(mttr) if mttr else None},
            "shadow_would_run": would_run,
            "top_unmatched": sorted(unmatched.items(), key=lambda kv: -kv[1])[:10],
            "residue": sorted(residue.items(), key=lambda kv: -kv[1])[:20],
            "pages": sorted(pages.items(), key=lambda kv: -kv[1])[:20],
            "forwards": {st: sum(1 for f in fwd if f["state"] == st)
                         for st in ("pending", "delivered", "expired", "skipped")}
                        | {"jit_paged": sum(1 for f in fwd if f["jit_at"])},
            "outcomes": {"count": len(outs),
                         "playbook_able": [o for o in outs if o.get("playbook_able")][:20],
                         "noise": sum(1 for o in outs if o.get("noise")),
                         "missed": [o for o in outs if o.get("found_by")][:20],
                         "by_fix_kind": {k: sum(1 for o in outs if o.get("fix_kind") == k)
                                         for k in outcomes.FIX_KINDS}},
            "escalations_without_outcome": sum(
                1 for f in fwd if f["state"] == "delivered" and not self.store.outcome_for(f["incident"])),
            "suppressions": supp.describe(self.suppressions, today),
            "breakers": self.store.breakers(),
            "open_windows": self.store.windows(),
        }
