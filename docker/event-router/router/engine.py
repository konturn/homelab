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

Phases 1-2: OpenClaw and email still receive every alert straight from
Grafana, so "escalated" and "paged" for Grafana alerts are recorded, not sent
(the existing routes deliver them). Facts only the router knows (a breaker
tripped, a remediation failed) are queued for Noah; event-router-control on
the host sends them over Telegram through the JIT bot (decision 3).
"""

import hashlib
import json
import os
import sys
import zoneinfo
from datetime import datetime, timedelta

from . import modes
from .config import match_playbook, page_class
from .events import Event
from .shared import SharedState
from .store import ACTIVE, TERMINAL

DEDUPE_TERMINAL_FOR = 24 * 3600   # a still-firing alert stays one incident this long
OVERRIDE_RENOTIFY = 24 * 3600     # decision 13
DEPLOY_EXEMPT = ("cancel_ci_job",)  # S7: the playbook that frees the lock


def _pb_version(pb):
    return hashlib.sha1(json.dumps(pb.raw, sort_keys=True, default=str).encode()).hexdigest()[:10]


class Engine:
    def __init__(self, cfg, playbooks, graph, store, clock, executor=None,
                 gitlab=None, host_override_path=None, shared=None, out=None):
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
        if ev.status == "resolved":
            if inc is None:
                return
            self._set(inc, alert_state="resolved")
            if inc["status"] in ("open", "symptom", "maint"):           # AlertResolves
                self._close(inc, "resolved", note="alert resolved before any action")
                self._log({"decision": "resolved", "reason": "alert resolved"}, inc)
            elif inc["status"] in TERMINAL and inc["closed_at"] is None:
                self._set(inc, closed_at=now)
            return

        if inc is not None and (inc["status"] in ACTIVE or (
                inc["alert_state"] == "firing" and now - inc["opened_at"] < DEDUPE_TERMINAL_FOR)):
            self._set(inc, deliveries=inc["deliveries"] + 1)            # same incident
            self._log({"decision": "deduped", "deliveries": inc["deliveries"]}, inc)
            return

        pb = match_playbook(self.pb_list, ev) if ev.kind in ("alert", "ci_orphan") else None
        node = pb.node_for(ev, self.graph) if pb else self._node_from_labels(ev)
        target = pb.target_for(ev) if pb else None
        pcls, urgent = page_class(self.cfg, ev) if ev.kind == "alert" else (None, False)
        fields = dict(fp=ev.fp, source=ev.source, kind=ev.kind, rule_uid=ev.rule_uid,
                      alertname=ev.alertname, node=node, labels=ev.labels,
                      playbook=pb.id if pb else None, target=target,
                      alert_state="firing", page_class=pcls, status="open")
        inc_id = self.store.new_incident(now, **fields)
        inc = self.store.get(inc_id)
        extra = {"summary": ev.summary} if ev.summary else {}

        if pcls and not self.in_maint(inc, pb, page_cls=pcls):           # S9, S11
            self._close(inc, "paged")
            self._log({"decision": "paged", "page_class": pcls, "urgent": urgent,
                       "delivery": "grafana-direct", **extra}, inc)
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
            self._close(inc, "escalated", note="burst")
            self._log({"decision": "escalated", "reason": "burst: many failures without a common ancestor",
                       "delivery": "grafana-direct", **extra}, inc)
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

    def _forward(self, inc, decision, reason=None):
        self._close(inc, "escalated")
        rec = {"decision": decision, "delivery": "grafana-direct"}
        if reason:
            rec["reason"] = reason
        self._log(rec, inc)

    def _trip(self, inc, pb, pmode, decision, reason):
        now = self.clock.now()
        self.store.open_breaker(pb.id, pmode, now)
        self._close(inc, "paged")
        self._log({"decision": decision, "reason": reason, "breaker": "open", "mode": pmode}, inc)
        if pmode == "active":
            self._notify(f"Event router: playbook {pb.id} stopped ({reason}) for {inc['target']}; "
                         f"incident {inc['id']} goes to OpenClaw.", urgent=False)

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
        for kind, within in pb.preconditions:
            if kind == "no_recent_deploy":
                last = self.gitlab.last_deploy_finished(self.cfg["deploy_jobs"]) if self.gitlab else None
                if last is None:
                    return self._forward(inc, "denied", reason="precondition: cannot check recent deploys")
                if now - last < within:
                    return self._forward(inc, "denied", reason=f"precondition: a deploy finished "
                                                               f"{int((now - last) // 60)}m ago")
        holder = self.store.lock_holder(target)
        if holder and holder != inc["id"]:
            return self._wait(inc, f"lock on {target} held by {holder}")
        action = f"{pb.action}:{target}"
        if pmode == "shadow":
            with self.store.tx():
                self.store.add_run(pb.id, target, "shadow", now, inc["id"])
            self._close(inc, "shadowed")
            self._log({"decision": "shadow", "action": action, "mode": "shadow",
                       "would": "run", "delivery": "grafana-direct"}, inc)
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
        res = self.executor.execute(pb.action, inc["target"], inc["id"])
        now = self.clock.now()
        action = f"{pb.action}:{inc['target']}"
        if res.get("ok"):
            self.store.add_action(inc["id"], pb.id, pb.action, inc["target"], now, "ok")
            self._set(inc, status="verifying", exec_count=inc["exec_count"] + 1,
                      verify_deadline=now + pb.verify_within, waiting=None)
            return self._log({"decision": "executed", "action": action, "result": "ok",
                              "detail": res.get("detail")}, inc)
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
        self._close(inc, "paged")
        self._log({"decision": "denied" if refused else "failed", "action": action, "reason": reason}, inc)
        self._notify(f"Event router: {action} for {inc['alertname'] or inc['fp']} was not done ({reason}). "
                     f"OpenClaw has the alert.", urgent=False)

    def _verify(self, inc, pb):
        now = self.clock.now()
        ok = False
        if pb.verify_kind == "alert_resolved":
            ok = inc["alert_state"] == "resolved"
        elif pb.verify_kind == "ci_job_finished":
            status = self.gitlab.job_status(inc["target"]) if self.gitlab else None
            ok = status is not None and status not in ("running", "pending", "created")
        if ok:
            self._release(inc)
            self._close(inc, "resolved")
            self._log({"decision": "verified", "verify": "ok", "notify": pb.on_success,
                       "duration_s": int(now - inc["opened_at"])}, inc)
            if pb.on_success == "page":
                self._notify(f"Event router: {pb.id} fixed {inc['target']}.", urgent=False)
        elif now >= (inc["verify_deadline"] or now):
            self._release(inc)
            self._close(inc, "paged")
            self._log({"decision": "verify_failed", "verify": "failed", "notify": pb.on_failure}, inc)
            self._notify(f"Event router: {pb.id} acted on {inc['target']} but verification failed "
                         f"({inc['alertname'] or inc['fp']}). OpenClaw has the alert.", urgent=False)

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
                       "reason": "still failing after maintenance", "delivery": "grafana-direct"}, inc)
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
        now = self.clock.now()
        if now - self._last_reconcile >= self.cfg["reconcile_interval"]:
            self._last_reconcile = now
            self.reconcile()

    def reconcile(self):
        now = self.clock.now()
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

    # ---- summary -----------------------------------------------------------------
    def summary(self, days):
        since = self.clock.now() - days * 86400
        decisions = self.store.decisions_since(since)
        incidents = self.store.recent_incidents(since)
        by_decision, unmatched, would_run = {}, {}, {}
        for d in decisions:
            by_decision[d["decision"]] = by_decision.get(d["decision"], 0) + 1
            if d["decision"] == "no_match":
                key = d.get("event", "?")
                unmatched[key] = unmatched.get(key, 0) + 1
            if d["decision"] == "shadow":
                key = d.get("playbook", "?")
                would_run[key] = would_run.get(key, 0) + 1
        verified = by_decision.get("verified", 0)
        acted = by_decision.get("executed", 0)
        return {
            "days": days, "mode": self.mode(), "incidents": len(incidents),
            "decisions": by_decision, "auto_resolved": verified,
            "playbook_success_rate": (verified / acted) if acted else None,
            "shadow_would_run": would_run,
            "top_unmatched": sorted(unmatched.items(), key=lambda kv: -kv[1])[:10],
            "breakers": self.store.breakers(),
            "open_windows": self.store.windows(),
        }
