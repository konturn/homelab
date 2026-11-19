"""Offline tests for ci/ha-maintenance (policy, scheduling, decisions)."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("HA_MAINT_DIR", tempfile.mkdtemp())
import ha_maint as M  # noqa: E402


def upd(**kw):
    base = {"entity_id": "update.hall_switch_firmware", "platform": "zwave_js", "name": "Hall Switch",
            "manufacturer": "Zooz", "model": "ZEN71", "domains": ["switch"], "cover_classes": [],
            "battery": False, "installed": "1.0", "latest": "1.1", "in_progress": False}
    base.update(kw)
    base["decision"], base["reason"] = M.classify(base)
    return base


def at(hour, day=18):
    return dt.datetime(2026, 11, day, hour, 30, tzinfo=M.TZ).timestamp()


def state():
    return {k: {} for k in ("seen", "approved", "skipped", "prompts", "failed", "nodes", "issues")}


class Classify(unittest.TestCase):
    def test_safe_classes_are_auto(self):
        self.assertEqual(upd()["decision"], "auto")
        self.assertEqual(upd(domains=["light"], model="ZEN77 dimmer")["decision"], "auto")
        self.assertEqual(upd(domains=["switch", "light"])["decision"], "auto")

    def test_hard_exclusions_are_denied(self):
        cases = [
            dict(platform="litterrobot", name="Basement Hallway Main", entity_id="update.basement_hallway_main_firmware"),
            dict(platform="whiskerless", name="Movie Room Litter Robot"),
            dict(platform="mqtt", manufacturer="Whisker", name="x"),
            dict(platform="smlight", name="SLZB-MR1"),
            dict(platform="mqtt", model="SLZB-06"),
            dict(platform="nut", name="Rack UPS"),
            dict(platform="zwave_js", name="Smart-UPS 1500"),
            dict(platform="subaru", name="Car"),
            dict(platform="mqtt", name="2026 Solterra Touring XT"),
            dict(platform="zwave_js", name="Front Door", domains=["lock"]),
            dict(platform="zwave_js", name="Front Deadbolt", domains=["switch"]),
            dict(platform="zwave_js", name="Opener", domains=["cover"], cover_classes=["garage"]),
            dict(platform="zwave_js", name="Garage Door Opener", domains=["switch"]),
            dict(platform="zwave_js", name="Panel", domains=["alarm_control_panel"]),
            dict(platform="hassio", entity_id="update.home_assistant_core_update", name="Home Assistant Core"),
            dict(platform="zwave_js", name="Z-Wave JS", model="ZST39 LR", domains=[]),
            dict(platform="plex", name="Plex Media Server"),
            dict(platform="frigate", name="Frigate"),
            dict(platform="marketplace", name="Keymaster", entity_id="update.keymaster_update"),
        ]
        for c in cases:
            with self.subTest(c=c):
                self.assertEqual(upd(**c)["decision"], "deny", upd(**c)["reason"])

    def test_everything_else_needs_a_tap(self):
        cases = [
            dict(platform="mqtt", name="Pantry", model='Hue slim downlight 6"', domains=["light"]),
            dict(platform="marketplace", name="Samsung TV Smart", domains=[]),
            dict(battery=True, domains=["switch"], name="Battery remote"),
            dict(domains=["climate"], name="Thermostat"),
            dict(domains=["cover"], cover_classes=["shade"], name="Shade"),
            dict(domains=[], name="Motion sensor"),
            dict(name="Movie Room Fridge Outlet", domains=["light"]),     # must not lose power
            dict(name="Server Rack Plug"),
        ]
        for c in cases:
            with self.subTest(c=c):
                self.assertEqual(upd(**c)["decision"], "tap", upd(**c)["reason"])


class Install(unittest.TestCase):
    def test_auto_needs_window_soak_and_one_per_day(self):
        u, st = upd(), state()
        now = at(12)
        st["seen"][u["entity_id"]] = {"version": "1.1", "since": now - 6 * 86400}
        self.assertIsNone(M.pick_install([u], st, now))                      # soak not over
        st["seen"][u["entity_id"]]["since"] = now - 8 * 86400
        self.assertEqual(M.pick_install([u], st, now)[1], "auto")
        self.assertIsNone(M.pick_install([u], st, at(8)))                    # before the window
        self.assertIsNone(M.pick_install([u], st, at(19)))                   # after it
        st["last_auto_day"] = M.local_day(now)
        self.assertIsNone(M.pick_install([u], st, now))                      # one per day
        st["last_auto_day"] = "2000-01-01"
        st["seen"][u["entity_id"]]["version"] = "1.0"                         # a newer release restarts the soak
        self.assertIsNone(M.pick_install([u], st, now))

    def test_failure_backs_off_and_pauses(self):
        u, st = upd(), state()
        now = at(12)
        st["seen"][u["entity_id"]] = {"version": "1.1", "since": now - 30 * 86400}
        st["failed"][u["entity_id"]] = now - 86400
        self.assertIsNone(M.pick_install([u], st, now))
        st["failed"] = {}
        st["auto_paused_until"] = now + 10
        self.assertIsNone(M.pick_install([u], st, now))

    def test_taps_install_only_when_approved_for_that_version(self):
        u, st = upd(platform="mqtt", name="Pantry", domains=["light"]), state()
        now = at(12)
        self.assertEqual(u["decision"], "tap")
        self.assertIsNone(M.pick_install([u], st, now))
        st["approved"][u["entity_id"]] = {"version": "1.0", "at": now}
        self.assertIsNone(M.pick_install([u], st, now))                      # approved an older version
        st["approved"][u["entity_id"]] = {"version": "1.1", "at": now}
        self.assertEqual(M.pick_install([u], st, now), (u, "approved"))
        self.assertIsNone(M.pick_install([u], st, at(20)))                   # still only in the window

    def test_denied_never_installs_even_if_approved(self):
        u, st = upd(platform="litterrobot", name="Litter-Robot"), state()
        st["approved"][u["entity_id"]] = {"version": u["latest"], "at": 0}
        st["seen"][u["entity_id"]] = {"version": u["latest"], "since": 0}
        self.assertIsNone(M.pick_install([u], st, at(12)))


class Nodes(unittest.TestCase):
    def test_ping_then_reinterview_with_limits(self):
        n, st = {"device_id": "d1", "name": "Fridge Outlet", "status": "unresponsive"}, state()
        t = at(10)
        self.assertEqual(M.pick_node_action([n], st, t)[1], "ping")
        st["nodes"]["d1"]["history"].append({"kind": "ping", "ts": t})
        self.assertIsNone(M.pick_node_action([n], st, t + 600))             # too soon after the ping
        self.assertEqual(M.pick_node_action([n], st, t + 7200)[1], "reinterview")
        st["nodes"]["d1"]["history"].append({"kind": "reinterview", "ts": t + 7200})
        self.assertIsNone(M.pick_node_action([n], st, t + 4 * 3600))         # cooldown
        st["nodes"]["d1"]["history"].append({"kind": "ping", "ts": t + 86400 + 7200})
        st["nodes"]["d1"]["history"].append({"kind": "reinterview", "ts": t + 2 * 86400 + 7200})
        st["nodes"]["d1"]["history"].append({"kind": "ping", "ts": t + 3 * 86400 + 7200})
        self.assertIsNone(M.pick_node_action([n], st, t + 3 * 86400 + 4 * 3600))   # 2 re-interviews this week

    def test_never_sends_a_switch_command(self):
        with open(os.path.join(HERE, "ha_maint.py")) as f:
            src = f.read()
        for forbidden in ('"turn_off"', '"turn_on"', '"toggle"', "'turn_off'", "'turn_on'", "'toggle'"):
            self.assertNotIn(forbidden, src)


class Prompts(unittest.TestCase):
    def test_identical_updates_share_a_prompt_and_are_not_resent(self):
        st, now = state(), at(12)
        bulbs = [upd(entity_id=f"update.bulb{i}", platform="mqtt", model="Hue slim", domains=["light"],
                     name=f"Bulb {i}") for i in range(8)]
        tv = upd(entity_id="update.tv", platform="marketplace", model=None, name="TV", domains=[])
        groups = M.tap_groups(bulbs + [tv], st, now)
        self.assertEqual(sorted(len(g) for g in groups), [1, 8])
        st["prompts"]["haup-aaaaaaaaaaaa"] = {"entity_ids": [b["entity_id"] for b in bulbs], "version": "1.1", "at": now}
        self.assertEqual([g[0]["entity_id"] for g in M.tap_groups(bulbs + [tv], st, now + 3600)], ["update.tv"])
        self.assertEqual(len(M.tap_groups(bulbs + [tv], st, now + 8 * 86400)), 2)   # weekly re-send
        st["skipped"]["update.tv"] = "1.1"
        self.assertEqual(len(M.tap_groups([tv], st, now)), 0)


class Decisions(unittest.TestCase):
    def test_presses_become_approvals_and_skips(self):
        st, now = state(), at(12)
        st["prompts"] = {"haup-aaaaaaaaaaaa": {"entity_ids": ["update.a", "update.a2"], "version": "2", "at": now},
                         "haup-bbbbbbbbbbbb": {"entity_ids": ["update.b"], "version": "3", "at": now}}
        with open(M.DECISIONS, "w") as f:
            for d in ({"id": "haup-aaaaaaaaaaaa", "action": "install"}, {"id": "haup-bbbbbbbbbbbb", "action": "skip"},
                      {"id": "apr-000000000000", "action": "approve"}, {"id": "haup-cccccccccccc", "action": "install"}):
                f.write(json.dumps(d) + "\n")
        M.apply_decisions(st, M.read_decisions(), now)
        self.assertEqual(st["approved"], {"update.a": {"version": "2", "at": now},
                                          "update.a2": {"version": "2", "at": now}})
        self.assertEqual(st["skipped"], {"update.b": "3"})
        self.assertEqual(M.read_decisions(), [])                             # consumed


if __name__ == "__main__":
    unittest.main()
