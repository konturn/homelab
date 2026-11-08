"""Property tests: the TLA+ properties (doc section 12, docs/design/tla) checked
against the real engine with Hypothesis.

A rule-based state machine plays the TLA+ environment: Grafana delivers and
re-delivers alerts (and resolves them), the kill switch flips, deploys start
and end, windows open and close, the router restarts, time passes, and the
executor succeeds, refuses or times out. The fake executor checks the
safety properties at the moment each action would run, as `viol` does in
the spec:

  S1 one action per target   S2 only allowed actions    S3 kill switch
  S4 rate limit              S5 breaker holds           S6 one root cause
  S7 no action during deploy S8 never twice per incident / per alert episode
  S9, S11 page classes page (even in a window that does not name them)
  S10 no action in a maintenance window

Liveness (L1, L5, L7, L8) is checked at teardown: once the environment
calms down (deploys end, windows expire, alerts resolve, time passes),
every incident is in a terminal state and no lock is held.
"""

import unittest

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from router.config import ALLOWED_ACTIONS, ephemeral
from router.store import ACTIVE

from .helpers import ALERTS, CONFIG, World, event

NAMES = sorted(ALERTS)
PLAYBOOK_FOR = {"zig": "zigbee-silence-restart", "app": "restart-once", "appdup": "app-log-restart"}


class RouterMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.w = World()
        self.violations = []
        self.acted_incidents = set()
        self.w.executor.check = self.check_action

    # ---- the property checks at execution time ---------------------------------
    def check_action(self, action, target, incident_id):
        e = self.w.engine
        inc = e.store.get(incident_id)
        v = self.violations
        others = [i for i in e.store.active() if i["id"] != incident_id
                  and i["target"] == target and i["status"] in ("acting", "verifying")]
        if others:
            v.append(("S1", target))
        if action not in ALLOWED_ACTIONS or target in e.cfg["restart_denylist"] or ephemeral(target):
            v.append(("S2", action, target))
        if e.mode() != "active":
            v.append(("S3", e.mode()))
        if e.store.breaker(inc["playbook"], "active"):
            v.append(("S5", inc["playbook"]))
        if self.w.gitlab.deploying and action != "cancel_ci_job":
            v.append(("S7",))
        if e.dep_open(inc):
            v.append(("S6", inc["node"]))
        if e.in_maint(inc, e.playbooks[inc["playbook"]]):
            v.append(("S10", inc["node"]))
        if incident_id in self.acted_incidents:
            v.append(("S8", incident_id))
        reply = self.w.executor.reply
        if reply.get("ok") or reply.get("unknown"):      # a refusal did not act
            self.acted_incidents.add(incident_id)

    # ---- environment -------------------------------------------------------------
    @rule(name=st.sampled_from(NAMES))
    def deliver(self, name):
        self.w.engine.ingest(event(name))

    @rule(name=st.sampled_from(NAMES))
    def resolve(self, name):
        self.w.engine.ingest(event(name, "resolved"))

    @rule()
    def step(self):
        self.w.engine.step()

    @rule(mode=st.sampled_from([None, "notify-only", "shadow", "active"]))
    def kill_switch(self, mode):
        self.w.set_override(mode)

    @rule(on=st.booleans())
    def deploy(self, on):
        self.w.gitlab.deploying = on

    @rule(scope=st.sampled_from(["zigbee2mqtt", "mosquitto", "app", "site"]),
          minutes=st.integers(1, 120))
    def open_window(self, scope, minutes):
        self.w.engine.open_window([scope], minutes * 60, "test", "hypothesis")

    @rule()
    def close_windows(self):
        for win in self.w.engine.store.windows():
            self.w.engine.close_window(win["id"], "hypothesis")

    @rule(minutes=st.integers(1, 8 * 60))
    def time_passes(self, minutes):
        self.w.clock.advance(minutes * 60)

    @rule()
    def router_restart(self):
        self.w.new_engine()

    @rule(reply=st.sampled_from([
        {"ok": True}, {"ok": False, "refused": "deploy"}, {"ok": False, "refused": "maintenance"},
        {"ok": False, "refused": "kill_switch"}, {"ok": False, "refused": "dependency"},
        {"ok": False, "refused": "rate_limit"}, {"ok": False, "error": "boom"},
        {"ok": False, "unknown": True}]))
    def executor_behaviour(self, reply):
        self.w.executor.reply = reply

    # ---- invariants after every rule ------------------------------------------------
    @invariant()
    def no_violations(self):
        assert not self.violations, self.violations

    @invariant()
    def s1_locks_consistent(self):
        e = self.w.engine
        holders = {}
        for inc in e.store.active():
            if inc["status"] in ("acting", "verifying"):
                assert e.store.lock_holder(inc["target"]) == inc["id"], ("S1 lock", inc["id"])
                assert inc["target"] not in holders, ("S1", inc["target"])
                holders[inc["target"]] = inc["id"]

    @invariant()
    def s4_rate_limit(self):
        e = self.w.engine
        now = self.w.clock.now()
        for pb in e.pb_list:
            per, mx = pb.limits["per"], pb.limits["max_runs"]
            for target in {r["target"] for r in e.store.conn.execute(
                    "SELECT DISTINCT target FROM runs WHERE playbook = ?", (pb.id,))}:
                ts = e.store.runs_since(pb.id, target, "active", 0)
                for i, t in enumerate(ts):
                    window = [x for x in ts[i:] if x - t < per]
                    assert len(window) <= mx, ("S4", pb.id, target, len(window))
        assert now

    @invariant()
    def s5_no_run_with_open_breaker(self):
        e = self.w.engine
        for b in e.store.breakers():
            later = e.store.conn.execute(
                "SELECT COUNT(*) FROM runs WHERE playbook = ? AND mode = ? AND ts > ?",
                (b["playbook"], b["mode"], b["last_trigger"])).fetchone()[0]
            assert later == 0, ("S5", b)

    @invariant()
    def s9_s11_page_classes(self):
        e = self.w.engine
        for inc in e.store.active() + e.store.recent_incidents(0):
            if inc["page_class"]:
                assert inc["status"] in ("paged", "resolved") or (
                    inc["status"] == "maint" and e.in_maint(inc, page_cls=inc["page_class"])), ("S9/S11", inc)

    # ---- liveness at the end ------------------------------------------------------
    def teardown(self):
        try:
            if self.violations:
                return
            w = self.w
            w.gitlab.deploying = False
            w.set_override(None)
            w.executor.reply = {"ok": True}
            for _ in range(3):
                w.clock.advance(13 * 3600)          # windows expire (L7), verify deadlines pass
                w.engine.step()
            assert not w.engine.store.windows(), "L7: a window outlived its expiry"
            for name in NAMES:
                w.engine.ingest(event(name, "resolved"))
            for _ in range(6):
                w.clock.advance(15 * 60)
                w.engine.step()
            left = [(i["fp"], i["status"]) for i in w.engine.store.active()]
            assert not left, f"L1/L8: incidents never handled: {left}"
            assert not w.engine.store.locks(), f"L5: locks held: {w.engine.store.locks()}"
            assert not self.violations, self.violations
        finally:
            self.w.close()


RouterMachine.TestCase.settings = settings(
    max_examples=200, stateful_step_count=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
TestRouterProperties = RouterMachine.TestCase


FWD_CONFIG = dict(CONFIG, openclaw={"forward": True})


class ForwardingMachine(RouterMachine):
    """Phase 3: the same environment plus OpenClaw going up and down, with
    the router forwarding residue itself. Adds:

      B   an incident a playbook handled (verified) is never forwarded
      L1  every escalated or paged incident is eventually delivered to
          OpenClaw, or (OpenClaw down for longer than max_age) expires into
          the digest; a page never expires without having gone out through
          the JIT bot
      one forward per incident (no duplicate wake-ups)
    """

    def __init__(self):
        RuleBasedStateMachine.__init__(self)
        self.w = World(cfg=FWD_CONFIG)
        self.violations = []
        self.acted_incidents = set()
        self.w.executor.check = self.check_action

    @rule(up=st.booleans())
    def openclaw(self, up):
        self.w.openclaw.result = "ok" if up else "down"

    @rule()
    def pump(self):
        self.w.engine._pump_forwards()

    @invariant()
    def handled_never_forwarded(self):
        e = self.w.engine
        verified = {r["incident"] for r in e.store.conn.execute(
            "SELECT incident FROM decisions WHERE decision = 'verified'")}
        forwarded = {r["incident"] for r in e.store.conn.execute("SELECT incident FROM forwards")}
        assert not verified & forwarded, ("B", verified & forwarded)

    @invariant()
    def one_forward_per_incident(self):
        dup = self.w.engine.store.conn.execute(
            "SELECT incident, COUNT(*) c FROM forwards GROUP BY incident HAVING c > 1").fetchall()
        assert not dup, ("one forward per incident", [tuple(r) for r in dup])

    def teardown(self):
        try:
            if self.violations:
                return
            w = self.w
            w.gitlab.deploying = False
            w.set_override(None)
            w.executor.reply = {"ok": True}
            for _ in range(3):
                w.clock.advance(13 * 3600)
                w.engine.step()
                w.engine._pump_forwards()
            for name in NAMES:
                w.engine.ingest(event(name, "resolved"))
            w.openclaw.result = "ok"
            for _ in range(8):
                w.clock.advance(15 * 60)
                w.engine.step()
                w.engine._pump_forwards()
            e = w.engine
            left = [(i["fp"], i["status"]) for i in e.store.active()]
            assert not left, f"L1: incidents never handled: {left}"
            pending = e.store.pending_forwards()
            assert not pending, f"L1: forwards never delivered: {[(f['incident'], f['kind']) for f in pending]}"
            handed = e.store.conn.execute(
                "SELECT id FROM incidents WHERE status IN ('escalated', 'paged', 'shadowed')").fetchall()
            for (inc_id,) in handed:
                f = e.store.forward_for(inc_id)
                assert f is not None, f"L1: {inc_id} handed off but never queued for OpenClaw"
                if f["state"] == "expired":
                    assert f["kind"] != "page" or f["jit_at"], f"L1: page {inc_id} expired unseen"
        finally:
            self.w.close()


ForwardingMachine.TestCase.settings = settings(
    max_examples=150, stateful_step_count=40, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])
TestForwardingProperties = ForwardingMachine.TestCase


class TestMatchingIsDataOnly(unittest.TestCase):
    """S2 at intake: whatever an event says, only playbook data decides."""

    from hypothesis import given

    @given(labels=st.dictionaries(st.text(min_size=1, max_size=20), st.text(max_size=300), max_size=8),
           summary=st.text(max_size=500), rule=st.text(max_size=40))
    @settings(max_examples=100, deadline=None)
    def test_arbitrary_events_never_build_actions(self, labels, summary, rule):
        from router.events import from_grafana
        w = World()
        try:
            payload = {"alerts": [{"status": "firing", "fingerprint": "abcdef0123", "labels": labels,
                                   "annotations": {"summary": summary, "description": summary},
                                   "generatorURL": f"https://g/alerting/grafana/{rule}/view"}]}
            for ev in from_grafana(payload):
                w.engine.ingest(ev)
            for _ in range(3):
                w.engine.step()
            for action, target, _ in w.executor.calls:
                self.assertIn(action, ALLOWED_ACTIONS)
                self.assertNotIn(target, w.cfg["restart_denylist"])
                self.assertIsNone(ephemeral(target))
                self.assertRegex(target, r"^[a-z0-9][a-z0-9_.-]{0,62}$")
        finally:
            w.close()


class TestEphemeralNeverTargeted(unittest.TestCase):
    """S2: no unhealthy gitlab-runner build, canary or e2e container is ever
    restarted, whatever its name, in any mode."""

    from hypothesis import given

    @given(prefix=st.sampled_from(["runner-", "canary-", "e2e-"]),
           suffix=st.from_regex(r"\A[a-z0-9_.-]{0,50}\Z"))
    @settings(max_examples=50, deadline=None)
    def test_ephemeral_names_are_denied(self, prefix, suffix):
        from router.events import Event
        w = World()
        try:
            name = prefix + suffix
            w.engine.ingest(Event(source="grafana", kind="alert", fp="grafana:eph", status="firing",
                                  rule_uid="container-health-failure", alertname="health",
                                  labels={"container_name": name, "alertname": "health"}))
            for _ in range(3):
                w.engine.step()
            self.assertEqual(w.executor.calls, [])
            self.assertEqual(w.engine.store.latest_for_fp("grafana:eph")["status"], "escalated")
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
