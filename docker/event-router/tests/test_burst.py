"""Bursts (doc 4.7 step 4, #170): many alerts with no common ancestor become
one incident for the agent, not one forward each."""

from .test_phase3 import FWD_CONFIG, Fwd, grafana

BURST_CONFIG = dict(FWD_CONFIG, correlation={"burst_events": 3, "burst_within": "2m",
                                             "burst_settle": "60s", "burst_hold": "1h"})


class TestBurst(Fwd):
    cfg = BURST_CONFIG

    def probe(self, n, status="firing"):
        # http_response probes: no playbook, no node (like 2026-11-23)
        return grafana("http-endpoint-unreachable", f"p{n}", status=status, url=f"https://s{n}.example")

    def test_burst_is_one_forward_with_all_members(self):
        for n in range(3):                      # three alerts, settling, not forwarded yet
            self.e.ingest(self.probe(n))
            self.w.clock.t += 10
            self.tick(1)
        self.assertEqual(self.w.openclaw.delivered, [])
        self.e.ingest(self.probe(3))            # the fourth: more than burst_events
        self.tick()
        [p] = self.w.openclaw.delivered
        self.assertEqual(p["incident"]["kind"], "burst")
        self.assertEqual(sorted(m["labels"]["url"] for m in p["burst_members"]),
                         [f"https://s{n}.example" for n in range(4)])
        for n in range(4):
            self.assertEqual(self.inc(f"grafana:p{n}")["status"], "symptom")
        # a fifth alert of the same blip joins the burst: no forward of its own
        self.w.clock.t += 30
        self.e.ingest(self.probe(4))
        self.w.clock.t += 120
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 1)
        self.assertEqual(self.inc("grafana:p4")["status"], "symptom")
        burst = self.e.store.get(self.inc("grafana:p4")["root"])
        self.assertEqual(len(self.e.incident_view(burst["id"])["burst_members"]), 5)
        # members resolve with their alerts; then the burst resolves
        for n in range(5):
            self.e.ingest(self.probe(n, status="resolved"))
        self.tick()
        for n in range(5):
            self.assertEqual(self.inc(f"grafana:p{n}")["status"], "resolved")
        self.assertEqual(self.e.store.get(burst["id"])["alert_state"], "resolved")
        # a later, unrelated alert is handled alone again (after settling)
        self.w.clock.t += 600
        self.e.ingest(self.probe(9))
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 1)
        self.w.clock.t += 61
        self.tick()
        self.assertEqual(self.w.openclaw.delivered[-1]["incident"]["fp"], "grafana:p9")

    def test_member_still_firing_after_hold_is_handled_alone(self):
        for n in range(4):
            self.e.ingest(self.probe(n))
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 1)
        for n in range(3):
            self.e.ingest(self.probe(n, status="resolved"))
        self.w.clock.t += 3600
        self.tick()
        self.assertEqual(self.inc("grafana:p3")["status"], "escalated")
        self.assertEqual(self.w.openclaw.delivered[-1]["incident"]["fp"], "grafana:p3")
        self.assertEqual(len(self.w.openclaw.delivered), 2)

    def test_already_forwarded_alerts_are_listed_not_resent(self):
        self.e.ingest(self.probe(0))
        self.w.clock.t += 61
        self.tick()                               # settled and forwarded alone
        self.assertEqual(len(self.w.openclaw.delivered), 1)
        for n in range(1, 4):
            self.e.ingest(self.probe(n))
        self.tick()
        self.assertEqual(len(self.w.openclaw.delivered), 2)
        members = {m["labels"]["url"]: m["member"] for m in self.w.openclaw.delivered[-1]["burst_members"]}
        self.assertEqual(members, {"https://s0.example": False, "https://s1.example": True,
                                   "https://s2.example": True, "https://s3.example": True})

    def test_pages_are_never_held_or_grouped(self):
        for n in range(4):
            self.e.ingest(self.probe(n))
        self.e.ingest(grafana("failed-ssh-logins", "ssh"))
        self.tick()
        kinds = sorted(p["kind"] for p in self.w.openclaw.delivered)
        self.assertEqual(kinds, ["page", "residue"])
        self.assertEqual(self.inc("grafana:ssh")["status"], "paged")

    def test_playbook_alert_waits_for_settle_then_acts(self):
        self.e.ingest(grafana("zigbee-radio-silence", "zig"))
        self.tick()
        self.assertEqual(self.w.executor.calls, [])
        self.w.clock.t += 61
        self.tick()
        self.assertEqual(self.w.executor.calls[0][:2], ("restart_container", "zigbee2mqtt"))
