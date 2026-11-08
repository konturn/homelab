"""Phase 3: residue to OpenClaw, outcome records, GitLab events, suppressions,
digest-only classes, /maint from Telegram, CI retries (doc 9, 14)."""

import datetime as dt
import http.client
import json
import os
import unittest

from router import maint_text, outcomes
from router import suppressions as supp
from router.config import ConfigError, Playbook, parse_config, validate
from router.events import Event, from_gitlab
from router.server import State, serve

from .helpers import CONFIG, PLAYBOOKS, World, event

DAY = 1_800_000_000 + 11 * 3600          # 14:00 in New York (FakeClock's default is 03:00)

FWD_CONFIG = dict(CONFIG, openclaw={"forward": True}, gitlab_events={"projects": [4, 14]},
                  quiet_hours={"start": "23:00", "end": "08:00", "tz": "America/New_York"},
                  digest_only=[{"source": "grafana", "rule_uid": "container-log-flood",
                                "labels": {"container": "zigbee2mqtt"}, "reason": "routine Zigbee work"},
                               {"source": "gitlab", "kind": "pipeline_failed",
                                "labels": {"handled_by": "automerge-rollback"}, "reason": "rolled back"}],
                  page_classes={"security": {"urgent": True, "rules": [{"rule_uid": "failed-ssh-logins"}]},
                                "host_not_reporting": {"rules": [{"rule_uid": "host-metrics-stale"}]}})

CI_PLAYBOOKS = PLAYBOOKS + [
    {"id": "ci-system-retry", "mode": "active",
     "match": {"source": "gitlab", "kind": "ci_job_failed", "labels": {"project_id": "4", "failure_class": "system"}},
     "node": "gitlab", "action": {"type": "retry_ci_job", "target_from_event": "job_id"},
     "verify": {"ci_job_succeeded": "75m"}, "limits": {"max_runs": 1, "per": "24h", "cooldown": "0s"},
     "on_failure": {"notify": "agent"}},
    {"id": "ci-orphan-retry", "mode": "active",
     "match": {"source": "reconciler", "kind": "ci_orphan_cancelled"},
     "node": "gitlab", "action": {"type": "retry_ci_job", "target_from_event": "job_id"},
     "verify": {"ci_job_succeeded": "75m"}, "limits": {"max_runs": 1, "per": "24h", "cooldown": "0s"},
     "on_failure": {"notify": "agent"}},
]


def grafana(rule_uid, fp, status="firing", **labels):
    return Event(source="grafana", kind="alert", fp=f"grafana:{fp}", status=status, rule_uid=rule_uid,
                 alertname=rule_uid, labels=dict(labels, alertname=rule_uid))


class Fwd(unittest.TestCase):
    cfg = FWD_CONFIG
    playbooks = None
    sups = ()

    def setUp(self):
        self.w = World(cfg=self.cfg, playbooks=self.playbooks, suppressions=self.sups)
        self.w.clock.t = DAY
        self.e = self.w.engine

    def tearDown(self):
        self.w.close()

    def tick(self, n=3):
        for _ in range(n):
            self.e.tick()

    def inc(self, fp):
        return self.e.store.latest_for_fp(fp)

    def decisions(self, name):
        return [d for d in self.w.decisions() if d["decision"] == name]


class TestResidue(Fwd):
    def test_unmatched_goes_to_openclaw_with_context(self):
        self.e.ingest(event("mqtt"))
        self.tick()
        self.assertEqual(self.inc("grafana:mqtt")["status"], "escalated")
        [p] = self.w.openclaw.delivered
        self.assertEqual(p["kind"], "residue")
        self.assertEqual(p["incident"]["rule_uid"], "critical-service-down")
        self.assertEqual(p["outcome"]["incident"], self.inc("grafana:mqtt")["id"])
        self.assertIsNone(p["page"])
        self.assertTrue(self.decisions("forwarded"))

    def test_handled_events_never_wake_openclaw(self):              # decision B
        self.e.ingest(event("zig"))
        self.tick()
        self.e.ingest(event("zig", "resolved"))
        self.tick()
        self.assertEqual(self.inc("grafana:zig")["status"], "resolved")
        self.assertEqual(self.w.openclaw.delivered, [])
        self.assertEqual(self.w.openclaw.attempts, 0)

    def test_repeats_are_forwarded_once(self):
        for _ in range(3):
            self.e.ingest(event("mqtt"))
            self.tick()
            self.w.clock.advance(4 * 3600)
        self.assertEqual(len(self.w.openclaw.delivered), 1)

    def test_shadow_playbook_event_is_residue(self):
        pbs = [dict(p, mode="shadow") for p in PLAYBOOKS]
        self.w.pbs = [Playbook(p, "test") for p in pbs]
        self.e = self.w.new_engine()
        self.e.ingest(event("zig"))
        self.tick()
        self.assertEqual(self.w.executor.calls, [])
        [p] = self.w.openclaw.delivered
        self.assertIn("shadow", p["reason"])

    def test_verify_failure_pages_through_openclaw(self):
        self.e.ingest(event("zig"))
        self.tick()
        self.w.clock.advance(11 * 60)
        self.tick()
        self.assertEqual(self.inc("grafana:zig")["status"], "paged")
        [p] = self.w.openclaw.delivered
        self.assertEqual(p["kind"], "page")
        self.assertEqual(p["page"]["class"], "failed_remediation")
        self.assertFalse(p["page"]["email"])
        self.assertEqual(self.w.outbox(), [])           # OpenClaw pages Noah, not the router

    def test_executor_gets_the_playbook_id(self):
        self.e.ingest(event("zig"))
        self.tick()
        self.assertEqual(self.w.executor.playbooks, ["zigbee-silence-restart"])

    def test_recent_deploy_refusal_is_residue_dependency_pages(self):
        self.w.executor.reply = {"ok": False, "refused": "recent_deploy", "detail": "recreated 10m ago"}
        self.e.ingest(event("app"))
        self.tick()
        self.assertEqual(self.inc("grafana:app")["status"], "escalated")
        self.assertEqual(self.w.openclaw.delivered[-1]["kind"], "residue")
        self.w.executor.reply = {"ok": False, "refused": "dependency", "detail": "down: x"}
        self.e.ingest(event("zig"))
        self.tick()
        self.assertEqual(self.inc("grafana:zig")["status"], "paged")
        self.assertEqual(self.w.openclaw.delivered[-1]["kind"], "page")


class TestOpenClawDown(Fwd):
    def test_residue_waits_and_is_delivered_when_back(self):
        self.w.openclaw.result = "down"
        self.e.ingest(event("mqtt"))
        self.tick()
        self.assertEqual(self.w.openclaw.delivered, [])
        self.assertEqual(self.w.outbox(), [])            # residue is not a page
        self.w.clock.advance(3600)
        self.tick()
        self.w.openclaw.result = "ok"
        self.w.clock.advance(600)
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertGreaterEqual(p["queued_s"], 3600)
        self.assertEqual(self.e.summary(1)["forwards"]["delivered"], 1)

    def test_page_goes_out_through_the_jit_bot_once(self):
        self.w.openclaw.result = "down"
        self.e.ingest(event("sec"))
        self.tick()
        self.w.clock.advance(150)
        self.tick()
        self.w.clock.advance(600)
        self.tick()
        [m] = self.w.outbox()
        self.assertTrue(m["urgent"])
        self.assertIn("OpenClaw unreachable", m["text"])
        self.w.openclaw.result = "ok"
        self.w.clock.advance(600)
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertTrue(p["jit_paged"])

    def test_old_residue_expires_into_the_digest(self):
        self.w.openclaw.result = "down"
        self.e.ingest(event("mqtt"))
        self.tick()
        self.w.clock.advance(7 * 3600)
        self.tick()
        self.w.openclaw.result = "ok"
        self.tick()
        self.assertEqual(self.w.openclaw.delivered, [])
        self.assertTrue(self.decisions("forward_expired"))

    def test_only_the_oldest_item_is_tried_while_down(self):
        self.w.openclaw.result = "down"
        for name in ("mqtt", "app"):
            self.w.executor.reply = {"ok": False, "refused": "recent_deploy"}
            self.e.ingest(event(name))
        self.e.tick()
        self.assertEqual(self.w.openclaw.attempts, 1)


class TestQuietHoursAndAck(Fwd):
    def test_non_urgent_page_held_until_morning(self):
        self.w.clock.t = DAY - 13 * 3600 + 2 * 3600      # 03:00
        self.e.ingest(grafana("host-metrics-stale", "h1"))
        self.tick()
        self.assertEqual(self.w.openclaw.delivered, [])
        self.w.clock.advance(5 * 3600 + 60)              # 08:01
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertTrue(p["page"]["held_for_quiet_hours"])

    def test_held_page_that_cleared_overnight_is_skipped(self):
        self.w.clock.t = DAY - 13 * 3600 + 2 * 3600
        self.e.ingest(grafana("host-metrics-stale", "h1"))
        self.tick()
        self.e.ingest(grafana("host-metrics-stale", "h1", "resolved"))
        self.w.clock.advance(6 * 3600)
        self.tick()
        self.assertEqual(self.w.openclaw.delivered, [])
        self.assertTrue(self.decisions("forward_skipped"))

    def test_urgent_page_is_not_held(self):
        self.w.clock.t = DAY - 13 * 3600 + 2 * 3600
        self.e.ingest(event("sec"))
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 1)

    def test_residue_in_quiet_hours_is_flagged_not_held(self):
        self.w.clock.t = DAY - 13 * 3600 + 2 * 3600
        self.e.ingest(event("mqtt"))
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertTrue(p["quiet_hours"])

    def test_ack(self):
        self.e.store.inbox_put(self.w.clock.now(), [event("mqtt").to_dict()])
        self.assertTrue(self.e.ack("grafana:mqtt", "firing"))
        self.assertFalse(self.e.ack("grafana:mqtt", "resolved"))
        self.assertFalse(self.e.ack("grafana:other", "firing"))
        self.w.clock.advance(7 * 3600)
        self.assertFalse(self.e.ack("grafana:mqtt", "firing"))

    def test_rejected_deliveries_stop_the_ack(self):
        self.e.store.inbox_put(self.w.clock.now(), [event("mqtt").to_dict()])
        self.w.openclaw.result = "rejected"
        self.tick()
        self.assertFalse(self.e.ack("grafana:mqtt", "firing"))
        self.w.openclaw.result = "ok"
        self.w.clock.advance(600)
        self.tick()
        self.assertTrue(self.e.ack("grafana:mqtt", "firing"))


class TestNoForwarding(unittest.TestCase):
    """forward: false keeps phases 1-2: nothing queued, nothing acked."""

    def test_phase2_behaviour(self):
        w = World()
        try:
            w.clock.t = DAY
            w.engine.store.inbox_put(w.clock.now(), [event("mqtt").to_dict()])
            for _ in range(3):
                w.engine.tick()
            self.assertEqual(w.openclaw.delivered, [])
            self.assertFalse(w.engine.ack("grafana:mqtt", "firing"))
            self.assertEqual([d["delivery"] for d in w.decisions() if d["decision"] == "no_match"],
                             ["grafana-direct"])
        finally:
            w.close()


class TestDigestAndSuppressions(Fwd):
    sups = supp.parse({"suppressions": [
        {"id": "app-noise", "match": {"source": "grafana", "rule_uid": "critical-service-down",
                                      "labels": {"container_name": "mosquitto"}},
         "reason": "broker is being replaced this month", "until": "2027-01-20"}]})

    def test_digest_only(self):
        self.e.ingest(grafana("container-log-flood", "lf", container="zigbee2mqtt"))
        self.tick()
        self.assertEqual(self.inc("grafana:lf")["status"], "digest")
        self.assertEqual(self.w.openclaw.delivered, [])

    def test_suppression_applies_until_it_expires(self):                 # L6
        self.e.ingest(event("mqtt"))
        self.tick()
        self.assertEqual(self.inc("grafana:mqtt")["status"], "suppressed")
        self.e.ingest(event("mqtt", "resolved"))
        self.w.clock.advance(10 * 86400)
        self.e.ingest(event("mqtt"))
        self.tick()
        self.assertEqual(self.inc("grafana:mqtt")["status"], "escalated")
        self.assertTrue(self.e.summary(30)["suppressions"][0]["expired"])

    def test_suppressed_root_still_suppresses_dependents(self):          # S6
        self.e.ingest(event("mqtt"))
        self.e.ingest(event("zig"))
        self.tick()
        self.assertEqual(self.inc("grafana:zig")["status"], "symptom")

    def test_suppression_validation(self):
        cfg = parse_config(FWD_CONFIG)
        today = dt.date(2027, 1, 15)
        far = supp.parse({"suppressions": [{"id": "x", "match": {"source": "grafana", "rule_uid": "r"},
                                            "reason": "a long enough reason", "until": "2028-06-01"}]})
        self.assertTrue(supp.validate(far, cfg, today))
        page = supp.parse({"suppressions": [{"id": "x", "match": {"source": "grafana",
                                                                  "rule_uid": "failed-ssh-logins"},
                                             "reason": "a long enough reason", "until": "2027-02-01"}]})
        self.assertTrue(supp.validate(page, cfg, today))
        for bad in ({"id": "x", "match": {"source": "grafana", "rule_uid": "r"}, "until": "2027-02-01"},
                    {"id": "x", "match": {"source": "grafana", "rule_uid": "r"}, "reason": "long enough reason"},
                    {"id": "x", "match": {"source": "grafana", "rule_uid": "r"}, "reason": "short",
                     "until": "2027-02-01"}):
            with self.assertRaises(ConfigError):
                supp.parse({"suppressions": [bad]})

    def test_page_class_is_never_suppressed(self):
        sups = supp.parse({"suppressions": [{"id": "x", "match": {"source": "grafana",
                                                                  "rule_uid": "failed-ssh-logins"},
                                             "reason": "a long enough reason", "until": "2027-02-01"}]})
        self.e.suppressions = sups
        self.e.ingest(event("sec"))
        self.assertEqual(self.inc("grafana:sec")["status"], "paged")

    def test_digest_only_cannot_cover_a_page_class_or_a_playbook(self):
        cfg = parse_config(dict(FWD_CONFIG, digest_only=[
            {"source": "grafana", "rule_uid": "failed-ssh-logins", "reason": "x"},
            {"source": "grafana", "rule_uid": "zigbee-radio-silence", "reason": "x"}]))
        errs = validate(cfg, self.w.pbs, self.w.graph)
        self.assertEqual(len(errs), 2, errs)


class TestOutcomes(Fwd):
    def good(self, inc_id, **kw):
        return dict({"incident": inc_id, "root_cause": "coordinator stopped forwarding frames",
                     "root_node": "slzb_coordinator", "fix": "restart_container:zigbee2mqtt",
                     "fix_kind": "allowlisted_action", "playbook_able": True, "existing_playbook": None,
                     "noise": False, "mr": None}, **kw)

    def test_valid_outcome_is_logged(self):
        self.e.ingest(event("mqtt"))
        self.tick()
        inc = self.inc("grafana:mqtt")["id"]
        self.e.record_outcome(self.good(inc))
        [d] = self.decisions("outcome")
        self.assertEqual(d["incident"], inc)
        self.assertTrue(self.e.summary(1)["outcomes"]["playbook_able"])
        self.assertEqual(self.e.summary(1)["escalations_without_outcome"], 0)

    def test_invalid_outcomes(self):
        self.e.ingest(event("mqtt"))
        self.tick()
        inc = self.inc("grafana:mqtt")["id"]
        for bad in (self.good("inc-000000000000"), self.good(inc, fix="rm -rf /"),
                    self.good(inc, fix="power_off:ups"), self.good(inc, fix_kind="config_change"),
                    self.good(inc, root_node="nope"), self.good(inc, extra=1),
                    self.good(inc, playbook_able=True, fix_kind="hardware", fix=""),
                    self.good(inc, mr="https://evil"), self.good(None)):
            with self.assertRaises(outcomes.BadOutcome, msg=str(bad)):
                self.e.record_outcome(bad)

    def test_missed_incident_from_a_cron(self):
        self.e.record_outcome(self.good(None, found_by="cron:main-pipeline-fix", fix="", fix_kind="config_change",
                                        playbook_able=False))
        self.assertTrue(self.e.summary(1)["outcomes"]["missed"])


class TestMaintText(Fwd):
    def test_open_list_close(self):
        r = self.e.maint_command("/maint zigbee2mqtt,mqtt 2h firmware update on the coordinator")
        self.assertIn("open until", r)
        [w] = self.e.store.windows()
        self.assertEqual(w["scope"], ["mqtt", "zigbee2mqtt"])
        self.assertEqual(w["opened_by"], "telegram")
        self.assertIn(w["id"], self.e.maint_command("/maint list"))
        self.e.maint_command(f"/maint close {w['id']}")
        self.assertEqual(self.e.store.windows(), [])

    def test_strict(self):
        for bad in ("/maint site 2h rack day", "/maint zigbee2mqtt 13h too long",
                    "/maint nosuchnode 1h x y z", "/maint zigbee2mqtt 2h", "/maint zigbee2mqtt two hours reason",
                    "/maint zigbee2mqtt 2d reason here", "maint zigbee2mqtt 2h reason",
                    "/maint zigbee2mqtt 2h reason\nsecond line", "/maint close maint-zzzz",
                    "/maint " + "a" * 400, "/maint zigbee2mqtt;rm 2h reason"):
            with self.assertRaises((maint_text.BadCommand, ValueError), msg=bad):
                self.e.maint_command(bad)
        self.assertEqual(self.e.store.windows(), [])


class TestGitLab(Fwd):
    playbooks = CI_PLAYBOOKS

    def pipeline(self, status, pid=10, source="push", builds=(), project=4, ref="main", task=None):
        attrs = {"id": pid, "ref": ref, "status": status, "source": source, "variables": []}
        if task:
            attrs["variables"] = [{"key": "SCHEDULED_TASK", "value": task}]
        return {"object_kind": "pipeline", "object_attributes": attrs, "project": {"id": project},
                "builds": [dict(b) for b in builds]}

    def job(self, reason, job_id=55, name="router:deploy", project=4, ref="main"):
        return {"object_kind": "build", "build_id": job_id, "build_name": name, "build_status": "failed",
                "build_failure_reason": reason, "ref": ref, "project_id": project, "pipeline_id": 10}

    def test_parsing(self):
        [ev] = from_gitlab(self.pipeline("failed", builds=[{"name": "router:deploy", "status": "failed"}]), [4])
        self.assertEqual((ev.kind, ev.status, ev.fp), ("pipeline_failed", "firing", "gitlab:pipeline:4:push:-"))
        [ev] = from_gitlab(self.pipeline("success"), [4])
        self.assertEqual(ev.status, "resolved")
        [ev] = from_gitlab(self.pipeline("failed", source="schedule", task="renovate"), [4])
        self.assertEqual(ev.labels["scheduled_task"], "renovate")
        [ev] = from_gitlab(self.pipeline("failed", builds=[{"name": "router:deploy-service", "status": "failed"}]), [4])
        self.assertEqual(ev.labels["handled_by"], "automerge-rollback")
        self.assertEqual(from_gitlab(self.pipeline("failed", ref="feature"), [4]), [])
        self.assertEqual(from_gitlab(self.pipeline("failed", project=99), [4]), [])
        self.assertEqual(from_gitlab(self.pipeline("running"), [4]), [])
        [ev] = from_gitlab(self.job("runner_system_failure"), [4])
        self.assertEqual((ev.kind, ev.labels["failure_class"]), ("ci_job_failed", "system"))
        self.assertEqual(from_gitlab(self.job("script_failure"), [4]), [])
        self.assertEqual(from_gitlab(self.job("job_execution_timeout"), [4]), [])

    def ingest_gitlab(self, payload):
        for ev in from_gitlab(payload, [4, 14]):
            self.e.ingest(ev)

    def test_failed_main_pipeline_reaches_openclaw_and_green_resolves(self):
        self.ingest_gitlab(self.pipeline("failed", builds=[{"name": "router:deploy", "status": "failed",
                                                             "failure_reason": "script_failure"}]))
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertEqual(p["incident"]["kind"], "pipeline_failed")
        self.ingest_gitlab(self.pipeline("failed", pid=11))           # still red: same incident
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 1)
        # every failed pipeline id is in the log (the safety-net crons check it)
        self.assertEqual({d.get("pipeline_id") for d in self.w.decisions() if d["decision"] in ("opened", "deduped")},
                         {"10", "11"})
        self.ingest_gitlab(self.pipeline("success", pid=12))
        self.assertEqual(self.inc("gitlab:pipeline:4:push:-")["alert_state"], "resolved")
        self.ingest_gitlab(self.pipeline("failed", pid=13))           # red again: new incident
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 2)

    def test_rolled_back_deploy_service_failure_is_digest_only(self):
        self.ingest_gitlab(self.pipeline("failed", builds=[{"name": "router:deploy-service", "status": "failed"}]))
        self.tick()
        self.assertEqual(self.w.openclaw.delivered, [])

    def test_system_failure_is_retried_and_verified(self):
        self.ingest_gitlab(self.job("runner_system_failure"))
        self.w.executor.reply = {"ok": True, "detail": "retried", "new_job_id": 77}
        self.w.gitlab.deploying = True                  # CI actions are exempt from the deploy lock
        self.tick()
        self.assertEqual(self.w.executor.calls[0][:2], ("retry_ci_job", "55"))
        self.assertEqual(self.w.executor.playbooks, ["ci-system-retry"])
        self.w.gitlab.status["77"] = "success"
        self.tick()
        self.assertEqual(self.inc("gitlab:job:4:55")["status"], "resolved")
        self.assertEqual(self.w.openclaw.delivered, [])

    def test_failed_retry_goes_to_openclaw(self):
        self.ingest_gitlab(self.job("runner_system_failure"))
        self.w.executor.reply = {"ok": True, "detail": "retried", "new_job_id": 77}
        self.tick()
        self.w.gitlab.status["77"] = "failed"
        self.tick()
        self.assertEqual(self.inc("gitlab:job:4:55")["status"], "escalated")
        self.assertEqual(self.w.openclaw.delivered[-1]["kind"], "residue")

    def test_orphan_cancel_then_retry(self):
        self.w.gitlab.jobs = [{"id": 101, "name": "router:deploy", "started_at": self.w.clock.now() - 3 * 3600}]
        self.e.reconcile()
        self.tick()
        self.assertEqual(self.w.executor.calls[0][:2], ("cancel_ci_job", "101"))
        self.w.gitlab.jobs = []
        self.w.gitlab.status["101"] = "canceled"
        self.w.executor.reply = {"ok": True, "detail": "retried", "new_job_id": 202}
        self.tick()
        self.assertEqual(self.w.executor.calls[1][:2], ("retry_ci_job", "101"))
        self.assertEqual(self.w.executor.playbooks[1], "ci-orphan-retry")

    def test_ci_incidents_never_suppress_alert_dependents(self):
        self.ingest_gitlab(self.pipeline("failed"))
        self.tick()
        app = grafana("container-health-failure", "appx", container_name="app")
        self.e.ingest(app)
        self.assertNotEqual(self.inc("grafana:appx")["status"], "symptom")


class TestHTTP(Fwd):
    def setUp(self):
        super().setUp()
        self.state = State(self.e, "gtok", "ctok", ["127.0.0.1/32"], gitlab_token="wtok")
        self.httpd = serve(self.state, 0)
        self.port = self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body).encode() if body is not None else None
        c.request(method, path, body=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out

    def test_gitlab_token(self):
        payload = {"object_kind": "build", "build_id": 5, "build_name": "x", "build_status": "failed",
                   "build_failure_reason": "api_failure", "ref": "main", "project_id": 4}
        self.assertEqual(self.req("POST", "/gitlab", payload)[0], 401)
        self.assertEqual(self.req("POST", "/gitlab", payload, {"X-Gitlab-Token": "nope"})[0], 401)
        self.assertEqual(self.req("POST", "/gitlab", payload, {"X-Gitlab-Token": "wtok"}),
                         (200, {"accepted": 1}))

    def test_ack_outcome_maint_from_allowed_client(self):
        self.e.store.inbox_put(self.w.clock.now(), [event("mqtt").to_dict()])
        self.assertEqual(self.req("GET", "/ack?fp=mqtt&status=firing")[1], {"error": "fp must be a Grafana fingerprint"})
        self.e.store.inbox_put(self.w.clock.now(), [Event(source="grafana", kind="alert", fp="grafana:abc123",
                                                          status="firing").to_dict()])
        self.assertEqual(self.req("GET", "/ack?fp=abc123&status=firing"), (200, {"accepted": True}))
        st, out = self.req("POST", "/maint/text", {"text": "/maint zigbee2mqtt 1h testing the parser"})
        self.assertEqual(st, 200, out)
        st, out = self.req("POST", "/maint/text", {"text": "/maint site 1h no"})
        self.assertEqual(st, 400)
        self.assertIn("reply", out)
        st, out = self.req("POST", "/outcome", {"incident": None})
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
