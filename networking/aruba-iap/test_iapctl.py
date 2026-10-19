"""Offline tests for iapctl.py (no device, no Vault)."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iapctl  # noqa: E402

RAW = """show running-config
version 8.13.1.0-8.13.1
virtual-controller-country US
virtual-controller-key 0123456789abcdef0123456789abcdef0123456789abcdef01
name Test_IAP
allowed-ap aa:bb:cc:dd:ee:01
syslog-level warn system

hash-mgmt-password
hash-mgmt-user admin password hash 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff0011 usertype admin

wlan access-rule guest
 index 1
 rule any any match any any any permit

wlan ssid-profile guest
 enable
 index 1
 essid guest
 wpa-passphrase deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef
 opmode wpa2-psk-aes
 vlan 5
 captive-portal disable

wlan auth-server rad1
 ip 10.0.0.9
 port 1812
 key cafebabecafebabecafebabecafebabe

snmp-server community s3cr3tcomm

wired-port-profile uplinkprof
 switchport-mode trunk
 native-vlan 4
 no shutdown

enet0-port-profile uplinkprof

uplink
 preemption
 enforce none
test-ap#
"""


def sanitized():
    return iapctl.sanitize(iapctl.parse(iapctl.clean_cli_output(RAW)))


def edit(blocks, header, old=None, new=None):
    """Return a copy of blocks with one child line of `header` replaced/added/removed."""
    out = []
    for b in blocks:
        ch = list(b.children)
        if b.header == header:
            if old is not None and new is not None:
                ch = [new if c == old else c for c in ch]
            elif old is not None:
                ch.remove(old)
            elif new is not None:
                ch.append(new)
        out.append(iapctl.Block(b.header, ch))
    return out


class SanitizeTests(unittest.TestCase):
    def test_no_secret_values_survive(self):
        text = iapctl.render(sanitized())
        for secret in ("0123456789abcdef0123", "00112233445566778899", "deadbeef", "cafebabe", "s3cr3tcomm"):
            self.assertNotIn(secret, text)

    def test_placeholders_reference_vault_fields(self):
        text = iapctl.render(sanitized())
        self.assertIn("wpa-passphrase {{vault:homelab/network/aruba-iap-ssids#guest}}", text)
        self.assertIn("virtual-controller-key {{vault:homelab/network/aruba-iap#virtual_controller_key}}", text)
        self.assertIn("hash-mgmt-user admin password hash {{vault:homelab/network/aruba-iap#mgmt_user_hash}} usertype admin", text)
        self.assertIn(" key {{vault:homelab/network/aruba-iap#rad1_key}}", text)
        self.assertIn("snmp-server community {{vault:homelab/network/aruba-iap#snmp_community}}", text)

    def test_non_secret_lines_untouched(self):
        text = iapctl.render(sanitized())
        self.assertIn(" opmode wpa2-psk-aes", text)
        self.assertIn("hash-mgmt-password", text)

    def test_prompt_echo_and_version_dropped(self):
        text = iapctl.render(sanitized())
        self.assertNotIn("show running-config", text)
        self.assertNotIn("test-ap#", text)
        self.assertNotIn("version ", text)

    def test_roundtrip_is_stable(self):
        once = iapctl.render(sanitized())
        twice = iapctl.render(iapctl.sanitize(iapctl.parse(once)))
        self.assertEqual(once, twice)

    def test_committed_file_has_no_secrets(self):
        path = iapctl.DEFAULT_DESIRED
        if not os.path.exists(path):
            self.skipTest("no committed config")
        with open(path) as f:
            for n, line in enumerate(f, 1):
                if line.startswith("#"):
                    continue
                self.assertIsNone(re.search(r"\b[0-9a-fA-F]{32,}\b", line), "hex blob on line %d" % n)
                toks = line.split()
                if toks and re.search(r"(key|secret|passphrase|password|passwd|community|psk)$", toks[0], re.I) \
                        and len(toks) > 1:
                    self.assertTrue(iapctl.is_secret_line(line), "unmasked secret on line %d" % n)

    def test_committed_file_parses(self):
        path = iapctl.DEFAULT_DESIRED
        if not os.path.exists(path):
            self.skipTest("no committed config")
        blocks = iapctl.load(path)
        self.assertTrue(any(b.header.startswith("wlan ssid-profile") for b in blocks))
        self.assertEqual(iapctl.diff(blocks, blocks), [])


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.base = sanitized()

    def plan(self, desired):
        return iapctl.compute(self.base, desired)

    def test_identical_is_empty(self):
        changes, _ = self.plan(sanitized())
        self.assertEqual(changes, [])

    def test_placeholder_matches_live_encrypted_value(self):
        live = iapctl.parse(iapctl.clean_cli_output(RAW))  # unsanitised
        self.assertEqual(iapctl.diff(live, sanitized()), [])

    def test_ssid_vlan_change_sends_only_that_line(self):
        d = edit(self.base, "wlan ssid-profile guest", " vlan 5", " vlan 8")
        changes, guards = self.plan(d)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].commands, ["wlan ssid-profile guest", "vlan 8"])
        self.assertEqual(guards[0], [])

    def test_removed_flag_is_negated(self):
        d = edit(self.base, "wlan ssid-profile guest", old=" captive-portal disable")
        changes, _ = self.plan(d)
        self.assertEqual(changes[0].commands, ["wlan ssid-profile guest", "no captive-portal disable"])

    def test_new_block_sends_all_children(self):
        d = list(self.base) + [iapctl.Block("wlan ssid-profile new", [" enable", " vlan 6"])]
        changes, _ = self.plan(d)
        self.assertEqual(changes[0].kind, "add-block")
        self.assertEqual(changes[0].commands, ["wlan ssid-profile new", "enable", "vlan 6"])

    def test_removed_block(self):
        d = [b for b in self.base if b.header != "wlan auth-server rad1"]
        changes, _ = self.plan(d)
        self.assertEqual([(c.kind, c.commands) for c in changes], [("remove-block", ["no wlan auth-server rad1"])])

    def test_single_valued_top_line_replaced(self):
        d = [iapctl.Block("name Other") if b.header == "name Test_IAP" else b for b in self.base]
        changes, _ = self.plan(d)
        self.assertEqual([(c.kind, c.commands) for c in changes], [("replace-line", ["name Other"])])

    def test_syslog_level_keyed_by_facility(self):
        d = [iapctl.Block("syslog-level info system") if b.header == "syslog-level warn system" else b
             for b in self.base]
        changes, _ = self.plan(d)
        self.assertEqual([(c.kind, c.commands) for c in changes], [("replace-line", ["syslog-level info system"])])

    def test_access_rule_change(self):
        d = edit(self.base, "wlan access-rule guest", new=" rule any any match udp 53 53 deny")
        changes, _ = self.plan(d)
        self.assertEqual(changes[0].commands,
                         ["wlan access-rule guest", "rule any any match udp 53 53 deny"])
        self.assertEqual(changes[0].warnings, [])


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.base = sanitized()

    def reasons(self, desired):
        changes, guards = iapctl.compute(self.base, desired)
        return [r for g in guards.values() for r in g]

    def test_uplink_block_protected(self):
        d = edit(self.base, "uplink", " enforce none", " enforce ethernet")
        self.assertIn("uplink", self.reasons(d))

    def test_uplink_port_profile_protected(self):
        d = edit(self.base, "wired-port-profile uplinkprof", " native-vlan 4", " native-vlan 1")
        self.assertTrue(any("uplink" in r for r in self.reasons(d)))

    def test_vc_ip_protected(self):
        d = list(self.base) + [iapctl.Block("virtual-controller-ip 10.4.0.250")]
        self.assertIn("virtual controller address/VLAN", self.reasons(d))

    def test_mesh_and_membership_protected(self):
        d = [b for b in self.base if b.header != "allowed-ap aa:bb:cc:dd:ee:01"]
        self.assertIn("cluster membership", self.reasons(d))
        d2 = list(self.base) + [iapctl.Block("mesh-disable")]
        self.assertTrue(any("mesh" in r for r in self.reasons(d2)))

    def test_ssid_change_not_protected(self):
        d = edit(self.base, "wlan ssid-profile guest", " vlan 5", " vlan 7")
        self.assertEqual(self.reasons(d), [])


class SecretsTests(unittest.TestCase):
    def test_resolve_and_missing(self):
        s = iapctl.Secrets(lambda path: {"guest": "pw"} if path.endswith("ssids") else None)
        line = "wpa-passphrase {{vault:homelab/network/aruba-iap-ssids#guest}}"
        self.assertEqual(s.resolve(line), "wpa-passphrase pw")
        with self.assertRaises(KeyError):
            s.get("homelab/network/aruba-iap-ssids", "other")

    def test_new_ssid_needs_secret(self):
        base = sanitized()
        d = list(base) + [iapctl.Block("wlan ssid-profile new", [
            " wpa-passphrase {{vault:homelab/network/aruba-iap-ssids#new}}"])]
        changes, _ = iapctl.compute(base, d)
        self.assertTrue(changes[0].needs_secret)

    def test_plan_never_prints_resolved_secret(self):
        base = sanitized()
        d = list(base) + [iapctl.Block("wlan ssid-profile new", [
            " wpa-passphrase {{vault:homelab/network/aruba-iap-ssids#new}}"])]
        changes, guards = iapctl.compute(base, d)
        self.assertIn("{{vault:", iapctl.format_plan(changes, guards))


class OfflinePlanCli(unittest.TestCase):
    def test_offline_plan_exit_codes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            a, b = os.path.join(t, "a.conf"), os.path.join(t, "b.conf")
            with open(a, "w") as f:
                f.write(iapctl.render(sanitized()))
            with open(b, "w") as f:
                f.write(iapctl.render(edit(sanitized(), "wlan ssid-profile guest", " vlan 5", " vlan 9")))
            self.assertEqual(iapctl.main(["plan", "--current", a, "--desired", a, "--detailed-exitcode"]), 0)
            self.assertEqual(iapctl.main(["plan", "--current", a, "--desired", b, "--detailed-exitcode"]), 2)


if __name__ == "__main__":
    unittest.main()
