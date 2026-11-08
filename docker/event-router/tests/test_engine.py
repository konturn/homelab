"""Scenario tests: the worked examples and the two TLC findings (doc 12.6)."""

import json
import os
import unittest

from router.engine import Engine

from .helpers import CONFIG, PLAYBOOKS, World, event


def status(w, name):
    return w.engine.store.latest_for_fp(f"grafana:{name}")["status"]


class Base(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.e = self.w.engine

    def tearDown(self):
        self.w.close()

    def run_steps(self, n=3):
        for _ in range(n):
            self.e.step()


class TestHappyPath(Base):
    def test_zigbee_restart_and_verify(self):
        self.e.ingest(event("zig"))
        self.run_steps()
        self.assertEqual(self.w.executor.calls, [("restart_container", "zigbee2mqtt",
                                                  self.e.store.latest_for_fp("grafana:zig")["id"])])
        self.assertEqual(status(self.w, "zig"), "verifying")
        self.e.ingest(event("zig", "resolved"))
        self.e.step()
        self.assertEqual(status(self.w, "zig"), "resolved")
        self.assertEqual(self.e.store.locks(), {})
        self.assertIn("verified", [d["decision"] for d in self.w.decisions()])

    def test_verify_failure_pages_and_releases(self):
        self.e.ingest(event("zig"))
        self.run_steps()
        self.w.clock.advance(11 * 60)
        self.e.step()
        self.assertEqual(status(self.w, "zig"), "paged")
        self.assertEqual(self.e.store.locks(), {})
        self.assertTrue(os.listdir(os.path.join(self.w.shared_dir, "outbox")))

    def test_no_playbook_escalates(self):
        self.e.ingest(event("mqtt"))
        self.e.step()
        self.assertEqual(status(self.w, "mqtt"), "escalated")
        self.assertEqual(self.w.executor.calls, [])

    def test_page_class_pages_even_in_maintenance(self):
        self.e.open_window(["site"], 3600, "rack day", "test")
        self.e.ingest(event("sec"))
        self.assertEqual(status(self.w, "sec"), "paged")


class TestDedupe(Base):
    def test_duplicates_are_one_incident(self):
        for _ in range(5):
            self.e.ingest(event("zig"))
            self.e.step()
        self.assertEqual(len(self.w.executor.calls), 1)
        inc = self.e.store.latest_for_fp("grafana:zig")
        self.assertEqual(inc["deliveries"], 5)

    def test_still_firing_after_escalation_is_not_new(self):
        self.e.ingest(event("mqtt"))
        self.e.step()
        first = self.e.store.latest_for_fp("grafana:mqtt")["id"]
        self.w.clock.advance(4 * 3600)       # Grafana repeat_interval
        self.e.ingest(event("mqtt"))
        self.assertEqual(self.e.store.latest_for_fp("grafana:mqtt")["id"], first)

    def test_refire_after_resolve_is_new_episode(self):
        self.e.ingest(event("mqtt"))
        self.e.step()
        first = self.e.store.latest_for_fp("grafana:mqtt")["id"]
        self.e.ingest(event("mqtt", "resolved"))
        self.e.ingest(event("mqtt"))
        self.assertNotEqual(self.e.store.latest_for_fp("grafana:mqtt")["id"], first)


class TestCorrelation(Base):
    def test_root_first_suppresses_symptom(self):
        self.e.ingest(event("mqtt"))
        self.e.ingest(event("zig"))
        self.assertEqual(status(self.w, "zig"), "symptom")
        self.run_steps()
        # mqtt escalated (handled), so zigbee re-enters handling on its own.
        self.assertEqual(status(self.w, "mqtt"), "escalated")
        self.assertIn(status(self.w, "zig"), ("verifying",))

    def test_root_arriving_second_still_suppresses(self):
        """12.6 (b): the symptom opened first, then its dependency failed."""
        self.e.ingest(event("zig"))
        self.e.ingest(event("mqtt"))
        self.e.step()
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "symptom")

    def test_dependency_fails_between_start_and_execute(self):
        """12.6 (b): Execute re-checks dependencies."""
        self.w.executor.reply = {"ok": True}
        self.e.ingest(event("zig"))
        inc = self.e.store.latest_for_fp("grafana:zig")
        pb = self.e.playbooks["zigbee-silence-restart"]
        self.e._start(inc, pb, "active")
        self.assertEqual(status(self.w, "zig"), "acting")
        self.e.ingest(event("mqtt"))
        self.e._execute(self.e.store.get(inc["id"]), pb)
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "symptom")
        self.assertEqual(self.e.store.locks(), {})


class TestExecutionTimeChecks(Base):
    def _acting(self):
        self.e.ingest(event("zig"))
        inc = self.e.store.latest_for_fp("grafana:zig")
        pb = self.e.playbooks["zigbee-silence-restart"]
        self.e._start(inc, pb, "active")
        return self.e.store.get(inc["id"]), pb

    def test_deploy_started_after_playbook_start_waits(self):
        """12.6 (a): a deploy that starts after StartPb holds the action."""
        inc, pb = self._acting()
        self.w.gitlab.deploying = True
        self.e._execute(inc, pb)
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "acting")
        self.w.gitlab.deploying = False
        self.e._execute(self.e.store.get(inc["id"]), pb)
        self.assertEqual(len(self.w.executor.calls), 1)

    def test_window_opened_after_playbook_start_parks(self):
        """12.6 (a): a window that opens after StartPb parks the incident."""
        inc, pb = self._acting()
        self.e.open_window(["mqtt"], 3600, "broker upgrade", "test")
        self.e._execute(inc, pb)
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "maint")
        self.assertEqual(self.e.store.locks(), {})

    def test_kill_switch_flipped_mid_action(self):
        inc, pb = self._acting()
        self.w.set_override("notify-only")
        self.e._execute(inc, pb)
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "escalated")

    def test_executor_refusals(self):
        for refused, expect in (("kill_switch", "escalated"), ("maintenance", "maint"),
                                ("rate_limit", "paged"), ("dependency", "paged"), ("deploy", "acting")):
            w = World()
            try:
                w.executor.reply = {"ok": False, "refused": refused}
                w.engine.ingest(event("zig"))
                for _ in range(2):
                    w.engine.step()
                self.assertEqual(status(w, "zig"), expect, refused)
                if expect != "acting":
                    self.assertEqual(w.engine.store.locks(), {}, refused)
            finally:
                w.close()

    def test_unknown_result_is_never_retried(self):
        self.w.executor.reply = {"ok": False, "unknown": True, "detail": "timeout"}
        self.e.ingest(event("zig"))
        self.run_steps(5)
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(status(self.w, "zig"), "verifying")


class TestLimits(Base):
    def test_rate_limit_trips_breaker_and_pages(self):
        self.e.ingest(event("zig"))
        self.run_steps()
        self.e.ingest(event("zig", "resolved"))
        self.e.step()
        self.w.clock.advance(3600)
        self.e.ingest(event("zig"))
        self.e.step()
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(status(self.w, "zig"), "paged")
        self.assertTrue(self.e.store.breaker("zigbee-silence-restart", "active"))
        # The breaker holds after the window resets, until 24h with no triggers.
        self.e.ingest(event("zig", "resolved"))
        self.w.clock.advance(7 * 3600)
        self.e.ingest(event("zig"))
        self.e.step()
        self.assertEqual(len(self.w.executor.calls), 1)
        self.w.clock.advance(25 * 3600)
        self.e.reconcile()
        self.assertIsNone(self.e.store.breaker("zigbee-silence-restart", "active"))

    def test_global_limit_switches_to_notify_only(self):
        cfg = dict(CONFIG, limits=dict(CONFIG["limits"], global_actions_per_hour=1))
        w = World(cfg=cfg)
        try:
            w.engine.ingest(event("zig"))
            w.engine.ingest(event("app"))
            for _ in range(3):
                w.engine.step()
            # The second start hits the limit: notify-only stops everything,
            # including the action already started.
            self.assertLessEqual(len(w.executor.calls), 1)
            self.assertEqual(w.engine.mode(), "notify-only")
        finally:
            w.close()

    def test_lock_serialises_one_target(self):
        """S1: two playbooks on one container never act together."""
        self.w.executor.check = None
        self.e.ingest(event("app"))
        self.e.ingest(event("appdup"))
        self.run_steps(2)
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(sorted([status(self.w, "app"), status(self.w, "appdup")]), ["open", "verifying"])


class TestShadowAndModes(Base):
    def test_shadow_never_calls_the_executor(self):
        cfg = dict(CONFIG, mode="shadow")
        w = World(cfg=cfg)
        try:
            w.engine.ingest(event("zig"))
            w.engine.step()
            self.assertEqual(w.executor.calls, [])
            self.assertEqual(status(w, "zig"), "shadowed")
            d = [x for x in w.decisions() if x["decision"] == "shadow"][0]
            self.assertEqual(d["action"], "restart_container:zigbee2mqtt")
        finally:
            w.close()

    def test_playbook_in_shadow_while_router_active(self):
        pbs = [dict(p, mode="shadow") if p["id"] == "zigbee-silence-restart" else p for p in PLAYBOOKS]
        w = World(playbooks=pbs)
        try:
            w.engine.ingest(event("zig"))
            w.engine.ingest(event("app"))
            w.engine.step()
            w.engine.step()
            self.assertEqual([c[1] for c in w.executor.calls], ["app"])
        finally:
            w.close()

    def test_host_override_wins_and_mode_change_logged(self):
        self.w.set_override("notify-only")
        self.e.check_mode_change()
        self.assertEqual(self.e.mode(), "notify-only")
        self.e.ingest(event("zig"))
        self.e.step()
        self.assertEqual(self.w.executor.calls, [])
        self.assertIn("mode_changed", [d["decision"] for d in self.w.decisions()])


class TestPersistence(Base):
    def test_restart_keeps_incidents_and_never_acts_twice(self):
        """S8 across a restart: the re-delivered alert is the same incident."""
        self.e.ingest(event("zig"))
        self.run_steps()
        self.w.new_engine()
        self.e = self.w.engine
        self.e.ingest(event("zig"))
        self.run_steps()
        self.assertEqual(len(self.w.executor.calls), 1)

    def test_restart_during_execution_goes_to_verification(self):
        self.e.ingest(event("zig"))
        inc = self.e.store.latest_for_fp("grafana:zig")
        self.e._start(inc, self.e.playbooks["zigbee-silence-restart"], "active")
        self.e.store.update(inc["id"], self.w.clock.now(), exec_started_at=self.w.clock.now())
        self.w.new_engine().step()
        self.assertEqual(self.w.executor.calls, [])
        self.assertEqual(status(self.w, "zig"), "verifying")

    def test_inbox_survives_restart(self):
        self.e.store.inbox_put(self.w.clock.now(), [event("zig").to_dict()])
        eng = self.w.new_engine()
        eng.tick()
        eng.tick()
        self.assertEqual(len(self.w.executor.calls), 1)


class TestMaintenance(Base):
    def test_window_suppresses_then_recovery_resumes(self):
        self.e.open_window(["zigbee2mqtt"], 1800, "firmware", "test")
        self.e.ingest(event("zig"))
        self.run_steps()
        self.assertEqual(status(self.w, "zig"), "maint")
        self.assertEqual(self.w.executor.calls, [])
        self.w.clock.advance(1801)
        self.run_steps()
        self.assertEqual(len(self.w.executor.calls), 1)     # L8: still failing -> playbook

    def test_dependents_of_scope_are_in_scope(self):
        self.e.open_window(["mosquitto"], 1800, "broker", "test")
        self.e.ingest(event("zig"))
        self.assertEqual(status(self.w, "zig"), "maint")

    def test_healthy_after_window_closes(self):
        self.e.open_window(["zigbee2mqtt"], 1800, "firmware", "test")
        self.e.ingest(event("zig"))
        self.e.ingest(event("zig", "resolved"))
        self.assertEqual(status(self.w, "zig"), "resolved")

    def test_window_limits(self):
        with self.assertRaises(ValueError):
            self.e.open_window(["zigbee2mqtt"], 13 * 3600, "too long", "test")
        with self.assertRaises(ValueError):
            self.e.open_window(["zigbee2mqtt"], 3600, "", "test")
        with self.assertRaises(ValueError):
            self.e.open_window(["nope"], 3600, "x", "test")

    def test_windows_published_for_the_executor(self):
        wid = self.e.open_window(["zigbee2mqtt"], 1800, "firmware", "test")
        with open(os.path.join(self.w.shared_dir, "windows.json")) as f:
            self.assertEqual(json.load(f)["windows"][0]["id"], wid)
        self.e.close_window(wid, "test")
        with open(os.path.join(self.w.shared_dir, "windows.json")) as f:
            self.assertEqual(json.load(f)["windows"], [])


class TestReconciler(Base):
    def test_orphaned_deploy_job_is_cancelled_even_during_deploy(self):
        now = self.w.clock.now()
        self.w.gitlab.deploying = True
        self.w.gitlab.jobs = [{"id": 101, "name": "router:deploy", "started_at": now - 3 * 3600}]
        self.e.reconcile()
        self.run_steps(2)
        self.assertEqual(self.w.executor.calls[0][:2], ("cancel_ci_job", "101"))
        self.w.gitlab.status["101"] = "canceled"
        self.w.gitlab.jobs = []
        self.e.step()
        inc = self.e.store.latest_for_fp("reconciler:ci-orphan:101")
        self.assertEqual(inc["status"], "resolved")

    def test_young_job_is_left_alone(self):
        now = self.w.clock.now()
        self.w.gitlab.jobs = [{"id": 102, "name": "router:deploy", "started_at": now - 600},
                              {"id": 103, "name": "netgear:firmware", "started_at": now - 9 * 3600}]
        self.e.reconcile()
        self.e.step()
        self.assertEqual(self.w.executor.calls, [])


class TestQuietHours(Base):
    def test_non_urgent_waits_until_morning(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        cfg = dict(CONFIG, quiet_hours={"start": "23:00", "end": "08:00", "tz": "America/New_York"})
        w = World(cfg=cfg)
        try:
            w.clock.t = datetime(2026, 11, 8, 2, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
            due = w.engine.quiet_until(w.clock.t)
            self.assertEqual(datetime.fromtimestamp(due, ZoneInfo("America/New_York")).hour, 8)
            w.clock.t = datetime(2026, 11, 8, 12, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
            self.assertEqual(w.engine.quiet_until(w.clock.t), w.clock.t)
        finally:
            w.close()


if __name__ == "__main__":
    unittest.main()
