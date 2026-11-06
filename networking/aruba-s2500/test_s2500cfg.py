#!/usr/bin/env python3
"""Offline tests for s2500cfg.py (no switch, no Vault).

testdata/show-running-config-raw.txt is a trimmed, synthetic `show running-config`
in the switch's format, with fake secrets; the committed snapshot
s2500.running-config.txt is checked too.
"""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import s2500cfg as c  # noqa: E402

RAW = (HERE / "testdata" / "show-running-config-raw.txt").read_text()


def desired(ports=None, profiles=None, protected=("0/0/0",)):
    return {"access_profiles": profiles if profiles is not None else {"vlan4": 4},
            "protected_ports": list(protected), "vlans": [],
            "ports": ports if ports is not None else {
                "0/0/0": {"profile": "vlan4", "poe": True, "description": None},
                "0/0/5": {"profile": "default", "poe": False, "description": "old printer"}}}


class TestSanitize(unittest.TestCase):
    def setUp(self):
        self.text = c.sanitize(RAW)

    def test_no_secrets(self):
        self.assertNotIn("fake", self.text)
        self.assertIn("enable secret <redacted>", self.text)
        self.assertIn("mgmt-user admin root <redacted>", self.text)
        self.assertIn("snmp-server community <redacted> view ALL", self.text)
        self.assertIn("snmp-server engine-id <redacted>", self.text)

    def test_revision_counter_normalized(self):
        self.assertEqual(c.sanitize("version 7.4\ncontroller config 17\n"), "version 7.4\ncontroller config <revision>\n")

    def test_strips_prompt_and_banner(self):
        self.assertTrue(self.text.startswith("version 7.4\n"))
        self.assertNotIn("Building", self.text)
        self.assertNotIn("(ArubaS2500", self.text)

    def test_leftover_hash_refused(self):
        with self.assertRaises(c.ctl.S2500Error):
            c.assert_sanitized("foo 0123456789abcdef0123456789abcdef\n")

    def test_committed_snapshot_is_sanitized(self):
        c.assert_sanitized((HERE / "s2500.running-config.txt").read_text())


class TestParse(unittest.TestCase):
    def setUp(self):
        self.cfg = c.parse_config(c.sanitize(RAW))

    def test_ports(self):
        self.assertEqual(self.cfg.port("0/0/0").profile, "vlan4")
        p5 = self.cfg.port("0/0/5")
        self.assertEqual((p5.profile, p5.poe_profile, p5.description), ("default", "nopoe", "old printer"))
        self.assertEqual(self.cfg.port("0/0/47").profile, "default")      # not in the config at all
        self.assertEqual(self.cfg.port("0/1/2").other, ['lacp-profile "test"'])

    def test_poe(self):
        self.assertEqual(self.cfg.group_poe_profile, "poe-factory-initial")
        self.assertTrue(self.cfg.poe_enabled("0/0/47"))     # via the default interface-group
        self.assertFalse(self.cfg.poe_enabled("0/0/5"))     # own profile without `enable`

    def test_profiles(self):
        self.assertEqual(self.cfg.access_vlan("vlan4"), 4)
        self.assertIsNone(self.cfg.access_vlan("trunk-main"))

    def test_changed_blocks(self):
        a = c.sanitize(RAW)
        b = a.replace('interface mgmt\n   ip address 10.100.0.1', 'interface mgmt\n   ip address 10.100.0.9')
        self.assertEqual(c.changed_blocks(a, b), ["interface mgmt"])


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.cfg = c.parse_config(c.sanitize(RAW))

    def test_in_sync(self):
        self.assertEqual(c.plan(desired(), self.cfg), [])

    def test_new_iot_port(self):
        d = desired(profiles={"vlan4": 4, "vlan6": 6})
        d["ports"]["0/0/47"] = {"profile": "vlan6", "poe": True, "description": "upstairs PoE switch (IoT)"}
        cmds = [x.commands() for x in c.plan(d, self.cfg)]
        self.assertEqual(cmds, [
            ['interface-profile switching-profile "vlan6"', "access-vlan 6", "exit"],
            ["interface gigabitethernet 0/0/47", 'switching-profile "vlan6"',
             'description "upstairs PoE switch (IoT)"', "exit"]])

    def test_unlisted_port_goes_back_to_default(self):
        d = desired()
        del d["ports"]["0/0/5"]
        (ch,) = c.plan(d, self.cfg)
        self.assertEqual(ch.lines, ['poe-profile "poe-factory-initial"', "no description"])

    def test_protected_port_refused(self):
        d = desired()
        d["ports"]["0/0/0"]["profile"] = "default"
        with self.assertRaises(c.Refused):
            c.plan(d, self.cfg)

    def test_poe_off_refused(self):
        d = desired()
        d["ports"]["0/0/0"]["poe"] = False
        d["protected_ports"] = []
        with self.assertRaises(c.Refused):
            c.plan(d, self.cfg)

    def test_existing_profile_not_changed(self):
        with self.assertRaises(c.Refused):
            c.plan(desired(profiles={"vlan4": 5}), self.cfg)

    def test_verify_scope(self):
        before = c.sanitize(RAW)
        after = before.replace('interface gigabitethernet "0/0/5"\n', 'interface gigabitethernet "0/0/5"\n'
                               '   switching-profile "vlan4"\n')
        ch = [c.Change("interface gigabitethernet 0/0/5", [], "")]
        self.assertEqual(c.verify_scope(before, after, ch), [])
        self.assertEqual(c.verify_scope(before, after, []), ['interface gigabitethernet "0/0/5"'])


class TestAllowList(unittest.TestCase):
    OK = [
        ['interface-profile switching-profile "vlan6"', "access-vlan 6", "exit"],
        ["interface gigabitethernet 0/0/47", 'switching-profile "vlan6"', 'poe-profile "poe-factory-initial"',
         'description "upstairs PoE switch (IoT) - not a trunk"', "exit"],
        ['interface gigabitethernet "0/0/3"', "no switching-profile", "no poe-profile", "no description", "exit"],
    ]
    BAD = [
        ["interface port-channel 0", 'switching-profile "vlan6"'],
        ["interface gigabitethernet 0/1/0", 'switching-profile "vlan6"'],
        ["interface gigabitethernet 0/1/2", "no lacp-profile"],
        ["interface gigabitethernet 0/0/48"],
        ["interface mgmt", "ip address 10.100.0.9 255.255.255.0"],
        ['interface vlan "6"', "ip address 10.6.0.2 255.255.0.0"],
        ['vlan "8"'],
        ["mgmt-user admin root x"],
        ["enable secret x"],
        ['aaa profile "default"'],
        ["snmp-server community x view ALL"],
        ["boot system partition 1"],
        ["copy tftp: 10.100.0.2 x system: partition 1"],
        ["write erase all"],
        ["write memory"],
        ["reload"],
        ['interface-profile switching-profile "trunk-main"', "trunk allowed vlan 1-8"],
        ['interface-profile switching-profile "vlan6"', "switchport-mode trunk"],
        ["interface gigabitethernet 0/0/47", "switchport-mode trunk"],
        ["interface gigabitethernet 0/0/47", "shutdown"],
        ["interface gigabitethernet 0/0/47", "lacp-profile \"test\""],
        ["interface gigabitethernet 0/0/47", 'description "x"; reload'],
        ["interface gigabitethernet 0/0/47", "exit", "reload"],
        ['interface-group gigabitethernet "default"', 'poe-profile "x"'],
        ["hostname x"],
    ]

    def test_allowed(self):
        for cmds in self.OK:
            c.check_allowed(cmds)

    def test_blocked(self):
        for cmds in self.BAD:
            with self.subTest(cmds=cmds), self.assertRaises(c.Refused):
                c.check_allowed(cmds)

    def test_protected(self):
        with self.assertRaises(c.Refused):
            c.check_allowed(["interface gigabitethernet 0/0/0", "no description"], protected=["0/0/0"])


class TestDesired(unittest.TestCase):
    def load(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as f:
            f.write(text)
        try:
            return c.load_desired(Path(f.name))
        finally:
            Path(f.name).unlink()

    def test_uplink_not_manageable(self):
        with self.assertRaises(c.ctl.S2500Error):
            self.load('access_profiles: {vlan6: 6}\nports:\n  "0/1/0": {profile: vlan6}\n')

    def test_bad_profile_name(self):
        with self.assertRaises(c.ctl.S2500Error):
            self.load('access_profiles: {vlan6: 7}\n')

    def test_committed_desired_state(self):
        d = c.load_desired()
        self.assertIn("0/0/0", d["protected_ports"])


class TestCommittedFiles(unittest.TestCase):
    def test_check_offline(self):
        self.assertEqual(c.check_offline(), [])


class TestDriftReport(unittest.TestCase):
    def test_clean_and_drift(self):
        snap = c.sanitize(RAW)
        drift, rep = c.drift_report(snap, snap, desired())
        self.assertFalse(drift)
        live = snap.replace('description "old printer"', 'description "new printer"')
        drift, rep = c.drift_report(live, snap, desired())
        self.assertTrue(drift)
        self.assertIn("+   description \"new printer\"", rep)
        self.assertIn("description 'new printer' -> 'old printer'", rep)


if __name__ == "__main__":
    unittest.main()
