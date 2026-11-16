"""Deferred retry and re-escalation (doc 16), replaying the 2026-11-14
plex incident: a deploy recreated plex at 02:57 in the middle of its nightly
maintenance, the new process hung, restart-once was refused at 03:09
(recent_deploy: "recreated 12m ago"), the incident went to OpenClaw, and
Grafana's repeats at 07:09, 11:09, 15:09 and 19:09 were deduped until
someone restarted plex by hand at 21:10.

With the fix the router defers the restart to the guard's expiry (04:58)
and runs it then; if it still cannot act, the incident goes back to OpenClaw
marked still failing after 2h and pages Noah after 6h (plex is user-facing).
"""

import unittest

from router.events import Event

from .helpers import CONFIG, World

DAY = 1_800_000_000 + 11 * 3600          # 14:00 in New York
AT_0309 = DAY - 11 * 3600 + 9 * 60       # 03:09
MIN = 60
HOUR = 3600

CFG = dict(CONFIG, openclaw={"forward": True},
           quiet_hours={"start": "23:00", "end": "08:00", "tz": "America/New_York"},
           stale={"reforward_after": "2h", "page_after": "6h", "firing_within": "270m",
                  "user_facing": ["plex", "snapclient*"]})

REFUSED = {"ok": False, "refused": "recent_deploy",
           "detail": "plex was (re)created 12m ago by a deploy: a revert, not a restart",
           "retry_after": 2 * HOUR - 12 * MIN + 1}


def health(container, status="firing"):
    return Event(source="grafana", kind="alert", fp=f"grafana:health-{container}", status=status,
                 rule_uid="container-health-failure", alertname="Container Health Check Failed",
                 labels={"alertname": "Container Health Check Failed", "container_name": container},
                 summary=f"Container {container} health check failed")


class Incident(unittest.TestCase):
    cfg = CFG

    def setUp(self):
        self.w = World(cfg=self.cfg)
        self.e = self.w.engine
        self.w.clock.t = AT_0309

    def tearDown(self):
        self.w.close()

    def at(self, hhmm, day=0):
        h, m = (int(x) for x in hhmm.split(":"))
        self.w.clock.t = AT_0309 - 9 * MIN - 3 * HOUR + h * HOUR + m * MIN + day * 24 * HOUR

    def tick(self, n=3):
        for _ in range(n):
            self.e.tick(drain=False)

    def inc(self, c="plex"):
        return self.e.store.latest_for_fp(f"grafana:health-{c}")

    def decisions(self, name=None):
        return [d for d in self.w.decisions() if name is None or d["decision"] == name]

    def deliver_repeats(self, until, c="plex", start="07:09"):
        """Grafana's repeat_interval (4h) from `start` up to `until` (hours)."""
        h = int(start.split(":")[0])
        while h <= until:
            self.at(f"{h:02d}:09")
            self.e.ingest(health(c))
            self.tick()
            h += 4


class TestPlexIncident(Incident):
    def test_restart_deferred_to_the_guard_expiry_then_run(self):
        self.w.executor.reply = dict(REFUSED)
        self.e.ingest(health("plex"))
        self.tick()
        inc = self.inc()
        self.assertEqual(inc["status"], "deferred")              # not escalated for good
        [d] = self.decisions("deferred")
        self.assertEqual(d["refused"], "recent_deploy")
        self.assertEqual(d["retry_in_s"], 2 * HOUR - 12 * MIN + 1 + 60)   # 04:58
        # OpenClaw gets the residue now, and sees the due time.
        [p] = self.w.openclaw.delivered
        self.assertEqual(p["decisions"][-1]["decision"], "deferred")
        self.assertIn("retries", p["reason"])
        self.assertEqual(self.e.store.runs_since("restart-once", "plex", "active", 0), [])

        self.at("04:50")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 1)          # not before the guard expires
        self.at("04:59")
        self.w.executor.reply = {"ok": True, "detail": "restarted plex"}
        self.tick()
        self.assertEqual(len(self.decisions("retry")), 1)
        self.assertEqual([c[:2] for c in self.w.executor.calls], [("restart_container", "plex")] * 2)
        self.assertEqual(self.inc()["id"], inc["id"])
        self.assertEqual(self.inc()["status"], "verifying")
        self.at("05:03")
        self.e.ingest(health("plex", "resolved"))
        self.tick()
        self.assertEqual(self.inc()["status"], "resolved")
        self.assertEqual(len(self.decisions("verified")), 1)
        # The 07:09 repeat never comes; nothing else happens all day.
        self.at("21:10")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 2)
        self.assertEqual(self.decisions("stale_reforwarded") + self.decisions("stale_paged"), [])

    def test_guard_keeps_refusing_reforward_at_2h_page_at_6h(self):
        """The worst case: every look is refused (more deploys, or an executor
        that cannot read the creation time). The incident is not deduped into
        silence: OpenClaw hears again at 2h, Noah is paged at 6h."""
        self.w.executor.reply = dict(REFUSED, retry_after=10 * MIN)
        self.e.ingest(health("plex"))                            # 03:09: deferral 1, due 03:20
        self.tick()
        self.at("03:21")
        self.tick()                                              # deferral 2, due 03:32
        self.at("03:33")
        self.tick()                                              # third refusal: escalated
        self.assertEqual(len(self.w.executor.calls), 3)
        self.assertEqual(self.inc()["status"], "escalated")
        self.assertEqual(len(self.decisions("deferred")), 2)
        delivered = len(self.w.openclaw.delivered)

        self.at("05:08")
        self.tick()
        self.assertEqual(self.decisions("stale_reforwarded"), [])
        self.at("05:10")                                         # 2h after it opened
        self.tick()
        [d] = self.decisions("stale_reforwarded")
        self.assertEqual(d["stale_level"], 1)
        p = self.w.openclaw.delivered[-1]
        self.assertEqual(p["kind"], "residue")
        self.assertEqual(p["stale"]["level"], 1)
        self.assertTrue(p["quiet_hours"])                        # OpenClaw works on it, quietly
        self.assertIn("still failing after 2h", p["reason"])
        self.assertEqual(len(self.w.openclaw.delivered), delivered + 1)

        self.deliver_repeats(until=7)                            # 07:09: deduped, still firing
        self.assertEqual(self.decisions("deduped")[-1]["deliveries"], 2)
        self.at("09:10")                                         # 6h after it opened
        self.tick()
        [d] = self.decisions("stale_paged")
        self.assertTrue(d["user_facing"])
        p = self.w.openclaw.delivered[-1]
        self.assertEqual(p["kind"], "page")
        self.assertEqual(p["page"]["class"], "still_failing")
        self.assertFalse(p["page"]["urgent"])
        self.assertFalse(p["page"]["email"])
        # Once per level: the 11:09, 15:09, 19:09 repeats add nothing.
        self.deliver_repeats(until=19, start="11:09")
        self.assertEqual(len(self.decisions("stale_paged")), 1)
        self.assertEqual(len(self.decisions("stale_reforwarded")), 1)
        self.assertEqual(len(self.w.executor.calls), 3)          # restart-once: no extra attempts

    def test_page_goes_out_through_the_jit_bot_while_openclaw_is_down(self):
        self.w.executor.reply = {"ok": False, "refused": "not_allowed", "detail": "x"}
        self.e.ingest(health("plex"))
        self.tick()
        self.deliver_repeats(until=7)
        self.w.openclaw.result = "down"
        self.at("09:10")
        self.tick()
        self.at("09:14")
        self.tick()
        [m] = self.w.outbox()
        self.assertIn("still_failing", m["text"])

    def test_stale_page_in_quiet_hours_waits_for_morning(self):
        self.at("19:30", day=-1)                                 # opened 19:30, page due 01:30
        self.w.executor.reply = {"ok": False, "refused": "not_allowed", "detail": "x"}
        self.e.ingest(health("plex"))
        self.tick()
        self.at("23:30", day=-1)
        self.e.ingest(health("plex"))                            # Grafana's 4h repeat
        self.tick()
        self.at("01:31")
        self.tick()
        self.assertEqual(len(self.decisions("stale_paged")), 1)
        self.assertNotEqual(self.w.openclaw.delivered[-1]["kind"], "page")   # held
        self.at("08:01")
        self.tick()
        p = self.w.openclaw.delivered[-1]
        self.assertEqual(p["kind"], "page")
        self.assertTrue(p["page"]["held_for_quiet_hours"])

    def test_not_user_facing_is_reforwarded_twice_never_paged(self):
        self.w.executor.reply = dict(REFUSED, retry_after=10 * MIN)
        self.e.ingest(health("sonarr"))
        self.tick()
        for t in ("03:21", "03:33", "05:10"):
            self.at(t)
            self.tick()
        self.deliver_repeats(until=7, c="sonarr")
        self.at("09:10")
        self.tick()
        self.assertEqual([d["stale_level"] for d in self.decisions("stale_reforwarded")], [1, 2])
        self.assertEqual(self.decisions("stale_paged"), [])
        self.assertTrue(all(p["kind"] == "residue" for p in self.w.openclaw.delivered))

    def test_lost_resolve_never_pages(self):
        """No delivery for longer than Grafana's repeat interval: the alert
        is not known to be firing (a resolve may have been lost)."""
        self.w.executor.reply = {"ok": False, "refused": "not_allowed", "detail": "x"}
        self.e.ingest(health("plex"))
        self.tick()
        self.at("05:10")
        self.tick()
        self.assertEqual(len(self.decisions("stale_reforwarded")), 1)    # 2h: last delivery 2h ago
        self.at("09:10")
        self.tick()
        self.assertEqual(self.decisions("stale_paged"), [])             # 6h without a delivery

    def test_resolved_incident_is_not_reescalated(self):
        self.w.executor.reply = {"ok": False, "refused": "not_allowed", "detail": "x"}
        self.e.ingest(health("plex"))
        self.tick()
        self.at("04:00")
        self.e.ingest(health("plex", "resolved"))
        self.at("09:10")
        self.tick()
        self.assertEqual(self.decisions("stale_reforwarded") + self.decisions("stale_paged"), [])

    def test_no_reescalation_inside_a_maintenance_window(self):
        self.w.executor.reply = {"ok": False, "refused": "not_allowed", "detail": "x"}
        self.e.ingest(health("plex"))
        self.tick()
        self.at("05:00")
        wid = self.e.open_window(["site"], 2 * HOUR, "rack day", "test")
        self.at("05:10")
        self.tick()
        self.assertEqual(self.decisions("stale_reforwarded"), [])
        self.e.close_window(wid, "test")
        self.tick()
        self.assertEqual(len(self.decisions("stale_reforwarded")), 1)


class TestDeferredRetryKeepsTheGuards(Incident):
    """The retry is a re-evaluation, not a pre-approved action: restart-once,
    the kill switch, maintenance windows and breakers all still apply."""

    def defer(self):
        self.w.executor.reply = dict(REFUSED)
        self.e.ingest(health("plex"))
        self.tick()
        self.assertEqual(self.inc()["status"], "deferred")
        self.w.executor.reply = {"ok": True}
        self.at("04:59")

    def test_kill_switch(self):
        self.defer()
        self.w.set_override("notify-only")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(self.inc()["status"], "escalated")
        self.assertIn("kill switch", self.decisions("escalated")[-1]["reason"])

    def test_maintenance_window(self):
        self.defer()
        wid = self.e.open_window(["site"], HOUR, "plex upgrade", "test")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(self.inc()["status"], "maint")
        self.e.close_window(wid, "test")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 2)           # recovery check: normal handling

    def test_breaker(self):
        self.defer()
        self.e.store.open_breaker("restart-once", "active", self.w.clock.now())
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 1)
        self.assertEqual(self.inc()["status"], "paged")

    def test_rate_limit_counts_the_retry_not_the_refusal(self):
        self.defer()
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 2)
        self.assertEqual(len(self.e.store.runs_since("restart-once", "plex", "active", 0)), 1)

    def test_restart_once_verification_failure_pages_no_second_retry(self):
        self.defer()
        self.tick()
        self.at("05:20")                                         # verify_within 10m, alert still firing
        self.tick()
        self.assertEqual(self.inc()["status"], "paged")
        self.at("09:00")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 2)

    def test_unknown_reply_is_never_deferred(self):
        self.w.executor.reply = {"ok": False, "unknown": True}
        self.e.ingest(health("plex"))
        self.tick()
        self.assertEqual(self.decisions("deferred"), [])
        self.at("09:00")
        self.tick()
        self.assertEqual(len(self.w.executor.calls), 1)

    def test_other_refusals_are_not_deferred(self):
        for c, why in (("plex", "not_allowed"), ("sonarr", "rate_limit"), ("radarr", "duplicate")):
            self.w.executor.reply = {"ok": False, "refused": why, "retry_after": 60}
            self.e.ingest(health(c))
            self.tick()
        self.assertEqual(len(self.w.executor.calls), 3)
        self.assertEqual(self.decisions("deferred"), [])


if __name__ == "__main__":
    unittest.main()
