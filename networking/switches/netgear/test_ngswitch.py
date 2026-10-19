"""Unit tests for ngswitch.py (offline; no switch or Vault access)."""
import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ngswitch as ng  # noqa: E402

# Markers are assembled at runtime so this file contains no key-like literals.
_BEGIN = "-----" + "BEGIN"
_END = "-----" + "END"

SAMPLE = """config-file-header

v6.7.0.42 / TEST
CLI v1.0
@
vlan database
vlan 4-7
exit
logging origin-id ip
!
interface vlan 1
 no ip address dhcp
!
interface vlan 4
 name Management
 ip address dhcp
!
interface vlan 5
 name Guest
!
interface vlan 6
 name Iot
!
interface vlan 7
 name Airgap
!
interface GigabitEthernet1
 lacp port-priority 128
 switchport general allowed vlan add 6 untagged
 switchport general pvid 6
 lldp med enable network-policy location inventory
!
interface GigabitEthernet2
 lacp port-priority 128
 switchport general allowed vlan add 4 untagged
 switchport general pvid 4
!
interface FiveGigabitEthernet3
 lacp port-priority 128
 switchport general allowed vlan add 5 tagged
 switchport general allowed vlan add 7 untagged
 switchport general pvid 7
 power inline limit 15400
 power inline priority critical
 power inline never
!
interface TengigabitEthernet4
 lacp port-priority 128
 switchport general allowed vlan add 4-7 tagged
!
exit
scc mode disable
"""

DESIRED = {
    "name": "test-switch",
    "model": "TEST",
    "vault_path": "homelab/network/test-switch",
    "management": {"vlan": 4, "uplink": "te4", "protected_ports": ["gi2", "te4"]},
    "poe_defaults_offline": {"enabled": True, "priority": "low", "limit_mw": 30000},
    "vlans": {1: "", 4: "Management", 5: "Guest", 6: "Iot", 7: "Airgap"},
    "ports": {
        "gi1": {"pvid": 6, "untagged": [6], "tagged": [], "poe": {"enabled": True, "priority": "low", "limit_mw": 30000}},
        "gi2": {"pvid": 4, "untagged": [4], "tagged": [], "poe": {"enabled": True, "priority": "low", "limit_mw": 30000}},
        "hx3": {"pvid": 7, "untagged": [7], "tagged": [5], "poe": {"enabled": False, "priority": "high", "limit_mw": 15400}},
        "te4": {"pvid": 1, "untagged": [], "tagged": [4, 5, 6, 7]},
    },
}


def desired(mutate=None):
    d = copy.deepcopy(DESIRED)
    if mutate:
        mutate(d)
    return ng.validate_desired(d, "test")


def current_from_sample():
    d = desired()
    st, _ = ng.parse_running_config(SAMPLE, ng.offline_poe_defaults(d))
    return st


class TestRanges(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(ng.parse_ranges("4-7,9,11-12"), [4, 5, 6, 7, 9, 11, 12])
        self.assertEqual(ng.format_ranges([12, 4, 5, 6, 7, 9, 11]), "4-7,9,11-12")
        self.assertEqual(ng.format_ranges([]), "")

    def test_short_iface(self):
        self.assertEqual(ng.short_iface("TwoPointFiveGigabitEthernet5"), "tw5")
        self.assertEqual(ng.short_iface("FiveGigabitEthernet7"), "hx7")
        self.assertEqual(ng.short_iface("TengigabitEthernet10"), "te10")
        self.assertEqual(ng.short_iface("GigabitEthernet1"), "gi1")
        self.assertIsNone(ng.short_iface("Port-channel1"))


class TestSanitize(unittest.TestCase):
    def raw(self):
        return (
            "0x4E470x010x00test      6.7.0.42     0x00000000\n"
            "config-file-header\n\nv6.7.0.42 / TEST\nCLI v1.0\n@\n"
            "username admin password encrypted FAKEVALUE privilege 15 \n"
            "snmp-server engineID local " + "0" * 24 + " \n"
            "snmp-server community notpublic ro\n"
            "logging origin-id ip \n"
            "encrypted crypto certificate 1 import\n"
            f"{_BEGIN} RSA ENCRYPTED PRIVATE KEY-----\nQUJDREVGRw==\n{_END} RSA PRIVATE KEY-----\n\n"
            f"{_BEGIN} CERTIFICATE-----\nQUJDREVGRw==\n{_END} CERTIFICATE-----\n.\n"
            "scc mode disable \r\n"
        )

    def test_strips_sensitive_content(self):
        out = ng.sanitize(self.raw())
        self.assertTrue(out.startswith("config-file-header\n"))
        for bad in ("0x4E47", "username", "password", "engineID", "community", "BEGIN", "END", "QUJD", "crypto"):
            self.assertNotIn(bad, out)
        self.assertIn("logging origin-id ip\n", out)
        self.assertTrue(out.endswith("scc mode disable\n"))

    def test_trailing_whitespace_and_crlf_removed(self):
        out = ng.sanitize(self.raw())
        self.assertNotIn("\r", out)
        self.assertFalse(any(l != l.rstrip() for l in out.splitlines()))

    def test_assert_sanitized_raises_on_leftovers(self):
        for bad in (f"{_BEGIN} CERTIFICATE-----", "snmp-server engineID local 80001", "enable password x"):
            with self.assertRaises(ng.NgError):
                ng.assert_sanitized("a\n" + bad + "\n")

    def test_stray_pem_block_removed(self):
        out = ng.sanitize(f"config-file-header\nfoo\n{_BEGIN} PUBLIC KEY-----\nQUJD\n{_END} PUBLIC KEY-----\nbar\n")
        self.assertEqual(out, "config-file-header\nfoo\nbar\n")


class TestParse(unittest.TestCase):
    def test_parse_model(self):
        st = current_from_sample()
        self.assertEqual(st["vlans"], {1: "", 4: "Management", 5: "Guest", 6: "Iot", 7: "Airgap"})
        self.assertEqual(st["ports"]["gi1"], {"pvid": 6, "untagged": [6], "tagged": [],
                                              "poe": {"enabled": True, "priority": "low", "limit_mw": 30000}})
        self.assertEqual(st["ports"]["hx3"]["poe"], {"enabled": False, "priority": "high", "limit_mw": 15400})
        self.assertEqual(st["ports"]["hx3"]["tagged"], [5])
        self.assertEqual(st["ports"]["te4"], {"pvid": 1, "untagged": [], "tagged": [4, 5, 6, 7], "poe": None})

    def test_unmanaged_lines(self):
        _, un = ng.parse_running_config(SAMPLE)
        self.assertIn(("port:gi1", "lldp med enable network-policy location inventory"), un)
        self.assertIn(("vlan:4", "ip address dhcp"), un)
        self.assertIn(("", "scc mode disable"), un)
        self.assertFalse(any("switchport" in l or "power inline" in l for _, l in un))

    def test_unsupported_mode_rejected(self):
        with self.assertRaises(ng.NgError):
            ng.parse_running_config("interface GigabitEthernet1\n switchport mode access\n!\n")

    def test_remove_line(self):
        st, _ = ng.parse_running_config("interface GigabitEthernet1\n switchport general allowed vlan add 4-6 tagged\n"
                                        " switchport general allowed vlan remove 5\n!\n")
        self.assertEqual(st["ports"]["gi1"]["tagged"], [4, 6])


class TestValidate(unittest.TestCase):
    def test_valid(self):
        d = desired()
        self.assertEqual(d["management"]["protected_ports"], ["gi2", "te4"])

    def assertInvalid(self, mutate, text):
        with self.assertRaises(ng.NgError) as cm:
            desired(mutate)
        self.assertIn(text, str(cm.exception))

    def test_unknown_vlan(self):
        self.assertInvalid(lambda d: d["ports"]["gi1"]["untagged"].append(9), "VLAN 9 is not listed")

    def test_both_tagged_and_untagged(self):
        self.assertInvalid(lambda d: d["ports"]["gi1"]["tagged"].append(6), "both untagged and tagged")

    def test_uplink_must_be_protected(self):
        self.assertInvalid(lambda d: d["management"].update(uplink="gi1"), "uplink")

    def test_protected_port_must_carry_mgmt(self):
        self.assertInvalid(lambda d: d["management"]["protected_ports"].append("gi1"), "does not carry management VLAN")

    def test_bad_priority(self):
        self.assertInvalid(lambda d: d["ports"]["gi1"]["poe"].update(priority="urgent"), "poe.priority")

    def test_unknown_key(self):
        self.assertInvalid(lambda d: d["ports"]["gi1"].update(speed=100), "unknown key 'speed'")


class TestPlan(unittest.TestCase):
    def test_noop(self):
        changes, _ = ng.plan(current_from_sample(), desired()["state"])
        self.assertEqual(changes, [])

    def test_change_access_vlan_orders_add_before_remove(self):
        def m(d):
            d["ports"]["gi1"].update(pvid=5, untagged=[5])
        changes, _ = ng.plan(current_from_sample(), desired(m)["state"])
        self.assertEqual([c["op"] for c in changes], ["member_set", "pvid", "member_remove"])
        self.assertEqual(changes[0]["vlan"], 5)
        self.assertEqual(changes[2]["vlan"], 6)

    def test_tagging_change(self):
        def m(d):
            d["ports"]["hx3"].update(untagged=[7, 5], tagged=[])
        changes, _ = ng.plan(current_from_sample(), desired(m)["state"])
        self.assertEqual(changes, [{"op": "member_set", "port": "hx3", "vlan": 5, "mode": "untagged", "old": "tagged"}])

    def test_vlan_add_rename_remove(self):
        def m(d):
            d["vlans"][9] = "Lab"
            d["vlans"][5] = "Visitors"
            del d["vlans"][7]
            d["ports"]["hx3"].update(pvid=5, untagged=[], tagged=[5])
            d["ports"]["te4"]["tagged"] = [4, 5, 6]
        changes, _ = ng.plan(current_from_sample(), desired(m)["state"])
        ops = [c["op"] for c in changes]
        self.assertEqual(ops[0], "vlan_add")
        self.assertEqual(ops[1], "vlan_rename")
        self.assertEqual(ops[-1], "vlan_remove")

    def test_poe_change(self):
        def m(d):
            d["ports"]["gi1"]["poe"]["priority"] = "medium"
        changes, _ = ng.plan(current_from_sample(), desired(m)["state"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["op"], "poe")

    def test_poe_unknown_offline_is_a_note(self):
        st, _ = ng.parse_running_config(SAMPLE.replace(" power inline limit 15400\n power inline priority critical\n power inline never\n", ""))
        changes, notes = ng.plan(st, desired()["state"])
        self.assertTrue(all(c["op"] != "poe" for c in changes))
        self.assertTrue(notes)

    def test_missing_port_is_error(self):
        st = current_from_sample()
        st["ports"]["gi9"] = {"pvid": 1, "untagged": [], "tagged": [], "poe": None}
        with self.assertRaises(ng.NgError):
            ng.plan(st, desired()["state"])


class TestGuard(unittest.TestCase):
    def guard(self, mutate, mgmt=(4,)):
        d = desired(mutate)
        cur = current_from_sample()
        changes, _ = ng.plan(cur, d["state"])
        return changes, ng.check_guard(changes, d, cur, mgmt)

    def test_allows_normal_change(self):
        _, r = self.guard(lambda d: d["ports"]["gi1"].update(pvid=5, untagged=[5]))
        self.assertEqual(r, [])

    def test_allows_non_mgmt_vlan_on_protected_port(self):
        _, r = self.guard(lambda d: d["ports"]["te4"].update(tagged=[4, 5, 6]))
        self.assertEqual(r, [])

    def test_refuses_mgmt_vlan_removal_on_uplink(self):
        # Rejected by schema validation already ...
        with self.assertRaises(ng.NgError):
            desired(lambda d: d["ports"]["te4"].update(tagged=[5, 6, 7]))
        # ... and by the guard if such a change is ever planned.
        d = desired()
        changes = [{"op": "member_remove", "port": "te4", "vlan": 4, "old": "tagged"}]
        r = ng.check_guard(changes, d, current_from_sample(), (4,))
        self.assertTrue(any("management VLAN on protected port" in x for x in r), r)

    def test_refuses_mgmt_tagging_change_on_protected_port(self):
        _, r = self.guard(lambda d: d["ports"]["gi2"].update(untagged=[], tagged=[4]))
        self.assertTrue(r)

    def test_refuses_pvid_change_on_protected_port(self):
        _, r = self.guard(lambda d: d["ports"]["te4"].update(pvid=5))
        self.assertTrue(any("PVID of protected port" in x for x in r), r)

    def test_refuses_unprotected_mgmt_carrier(self):
        # gi1 is a management carrier on the switch but missing from protected_ports.
        d = desired()
        cur = current_from_sample()
        cur["ports"]["gi1"]["tagged"] = [4]
        changes, _ = ng.plan(cur, d["state"])
        r = ng.check_guard(changes, d, cur, (4,))
        self.assertTrue(any("not in protected_ports: gi1" in x for x in r), r)

    def test_refuses_mgmt_vlan_delete(self):
        d = desired()
        cur = current_from_sample()
        changes = [{"op": "vlan_remove", "vlan": 4}]
        self.assertTrue(ng.check_guard(changes, d, cur, (4,)))

    def test_refuses_when_mgmt_interface_elsewhere(self):
        _, r = self.guard(None, mgmt=(6,))
        self.assertTrue(any("has no IP interface" in x for x in r), r)

    def test_refuses_poe_off_on_protected_port(self):
        _, r = self.guard(lambda d: d["ports"]["gi2"]["poe"].update(enabled=False))
        self.assertTrue(any("power to protected port" in x for x in r), r)

    def test_allows_poe_off_on_other_port(self):
        _, r = self.guard(lambda d: d["ports"]["gi1"]["poe"].update(enabled=False))
        self.assertEqual(r, [])


class TestRenderSnapshot(unittest.TestCase):
    def roundtrip(self, mutate):
        d = desired(mutate)
        out = ng.render_snapshot(SAMPLE, d)
        st, _ = ng.parse_running_config(out, ng.offline_poe_defaults(d))
        changes, _ = ng.plan(st, d["state"])
        self.assertEqual(changes, [], out)
        self.assertEqual(ng.unmanaged_diff(SAMPLE, out), "")
        return out

    def test_identity(self):
        out = self.roundtrip(None)
        self.assertEqual(out, SAMPLE)

    def test_port_vlan_and_poe_changes(self):
        def m(d):
            d["ports"]["gi1"].update(pvid=5, untagged=[5], tagged=[7])
            d["ports"]["gi1"]["poe"].update(enabled=False, priority="medium")
            d["ports"]["hx3"]["poe"] = {"enabled": True, "priority": "low", "limit_mw": 30000}
        out = self.roundtrip(m)
        self.assertIn(" power inline priority high", out)
        self.assertNotIn(" power inline limit 15400", out)

    def test_vlan_add_and_remove(self):
        def m(d):
            d["vlans"][9] = "Lab"
            del d["vlans"][7]
            d["ports"]["hx3"].update(pvid=5, untagged=[], tagged=[5, 9])
            d["ports"]["te4"]["tagged"] = [4, 5, 6, 9]
        out = self.roundtrip(m)
        self.assertIn("vlan 4-6,9\n", out)
        self.assertIn("interface vlan 9\n name Lab\n", out)
        self.assertNotIn("Airgap", out)


class TestPayload(unittest.TestCase):
    def test_member_set(self):
        x = ng.payload({"op": "member_set", "port": "hx8", "vlan": 6, "mode": "untagged"}).decode()
        self.assertEqual(x, "<?xml version='1.0' encoding='utf-8'?><DeviceConfiguration>"
                            "<VLANMembershipList action=\"set\"><VLAN><VLANID>6</VLANID><MembershipList><VLANMember>"
                            "<interfaceName>hx8</interfaceName><interfaceType>1</interfaceType><membershipType>2</membershipType>"
                            "<taggingMode>1</taggingMode></VLANMember></MembershipList></VLAN></VLANMembershipList></DeviceConfiguration>")

    def test_member_remove(self):
        x = ng.payload({"op": "member_remove", "port": "hx8", "vlan": 5}).decode()
        self.assertIn('<VLANMembershipList action="delete">', x)
        self.assertNotIn("taggingMode", x)

    def test_pvid_keeps_other_port_settings(self):
        extra = {"hx8": {"frameType": "1", "ingressFilteringEnabled": "1", "portDefaultCoS": "0"}}
        x = ng.payload({"op": "pvid", "port": "hx8", "pvid": 7}, extra).decode()
        self.assertIn("<interfaceName>hx8</interfaceName><PVID>7</PVID><frameType>1</frameType>", x)
        with self.assertRaises(ng.NgError):
            ng.payload({"op": "pvid", "port": "gi1", "pvid": 7}, extra)

    def test_poe(self):
        x = ng.payload({"op": "poe", "port": "hx8", "new": {"enabled": False, "priority": "high", "limit_mw": 15400}}, None, "0").decode()
        self.assertIn("<adminEnable>2</adminEnable><powerPriority>1</powerPriority><powerLimit>15400</powerLimit>", x)
        x = ng.payload({"op": "poe", "port": "hx8", "new": {"enabled": True, "priority": "low", "limit_mw": 15400}}, None, "1").decode()
        self.assertNotIn("powerLimit", x)

    def test_vlan_name_escaped(self):
        x = ng.payload({"op": "vlan_add", "vlan": 9, "name": "A&B"}).decode()
        self.assertIn("<VLANName>A&amp;B</VLANName>", x)


class TestCommittedFiles(unittest.TestCase):
    """Every committed desired-state file is valid and consistent with its snapshot."""

    def test_committed(self):
        files = sorted(HERE.glob("*.yml"))
        for f in files:
            with self.subTest(file=f.name):
                d = ng.load_desired(f)
                snap = ng.snapshot_path(f)
                self.assertTrue(snap.exists(), f"{snap.name} missing")
                text = snap.read_text()
                ng.assert_sanitized(text)
                st, _ = ng.parse_running_config(text, ng.offline_poe_defaults(d))
                changes, _ = ng.plan(st, d["state"])
                self.assertEqual(changes, [], [ng.describe(c) for c in changes])
                cur = st
                self.assertEqual(ng.check_guard([], d, cur, None), [])


if __name__ == "__main__":
    unittest.main()
