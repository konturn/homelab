"""Unit tests for ngswitch.py (offline; no switch or Vault access)."""
import argparse
import copy
import hashlib
import io
import json
import re
import tempfile
import time
import zipfile
from contextlib import redirect_stdout
from unittest import mock
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


def fake_image(version="6.7.0.52", size=4096):
    """A blob with the NETGEAR image header layout ("NG10" at 0x0c, version at 0x14)."""
    hdr = bytearray(size)
    hdr[0x0c:0x10] = b"NG10"
    v = version.encode()
    hdr[0x14:0x14 + len(v)] = v
    return bytes(hdr)


FW = {"version": "6.7.0.52", "url": "https://example.invalid/fw/X_V6.7.0.52.zip", "sha256": "a" * 64,
      "image": "X_V6.7.0.52.bin", "image_sha256": "b" * 64}


class TestFirmwareSchema(unittest.TestCase):
    def test_valid(self):
        d = desired(lambda d: d.update(firmware=dict(FW)))
        self.assertEqual(d["firmware"]["version"], "6.7.0.52")

    def test_absent_is_none(self):
        self.assertIsNone(desired()["firmware"])

    def assertInvalid(self, change, text):
        def mutate(d):
            fw = dict(FW)
            fw.update(change)
            d["firmware"] = fw
        with self.assertRaises(ng.NgError) as cm:
            desired(mutate)
        self.assertIn(text, str(cm.exception))

    def test_unquoted_version(self):
        self.assertInvalid({"version": 6.7}, "firmware.version")

    def test_bad_sha(self):
        self.assertInvalid({"sha256": "XYZ"}, "firmware.sha256")

    def test_http_url_refused(self):
        self.assertInvalid({"url": "http://example.invalid/x.zip"}, "firmware.url")

    def test_image_must_match_version(self):
        self.assertInvalid({"image": "X_V6.7.0.42.bin"}, "does not contain firmware.version")

    def test_unknown_key(self):
        self.assertInvalid({"slot": 2}, "firmware: unknown key 'slot'")


class TestFirmwareArtifact(unittest.TestCase):
    def setUp(self):
        import hashlib
        import tempfile
        import zipfile
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src"
        self.src.mkdir()
        self.blob = fake_image()
        zpath = self.src / "X_V6.7.0.52.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("X_V6.7.0.52.bin", self.blob)
            z.writestr("notes.htm", "<html></html>")
        self.fw = {"version": "6.7.0.52", "url": zpath.as_uri(),
                   "sha256": hashlib.sha256(zpath.read_bytes()).hexdigest(),
                   "image": "X_V6.7.0.52.bin", "image_sha256": hashlib.sha256(self.blob).hexdigest()}
        self.cache = self.tmp / "cache"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp)

    def test_image_version(self):
        self.assertEqual(ng.image_version(self.blob), "6.7.0.52")
        self.assertIsNone(ng.image_version(b"\x00" * 64))

    def test_download_verify_extract_and_cache(self):
        p = ng.fetch_firmware(self.fw, self.cache, log=lambda m: None)
        self.assertEqual(p.read_bytes(), self.blob)
        msgs = []
        ng.fetch_firmware(self.fw, self.cache, log=msgs.append)
        self.assertTrue(any("cached" in m for m in msgs))

    def test_zip_hash_mismatch(self):
        self.fw["sha256"] = "0" * 64
        with self.assertRaises(ng.NgError) as cm:
            ng.fetch_firmware(self.fw, self.cache, log=lambda m: None)
        self.assertIn("zip sha256 mismatch", str(cm.exception))
        self.assertEqual(list(self.cache.glob("*.part")), [])

    def test_image_hash_mismatch(self):
        self.fw["image_sha256"] = "0" * 64
        with self.assertRaises(ng.NgError) as cm:
            ng.fetch_firmware(self.fw, self.cache, log=lambda m: None)
        self.assertIn("image sha256 mismatch", str(cm.exception))
        self.assertFalse((self.cache / self.fw["image"]).exists())

    def test_corrupt_cached_image_is_replaced(self):
        self.cache.mkdir()
        (self.cache / self.fw["image"]).write_bytes(b"junk")
        p = ng.fetch_firmware(self.fw, self.cache, log=lambda m: None)
        self.assertEqual(p.read_bytes(), self.blob)

    def test_header_version_mismatch(self):
        import hashlib
        import zipfile
        blob = fake_image("6.7.0.42")
        zpath = self.src / "X_V6.7.0.52.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            z.writestr("X_V6.7.0.52.bin", blob)
        self.fw["sha256"] = hashlib.sha256(zpath.read_bytes()).hexdigest()
        self.fw["image_sha256"] = hashlib.sha256(blob).hexdigest()
        with self.assertRaises(ng.NgError) as cm:
            ng.fetch_firmware(self.fw, self.cache, log=lambda m: None)
        self.assertIn("image header names version 6.7.0.42", str(cm.exception))


IMAGE_XML = """<ImageUnitList><Entry><unitID>1</unitID><image1Version>6.7.0.42</image1Version>
<image2Version>{v2}</image2Version><currentImage>1</currentImage><nextBootImage>{nb}</nextBootImage>
<image1Description></image1Description><image2Description></image2Description>{names}</Entry></ImageUnitList>"""
NAMES = "<image1Name>flash://system/images/image1.bin</image1Name><image2Name>flash://system/images/{n2}</image2Name>"


class TestFirmwareStatus(unittest.TestCase):
    def img(self, v2="6.7.0.42", nb="1", n2="image2.bin", names=True):
        import xml.etree.ElementTree as ET
        xml = IMAGE_XML.format(v2=v2, nb=nb, names=NAMES.format(n2=n2) if names else "")
        return ng.parse_image_status(ET.fromstring(xml))

    def test_parse(self):
        self.assertEqual(self.img(), {"image1": "6.7.0.42", "image2": "6.7.0.42", "current": 1, "next": 1,
                                      "image1_name": "flash://system/images/image1.bin",
                                      "image2_name": "flash://system/images/image2.bin"})
        self.assertEqual(ng.running_version(self.img()), "6.7.0.42")
        self.assertIsNone(self.img(names=False)["image2_name"])

    def test_slot_problems(self):
        self.assertEqual(ng.slot_problems(self.img(v2="6.7.0.52"), 2, "6.7.0.52"), [])
        # a truncated file name after a failed upload, even though the version reads fine
        bad = self.img(v2="6.7.0.52", nb="2", n2="rs")
        self.assertIn("incomplete write", " ".join(ng.slot_problems(bad, 2, "6.7.0.52")))
        self.assertEqual(ng.slot_problems(bad, 1), [])
        # names the switch really uses: the vendor file name, or image1.bin in slot 2
        for n2 in ("MS510TX_MS510TXPP_V6.7.0.42.bin", "image1.bin", "MS510TX_TXPP_V6.7.0.52.bin"):
            self.assertEqual(ng.slot_problems(self.img(v2="6.7.0.52", n2=n2), 2, "6.7.0.52"), [], n2)
        for n2 in ("image2", "image2.bi", "", "sub/image2.bin"):
            self.assertTrue(ng.slot_problems(self.img(v2="6.7.0.52", n2=n2), 2, "6.7.0.52"), n2)
        self.assertIn("expected 6.7.0.52", " ".join(ng.slot_problems(self.img(), 2, "6.7.0.52")))
        self.assertIn("cannot be verified", " ".join(ng.slot_problems(self.img(names=False), 2)))
        self.assertIn("no version", " ".join(ng.slot_problems(self.img(v2=""), 2)))
        self.assertIn("image2 6.7.0.52 [INVALID]", ng.describe_images(bad))
        self.assertNotIn("INVALID", ng.describe_images(self.img()))

    def test_status_line_flags_invalid_next_boot(self):
        bad = self.img(v2="6.7.0.52", nb="2", n2="rs")
        line, flagged = ng.firmware_status_line("sw", dict(FW, version="6.7.0.42"), bad)
        self.assertTrue(flagged)
        self.assertIn("NEXT BOOT IMAGE IS INVALID", line)

    def test_tftp_download_payload(self):
        x = ng.tftp_download_payload("192.0.2.1", "MS510TX_TXPP_V6.7.0.52.bin").decode()
        self.assertIn('<TFTPDownload action="set"><destFileType>8</destFileType>'
                      '<srcIPAddr>192.0.2.1</srcIPAddr><srcFileName>MS510TX_TXPP_V6.7.0.52.bin</srcFileName>'
                      '</TFTPDownload>', x)
        for server, fname in (("host.example", "a.bin"), ("192.0.2.1", "../a.bin"), ("192.0.2.1", "a" * 65)):
            with self.assertRaises(ng.NgError):
                ng.tftp_download_payload(server, fname)

    def test_upload_problems(self):
        self.assertEqual(ng.upload_problems({"bytesTransfered": "100", "copyStatusType": "5"}, 100), [])
        self.assertTrue(ng.upload_problems({"bytesTransfered": "99"}, 100))
        self.assertTrue(ng.upload_problems({}, 100))
        self.assertTrue(ng.upload_problems(None, 100))

    def test_status_line(self):
        line, behind = ng.firmware_status_line("sw", dict(FW), self.img())
        self.assertTrue(behind)
        self.assertIn("BEHIND", line)
        line, behind = ng.firmware_status_line("sw", dict(FW, version="6.7.0.42"), self.img())
        self.assertFalse(behind)
        self.assertFalse(ng.firmware_status_line("sw", None, self.img())[1])

    def test_slot_after_upload_prefers_other_slot(self):
        self.assertEqual(ng.slot_after_upload(self.img(v2="6.7.0.52", nb="2"), "6.7.0.52"), 2)

    def test_slot_after_upload_missing(self):
        with self.assertRaises(ng.NgError):
            ng.slot_after_upload(self.img(), "6.7.0.52")

    def test_next_boot_payload(self):
        x = ng.next_boot_payload(2, "1", "").decode()
        self.assertIn('<ImageUnitList action="set"><Entry><nextBootImage>2</nextBootImage><unitID>1</unitID>'
                      '<currentImage>1</currentImage><image2Description', x)
        self.assertNotIn("image1Description", x)
        with self.assertRaises(ng.NgError):
            ng.next_boot_payload(3, "1", "")

    def test_multipart(self):
        body, ctype = ng.multipart_body([("a", "1"), ("rlCopyDestinationFileType", "8")], "srcFileName", "f.bin", b"\x00\x01")
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertIn(b'name="rlCopyDestinationFileType"\r\n\r\n8\r\n', body)
        self.assertIn(b'filename="f.bin"\r\nContent-Type: application/octet-stream\r\n\r\n\x00\x01\r\n', body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))


class TestExportFirmwareHeader(unittest.TestCase):
    def test_version_read(self):
        self.assertEqual(ng.export_firmware_version(SAMPLE), "6.7.0.42")

    def test_header_ignored_by_unmanaged_diff(self):
        upgraded = SAMPLE.replace("v6.7.0.42 / TEST", "v6.7.0.52 / TEST")
        self.assertEqual(ng.unmanaged_diff(SAMPLE, upgraded), "")
        changed = SAMPLE.replace("scc mode disable", "scc mode enable")
        self.assertIn("scc mode enable", ng.unmanaged_diff(SAMPLE, changed))

    def test_image_table_selector_ignored_by_unmanaged_diff(self):
        line = "snmp-server set  rndImageInfoTable rndStackUnitNumber 1"
        live = SAMPLE.replace("scc mode disable", "scc mode disable\n" + line)
        self.assertNotEqual(live, SAMPLE)
        self.assertEqual(ng.unmanaged_diff(SAMPLE, live), "")
        self.assertEqual(ng.unmanaged_diff(live, SAMPLE), "")
        other = SAMPLE.replace("scc mode disable", "scc mode disable\nsnmp-server set  rlFoo rlFoo 1")
        self.assertIn("rlFoo", ng.unmanaged_diff(SAMPLE, other))


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



IMAGE_BYTES = b"x" * 10
IMAGE_MD5 = hashlib.md5(IMAGE_BYTES).hexdigest()


class FakeSwitch:
    """Stands in for Switch in the firmware command tests (no network).

    Tracks two slots like the real switch: after a reboot into the next-boot slot,
    that image becomes image1 (the switch numbers slots from the running image)."""

    def __init__(self, images, upload_status=None, **_):
        self.images = images
        self.upload_status = upload_status
        self.calls = []
        self.state = None
        self.mgmt = [4]
        self.after_upload = {}
        self.md5 = {}            # slot -> MD5 of the stored image
        self.boots = []          # what each reboot does: "next" (boot the next-boot slot) or "fallback"
        self.healthy_after_boot = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, attempts=4):
        pass

    def logout(self):
        pass

    def image_status(self):
        return dict(self.images)

    def stored_images(self):
        cur = self.images["current"]
        out = {}
        for kind, s in (("active", cur), ("inactive", 3 - cur)):
            if self.images[f"image{s}"]:
                out[kind] = {"name": self.images[f"image{s}_name"], "version": self.images[f"image{s}"],
                             "md5": self.md5.get(s, "0" * 32)}
        return out

    def set_next_boot(self, slot):
        self.calls.append(("set_next_boot", slot))
        self.images["next"] = slot

    def read_state(self):
        return self.state, self.mgmt

    def export(self):
        return "exported"

    def clear_load_status(self):
        pass

    def _loaded(self):
        self.images.update(self.after_upload)
        self.md5.setdefault(3 - self.images["current"], IMAGE_MD5)

    def upload_firmware(self, image, timeout=0, progress=None):
        self.calls.append(("upload", image.name))
        self._loaded()
        return self.upload_status

    def tftp_download(self, server, image, timeout=0, progress=None):
        self.calls.append(("tftp", server, image.name))
        self._loaded()
        return self.upload_status

    def reboot(self):
        self.calls.append(("reboot",))
        if not self.boots:
            raise RuntimeError("stop after reboot")  # the test ends here
        what = self.boots.pop(0)
        cur = self.images["current"]
        boot = self.images["next"] if what == "next" else cur
        other = 3 - boot
        im = self.images
        new = {"image1": im[f"image{boot}"], "image1_name": im[f"image{boot}_name"],
               "image2": im[f"image{other}"], "image2_name": im[f"image{other}_name"], "current": 1}
        new["next"] = 1 if im["next"] == boot else 2
        self.md5 = {1: self.md5.get(boot), 2: self.md5.get(other)}
        self.images = new
        if not self.healthy_after_boot and new["image1"] == "6.7.0.52":
            self.mgmt = []

    def wait_for_return(self, timeout, down_timeout=180, log=None):
        return 0


def _images(v2="6.7.0.42", n2="image2.bin", nb=2):
    return {"image1": "6.7.0.42", "image2": v2, "current": 1, "next": nb,
            "image1_name": "flash://system/images/image1.bin", "image2_name": f"flash://system/images/{n2}"}


GOOD_LOAD = {"image2": "6.7.0.52", "image2_name": "flash://system/images/MS510TX_TXPP_V6.7.0.52.bin", "next": 2}
DONE = {"bytesTransfered": "10", "copyStatusType": "5"}


class TestFirmwareCommands(unittest.TestCase):
    def setUp(self):
        self.desired = HERE / "netgear-movie.yml"
        mock.patch.object(ng, "credentials", return_value={"host": "h", "username": "u", "password": "p"}).start()
        self.drift = mock.patch.object(ng, "cmd_drift", return_value=0).start()

    def tearDown(self):
        mock.patch.stopall()

    def run_set(self, sw, slot, dry=False):
        with mock.patch.object(ng, "Switch", return_value=sw), redirect_stdout(io.StringIO()):
            return ng.cmd_firmware_set_next_boot(argparse.Namespace(file=str(self.desired), slot=slot, dry_run=dry))

    def test_set_next_boot_to_running_image(self):
        sw = FakeSwitch(_images(v2="6.7.0.52", n2="rs", nb=2))
        self.assertEqual(self.run_set(sw, 1), 0)
        self.assertEqual(sw.calls, [("set_next_boot", 1)])

    def test_set_next_boot_refuses_invalid_image(self):
        sw = FakeSwitch(_images(v2="6.7.0.52", n2="rs", nb=1))
        with self.assertRaises(ng.NgError):
            self.run_set(sw, 2)
        self.assertEqual(sw.calls, [])

    def test_set_next_boot_noop_and_dry_run(self):
        sw = FakeSwitch(_images(nb=1))
        self.assertEqual(self.run_set(sw, 1), 0)
        self.assertEqual(self.run_set(sw, 2, dry=True), 0)
        self.assertEqual(sw.calls, [])

    def _args(self, tmp, tftp="192.0.2.1", dry=False, file=None):
        return argparse.Namespace(file=str(file or self.desired), cache_dir=tmp, backup_dir=tmp, export_dir=None,
                                  report=None, upload_timeout=1, reboot_timeout=1, dry_run=dry, tftp_server=tftp)

    def _upgrade(self, sw, tftp="192.0.2.1", dry=False, out=None):
        d = ng.load_desired(self.desired)
        sw.state = d["state"]
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / d["firmware"]["image"]
            image.write_bytes(IMAGE_BYTES)
            with mock.patch.object(ng, "Switch", return_value=sw), \
                    mock.patch.object(ng, "fetch_firmware", return_value=image), \
                    redirect_stdout(out if out is not None else io.StringIO()):
                return ng.cmd_firmware_upgrade(self._args(tmp, tftp, dry))

    def test_full_upgrade_by_tftp(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = GOOD_LOAD
        sw.boots = ["next"]
        self.assertEqual(self._upgrade(sw), 0)
        image = ng.load_desired(self.desired)["firmware"]["image"]
        self.assertEqual(sw.calls, [("tftp", "192.0.2.1", image), ("reboot",)])
        self.assertEqual(ng.running_version(sw.images), "6.7.0.52")
        self.drift.assert_called_once()

    def test_dry_run_covers_the_flow_without_writing(self):
        sw = FakeSwitch(_images(nb=2), upload_status=DONE)
        out = io.StringIO()
        self.assertEqual(self._upgrade(sw, dry=True, out=out), 0)
        self.assertEqual(sw.calls, [])
        text = out.getvalue()
        for step in ("next boot is image2, not the running image1", "would load", "by TFTP from 192.0.2.1",
                     "would set image2 as next boot, reboot", "rolling back to 6.7.0.42"):
            self.assertIn(step, text)
        self.drift.assert_not_called()

    def test_already_on_desired_version_is_a_noop(self):
        sw = FakeSwitch({**_images(v2="6.7.0.42", nb=1), "image1": "6.7.0.52"})
        self.assertEqual(self._upgrade(sw), 0)
        self.assertEqual(sw.calls, [])
        self.drift.assert_not_called()

    def test_already_on_desired_version_fixes_next_boot(self):
        sw = FakeSwitch({**_images(v2="6.7.0.42", nb=2), "image1": "6.7.0.52"})
        self.assertEqual(self._upgrade(sw), 0)
        self.assertEqual(sw.calls, [("set_next_boot", 1)])

    def test_rerun_reuses_verified_image_in_inactive_slot(self):
        sw = FakeSwitch(_images(v2="6.7.0.52", n2="MS510TX_TXPP_V6.7.0.52.bin", nb=2))
        sw.md5 = {2: IMAGE_MD5}
        sw.boots = ["next"]
        self.assertEqual(self._upgrade(sw), 0)
        # next boot is reset to the running image first, then set to the new one for the reboot
        self.assertEqual(sw.calls, [("set_next_boot", 1), ("set_next_boot", 2), ("reboot",)])

    def test_stale_image_in_inactive_slot_is_loaded_again(self):
        sw = FakeSwitch(_images(v2="6.7.0.52", n2="MS510TX_TXPP_V6.7.0.52.bin", nb=1), upload_status=DONE)
        sw.md5 = {2: "f" * 32}  # same version, different file
        sw.after_upload = GOOD_LOAD

        def load(server, image, timeout=0, progress=None):
            sw.calls.append(("tftp", server, image.name))
            sw.images.update(GOOD_LOAD)
            sw.md5[2] = IMAGE_MD5
            return DONE
        sw.tftp_download = load
        with self.assertRaises(RuntimeError):
            self._upgrade(sw)
        self.assertEqual([c[0] for c in sw.calls], ["tftp", "reboot"])

    def test_truncated_image_restores_next_boot_and_does_not_reboot(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = {"image2": "6.7.0.52", "image2_name": "flash://system/images/rs", "next": 2}
        with self.assertRaises(ng.NgError) as cm:
            self._upgrade(sw, tftp=None)
        self.assertIn("DID NOT VERIFY", str(cm.exception))
        self.assertIn(("set_next_boot", 1), sw.calls)
        self.assertNotIn(("reboot",), sw.calls)
        self.assertEqual(sw.images["next"], 1)

    def test_size_mismatch_does_not_reboot(self):
        sw = FakeSwitch(_images(nb=1), upload_status={"bytesTransfered": "9", "copyStatusType": "5"})
        sw.after_upload = GOOD_LOAD
        with self.assertRaises(ng.NgError) as cm:
            self._upgrade(sw)
        self.assertIn("bytes transferred", str(cm.exception))
        self.assertNotIn(("reboot",), sw.calls)
        self.assertEqual(sw.images["next"], 1)

    def test_md5_mismatch_does_not_reboot(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = GOOD_LOAD
        sw.md5 = {2: "e" * 32}
        with self.assertRaises(ng.NgError) as cm:
            self._upgrade(sw)
        self.assertIn("MD5", str(cm.exception))
        self.assertNotIn(("reboot",), sw.calls)

    def test_switch_falls_back_by_itself(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = GOOD_LOAD
        sw.boots = ["fallback"]
        with self.assertRaises(ng.NgError) as cm:
            self._upgrade(sw)
        self.assertIn("came back running 6.7.0.42", str(cm.exception))
        self.assertEqual(ng.running_version(sw.images), "6.7.0.42")
        self.assertEqual(sw.images["next"], sw.images["current"])  # will not try the new image again
        self.drift.assert_not_called()

    def test_unhealthy_after_boot_rolls_back(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = GOOD_LOAD
        sw.boots = ["next", "next"]
        sw.healthy_after_boot = False
        with self.assertRaises(ng.NgError) as cm:
            self._upgrade(sw)
        self.assertIn("rolled back to 6.7.0.42", str(cm.exception))
        self.assertEqual(ng.running_version(sw.images), "6.7.0.42")
        self.assertEqual(sw.calls.count(("reboot",)), 2)
        self.assertIn(("set_next_boot", 2), sw.calls[-2:])  # the old image is image2 after the first boot
        self.drift.assert_not_called()

    def test_unmanaged_drift_after_upgrade_is_reported_not_rolled_back(self):
        sw = FakeSwitch(_images(nb=1), upload_status=DONE)
        sw.after_upload = GOOD_LOAD
        sw.boots = ["next"]
        self.drift.return_value = 2
        out = io.StringIO()
        self.assertEqual(self._upgrade(sw, out=out), ng.UPGRADED_WITH_DRIFT)
        self.assertEqual(sw.calls.count(("reboot",)), 1)
        self.assertIn("set-next-boot netgear-movie.yml 2", out.getvalue())

    def _rollout(self, results, window=None):
        files = [str(HERE / "netgear-movie.yml"), str(HERE / "netgear-office.yml")]
        args = argparse.Namespace(files=files, window=window, settle=0, cache_dir=None, backup_dir="b",
                                  export_dir=None, report_prefix="r-", upload_timeout=1, tftp_server=None,
                                  reboot_timeout=1, dry_run=False)
        seen = []

        def up(ns):
            seen.append((Path(ns.file).stem, ns.report))
            return results.pop(0)
        with mock.patch.object(ng, "cmd_firmware_upgrade", side_effect=up), redirect_stdout(io.StringIO()):
            return ng.cmd_firmware_rollout(args), seen

    def test_rollout_in_order(self):
        rc, seen = self._rollout([0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [("netgear-movie", "r-netgear-movie.txt"), ("netgear-office", "r-netgear-office.txt")])

    def test_rollout_stops_at_first_failure(self):
        for first in (1, ng.UPGRADED_WITH_DRIFT):
            rc, seen = self._rollout([first, 0])
            self.assertEqual(rc, first)
            self.assertEqual([s for s, _ in seen], ["netgear-movie"])

    def test_rollout_outside_window_does_nothing(self):
        with mock.patch.object(ng, "in_window", return_value=False):
            rc, seen = self._rollout([0, 0], window="04:45-06:00")
        self.assertEqual((rc, seen), (0, []))

    def test_in_window(self):
        def at(h, m):
            return time.struct_time((2026, 11, 5, h, m, 0, 3, 309, 0))
        self.assertTrue(ng.in_window("04:45-06:00", at(4, 45)))
        self.assertTrue(ng.in_window("04:45-06:00", at(5, 59)))
        self.assertFalse(ng.in_window("04:45-06:00", at(6, 0)))
        self.assertFalse(ng.in_window("04:45-06:00", at(3, 0)))
        self.assertTrue(ng.in_window("23:00-01:00", at(0, 30)))
        self.assertFalse(ng.in_window("23:00-01:00", at(12, 0)))
        for bad in ("4-6", "25:00-26:00", "05:00-05:00", ""):
            with self.assertRaises(ng.NgError):
                ng.in_window(bad, at(5, 0))


BOOTVAR = """------------------ show bootvar ------------------
Active-image: flash://system/images/image1.bin
  Version: 6.7.0.42
  MD5 Digest: 4c04bb2e4a5d783f6399a790e69a1b63
  Date: 17-Dec-2019
  Time: 12:31:41
  Inactive after reboot
Inactive-image: flash://system/images/MS510TX_TXPP_V6.7.0.52.bin
  Version: 6.7.0.52
  MD5 Digest: d9ceb309108378b437fa9ebb958750b4
  Date: 15-Nov-2021
  Time: 15:32:03
  Active after reboot
------------------ buffers info ------------------
"""


class TestBootvar(unittest.TestCase):
    def test_parse(self):
        b = ng.parse_bootvar("------ show version ------\n" + BOOTVAR.split("\n", 1)[1] + BOOTVAR)
        self.assertEqual(b["active"], {"name": "flash://system/images/image1.bin", "version": "6.7.0.42",
                                       "md5": "4c04bb2e4a5d783f6399a790e69a1b63"})
        self.assertEqual(b["inactive"]["version"], "6.7.0.52")
        self.assertEqual(b["inactive"]["md5"], "d9ceb309108378b437fa9ebb958750b4")

    def test_missing(self):
        with self.assertRaises(ng.NgError):
            ng.parse_bootvar("no images here")

    def test_md5_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.bin"
            p.write_bytes(IMAGE_BYTES)
            self.assertEqual(ng.md5_file(p), IMAGE_MD5)


HEAD_FIXTURE = json.loads((HERE / "testdata" / "download-server-head.json").read_text())
REAL_FW = {"version": "6.7.0.42",
           "url": "https://www.downloads.netgear.com/files/GDC/MS510TX/MS510TX_TXPP_V6.7.0.42.zip"}


def fixture_head(fixture=HEAD_FIXTURE):
    def head(url):
        m = re.search(r"_V(\d+\.\d+\.\d+\.\d+)\.zip$", url)
        e = fixture.get(m.group(1)) if m else None
        if e is None:
            return 403, {"content-type": "application/xml"}
        return e["status"], e["headers"]
    return head


class TestCheckLatest(unittest.TestCase):
    def test_versions(self):
        self.assertLess(ng.version_tuple("6.7.0.9"), ng.version_tuple("6.7.0.10"))
        with self.assertRaises(ng.NgError):
            ng.version_tuple("6.7.0")
        c = ng.candidate_versions("6.7.0.52", span=3)
        self.assertEqual(c, ["6.7.0.53", "6.7.0.54", "6.7.0.55", "6.7.1.0", "6.7.1.1", "6.7.1.2",
                             "6.8.0.0", "6.8.0.1", "6.8.0.2", "7.0.0.0", "7.0.0.1", "7.0.0.2"])

    def test_release_url(self):
        self.assertEqual(ng.release_url(REAL_FW["url"], "6.7.0.42", "6.7.0.52"),
                         "https://www.downloads.netgear.com/files/GDC/MS510TX/MS510TX_TXPP_V6.7.0.52.zip")
        with self.assertRaises(ng.NgError):
            ng.release_url("https://x/fw.zip", "6.7.0.42", "6.7.0.52")

    def test_finds_newest_against_captured_server_answers(self):
        self.assertEqual(ng.find_latest(REAL_FW, head=fixture_head()),
                         ("6.7.0.52", REAL_FW["url"].replace("6.7.0.42", "6.7.0.52")))
        cur = dict(REAL_FW, version="6.7.0.52", url=REAL_FW["url"].replace("6.7.0.42", "6.7.0.52"))
        self.assertEqual(ng.find_latest(cur, head=fixture_head())[0], "6.7.0.52")

    def test_finds_next_minor_release(self):
        fx = dict(HEAD_FIXTURE)
        fx["6.7.1.3"] = fx["6.7.0.52"]
        self.assertEqual(ng.find_latest(REAL_FW, head=fixture_head(fx))[0], "6.7.1.3")

    def test_fails_loudly_when_the_server_changes(self):
        fx = {k: v for k, v in HEAD_FIXTURE.items() if k != "6.7.0.42"}
        with self.assertRaises(ng.NgError) as cm:  # declared release gone: URL scheme changed
            ng.find_latest(REAL_FW, head=fixture_head(fx))
        self.assertIn("not on the download server", str(cm.exception))
        fx = dict(HEAD_FIXTURE)
        fx["0.0.0.0"] = fx["6.7.0.52"]  # everything answers 200
        with self.assertRaises(ng.NgError):
            ng.find_latest(REAL_FW, head=fixture_head(fx))
        fx = dict(HEAD_FIXTURE)
        fx["6.7.0.53"] = {"status": 200, "headers": {"content-type": "text/html"}}  # a soft-404 page
        with self.assertRaises(ng.NgError) as cm:
            ng.find_latest(REAL_FW, head=fixture_head(fx))
        self.assertIn("not a zip", str(cm.exception))
        fx = dict(HEAD_FIXTURE)
        fx["6.7.0.53"] = {"status": 500, "headers": {}}
        with self.assertRaises(ng.NgError):
            ng.find_latest(REAL_FW, head=fixture_head(fx))


def _image_blob(version: str) -> bytes:
    head = bytearray(256)
    head[12:16] = b"NG10"
    head[0x14:0x14 + len(version)] = version.encode()
    return bytes(head) + b"payload"


NOTES_HTML = """<html><body><h2>MS510TX/MS510TXPP Firmware version 6.7.0.60</h2><p>Release Date: May 1, 2027</p>
<h3>Bug Fixes</h3><ul><li>Fixed&nbsp;one thing.</li><li>Fixed another.</li></ul><script>x()</script></body></html>"""


class TestBump(unittest.TestCase):
    def make_zip(self, tmp, version="6.7.0.60", header=None, extra=()):
        z = Path(tmp) / f"MS510TX_TXPP_V{version}.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr(f"MS510TX_TXPP_V{version}.bin", _image_blob(header or version))
            zf.writestr(f"MS510TX_TXPP_{version}.htm", NOTES_HTML)
            for n in extra:
                zf.writestr(n, b"x")
        return z

    def test_describe_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            z = self.make_zip(tmp)
            fw, notes = ng.describe_release(z, "6.7.0.60", "https://example.invalid/MS510TX_TXPP_V6.7.0.60.zip")
            self.assertEqual(fw["image"], "MS510TX_TXPP_V6.7.0.60.bin")
            self.assertEqual(fw["sha256"], ng.sha256_file(z))
            self.assertEqual(fw["image_sha256"], hashlib.sha256(_image_blob("6.7.0.60")).hexdigest())
            self.assertIn("- Fixed one thing.", notes)
            self.assertNotIn("x()", notes)

    def test_describe_release_checks_header_and_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ng.NgError):
                ng.describe_release(self.make_zip(tmp, header="6.7.0.52"), "6.7.0.60", "u")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ng.NgError):
                ng.describe_release(self.make_zip(tmp, extra=["other_6.7.0.60.bin"]), "6.7.0.60", "u")

    def test_replace_firmware_block_on_committed_files(self):
        new = {"version": "6.7.0.60", "url": "https://example.invalid/MS510TX_TXPP_V6.7.0.60.zip",
               "sha256": "1" * 64, "image": "MS510TX_TXPP_V6.7.0.60.bin", "image_sha256": "2" * 64}
        for f in sorted(HERE.glob("netgear-*.yml")):
            text = f.read_text()
            out = ng.replace_firmware_block(text, new)
            self.assertEqual(len(out.splitlines()), len(text.splitlines()))
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / f.name
                p.write_text(out)
                d = ng._load_yaml(p)
            self.assertEqual(d["firmware"], new)
            self.assertEqual({k: v for k, v in d.items() if k != "firmware"},
                             {k: v for k, v in ng._load_yaml(f).items() if k != "firmware"})

    def test_replace_known_latest_on_committed_report(self):
        text = ng.KNOWN_LATEST_PATH.read_text()
        out = ng.replace_known_latest(text, "MS510TXPP", "6.7.0.60", "2027-05-02")
        self.assertIn('"MS510TXPP": ("6.7.0.60", "2027-05-02"),', out)
        self.assertEqual(len(out), len(text))
        with self.assertRaises(ng.NgError):
            ng.replace_known_latest(text, "GS108", "1.0.0.1", "2027-05-02")

    def test_bump_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            for f in HERE.glob("netgear-*.yml"):
                (Path(tmp) / f.name).write_text(f.read_text())
                (Path(tmp) / f.name.replace(".yml", ".running-config.txt")).write_text(
                    ng.snapshot_path(f).read_text())
            kl = Path(tmp) / "firmware.py"
            kl.write_text(ng.KNOWN_LATEST_PATH.read_text())
            z = self.make_zip(tmp)

            def fake_download(url, dest):
                self.assertTrue(url.endswith("MS510TX_TXPP_V6.7.0.60.zip"))
                return z
            args = argparse.Namespace(files=[str(p) for p in sorted(Path(tmp).glob("netgear-*.yml"))],
                                      version="6.7.0.60", url=None, cache_dir=tmp, known_latest=str(kl),
                                      notes=str(Path(tmp) / "notes.txt"), json=None)
            with mock.patch.object(ng, "download", side_effect=fake_download), redirect_stdout(io.StringIO()):
                self.assertEqual(ng.cmd_firmware_bump(args), 0)
            for p in Path(tmp).glob("netgear-*.yml"):
                self.assertEqual(ng.load_desired(p)["firmware"]["version"], "6.7.0.60")
            self.assertIn('"MS510TXPP": ("6.7.0.60"', kl.read_text())
            self.assertIn("Bug Fixes", (Path(tmp) / "notes.txt").read_text())
            args.version = "6.7.0.52"
            with self.assertRaises(ng.NgError):
                ng.cmd_firmware_bump(args)


class TestTftpServer(unittest.TestCase):
    """The built-in TFTP server, exercised over loopback with a minimal client."""

    def fetch(self, port, name, opts=None, ack_first_block_twice=False):
        import socket
        import struct
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.settimeout(5)
        req = struct.pack("!H", 1) + name.encode() + b"\0octet\0"
        for k, v in (opts or {}).items():
            req += k.encode() + b"\0" + v.encode() + b"\0"
        c.sendto(req, ("127.0.0.1", port))
        out, blksize, expect, oack = b"", 512, 1, {}
        try:
            while True:
                pkt, peer = c.recvfrom(70000)
                op = struct.unpack("!H", pkt[:2])[0]
                if op == 5:
                    return None, pkt[4:].rstrip(b"\0").decode()
                if op == 6:
                    f = pkt[2:].split(b"\0")
                    oack = {f[i].decode(): f[i + 1].decode() for i in range(0, len(f) - 1, 2)}
                    blksize = int(oack.get("blksize", 512))
                    c.sendto(struct.pack("!HH", 4, 0), peer)
                    continue
                num = struct.unpack("!H", pkt[2:4])[0]
                if num == expect:
                    out += pkt[4:]
                    expect += 1
                    if ack_first_block_twice and num == 1:
                        ack_first_block_twice = False
                        continue  # drop this ACK: the server must retransmit block 1
                c.sendto(struct.pack("!HH", 4, num), peer)
                if num == expect - 1 and len(pkt) - 4 < blksize:
                    return out, oack
        finally:
            c.close()

    def serve(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "fw.bin"
        p.write_bytes(data)
        srv = ng.TftpServer("127.0.0.1", p, port=0)
        srv.TIMEOUT = 0.3
        srv.__enter__()
        self.addCleanup(srv.__exit__)
        return srv

    def test_serves_file(self):
        data = bytes(range(256)) * 9 + b"tail"
        srv = self.serve(data)
        got, _ = self.fetch(srv.port, "fw.bin")
        self.assertEqual(got, data)
        import time
        for _ in range(50):  # the server counts the last block once its ACK arrives
            if srv.sent == len(data):
                break
            time.sleep(0.02)
        self.assertEqual((srv.sent, srv.transfers), (len(data), 1))

    def test_exact_block_multiple_ends_with_empty_block(self):
        data = b"a" * 1024
        srv = self.serve(data)
        got, _ = self.fetch(srv.port, "fw.bin")
        self.assertEqual(got, data)

    def test_options_and_retransmit(self):
        data = b"z" * 5000
        srv = self.serve(data)
        got, oack = self.fetch(srv.port, "fw.bin", {"blksize": "1024", "tsize": "0"}, ack_first_block_twice=True)
        self.assertEqual(got, data)
        self.assertEqual(oack, {"blksize": "1024", "tsize": "5000"})

    def test_other_files_refused(self):
        srv = self.serve(b"x")
        got, msg = self.fetch(srv.port, "../etc/passwd")
        self.assertIsNone(got)
        self.assertIn("not found", msg)
        self.assertTrue(srv.errors)


class TestSwitchFirmwareCalls(unittest.TestCase):
    def sw(self):
        s = ng.Switch("192.0.2.1", "u", "p")
        s.cs = "cs0"
        return s

    def test_reboot_sends_unit_0_like_the_device_reboot_page(self):
        s = self.sw()
        with mock.patch.object(s, "post") as post:
            s.reboot()
        self.assertIn(b"<Reload action=\"set\"><UnitList><UnitEntry><unitID>0</unitID>", post.call_args[0][0])
        self.assertIsNone(s.cs)

    def test_wait_for_load_tolerates_404_during_flash_write(self):
        import urllib.error
        s = self.sw()
        err = urllib.error.HTTPError("http://x", 404, "Site or Page Not Found", {}, None)
        seq = [{"copyStatusType": "2", "bytesTransfered": "512"}, err, err,
               {"copyStatusType": "5", "bytesTransfered": "1024"}]

        def status():
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        with mock.patch.object(s, "load_status", side_effect=status), \
                mock.patch.object(s, "clear_load_status"), mock.patch.object(ng.time, "sleep"):
            st = s.wait_for_load(60)
        self.assertEqual(st["bytesTransfered"], "1024")

    def test_wait_for_load_still_reports_failure(self):
        s = self.sw()
        with mock.patch.object(s, "load_status", return_value={"copyStatusType": "4", "errorMessage": "boom"}), \
                mock.patch.object(s, "clear_load_status"), self.assertRaises(ng.NgError) as cm:
            s.wait_for_load(60)
        self.assertIn("boom", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
