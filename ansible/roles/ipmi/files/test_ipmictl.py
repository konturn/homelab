"""Offline tests for ipmictl.py (no BMC needed). Run:
python3 -m unittest discover -s ansible/roles/ipmi/files -p 'test_*.py' -v
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ipmictl  # noqa: E402

# Synthetic outputs in the format ipmitool 1.8.19 prints on an X9 board.
LAN = """Set in Progress         : Set Complete
IP Address Source       : DHCP Address
IP Address              : 192.0.2.7
Subnet Mask             : 255.255.0.0
MAC Address             : 02:00:00:00:00:01
Default Gateway IP      : 192.0.2.1
802.1q VLAN ID          : Disabled
RMCP+ Cipher Suites     : 1,2,3,6,7,8,11,12
Cipher Suite Priv Max   : XaaaXXaaaXXaaXX
                        :     X=Cipher Suite Unused
"""
SOL = """Set in progress                 : set-complete
Enabled                         : true
Force Encryption                : false
Force Authentication            : false
Privilege Level                 : OPERATOR
Character Accumulate Level (ms) : 50
Character Send Threshold        : 80
Retry Count                     : 0
Retry Interval (ms)             : 0
Volatile Bit Rate (kbps)        : 115.2
Non-Volatile Bit Rate (kbps)    : 115.2
Payload Channel                 : 1 (0x01)
Payload Port                    : 623
"""
CHASSIS = """System Power         : on
Power Overload       : false
Power Interlock      : inactive
Main Power Fault     : false
Power Control Fault  : false
Power Restore Policy : always-on
Last Power Event     :
Chassis Intrusion    : inactive
Front-Panel Lockout  : inactive
Drive Fault          : false
Cooling/Fan Fault    : false
"""
SENSOR = """CPU1 Temp        | 41.000     | degrees C  | ok    | 0.000     | 0.000     | 0.000     | 86.000    | 89.000    | 91.000
CPU2 Temp        | 90.000     | degrees C  | cr    | 0.000     | 0.000     | 0.000     | 86.000    | 89.000    | 91.000
P1-DIMMA2 TEMP   | na         |            | na    | na        | na        | na        | na        | na        | na
FAN1             | 6975.000   | RPM        | ok    | 150.000   | 225.000   | 300.000   | 18975.000 | 19050.000 | 19125.000
FAN2             | 0.000      | RPM        | lnr   | 150.000   | 225.000   | 300.000   | 18975.000 | 19050.000 | 19125.000
12V              | 11.978     | Volts      | ok    | 10.176    | 10.494    | 10.812    | 13.250    | 13.568    | 13.886
Chassis Intru    | 0x1        | discrete   | 0x0100| na        | na        | na        | na        | na        | na
PS1 Status       | 0x1        | discrete   | 0x0100| na        | na        | na        | na        | na        | na
PS2 Status       | 0x3        | discrete   | 0x0300| na        | na        | na        | na        | na        | na
"""
SEL_INFO = """SEL Information
Version          : 1.5 (v1.5, v2 compliant)
Entries          : 3
Free Space       : 10180 bytes
Percent Used     : 0%
Last Add Time    : 11/06/2026 10:00:00
Last Del Time    : Not Available
Overflow         : false
# of Alloc Units : 512
"""
SEL_LIST = """   1 | 11/06/2026 | 09:00:00 AM EST | Fan FAN2 | Lower Critical going low  | Asserted | Reading 0 < Threshold 225 RPM
   2 | 11/06/2026 | 09:00:10 AM EST | Fan FAN2 | Lower Critical going low  | Deasserted | Reading 6900 > Threshold 225 RPM
   3 |  Pre-Init  |0000000038| Unknown #0xff |  | Asserted
"""


class FakeBmc:
    def __init__(self, over=None):
        self.out = {
            ("lan", "print", "1"): LAN,
            ("sol", "info", "1"): SOL,
            ("chassis", "status"): CHASSIS,
            ("raw", "0x30", "0x45", "0x00"): " 01\n",
            ("raw", "0x06", "0x41", "0x01", "0x40"): " 02 04\n",
            ("mc", "getenables"): "System Event Logging                     : enabled\n",
            ("sensor",): SENSOR,
            ("sel", "info"): SEL_INFO,
            ("sel", "time", "get"): "11/06/2026 10:05:00\n",
            ("sel", "elist"): SEL_LIST,
        }
        self.out.update(over or {})
        self.calls = []

    def __call__(self, args):
        self.calls.append(tuple(args))
        if args and args[0] in ("chassis", "raw", "sol", "sensor", "mc") and tuple(args) not in self.out:
            if tuple(args[:2]) in (("chassis", "policy"), ("sol", "set"), ("sensor", "thresh"),
                                   ("mc", "setenables")) or tuple(args[:4]) == ("raw", "0x30", "0x45", "0x01"):
                return ""
        return self.out[tuple(args)]


DESIRED = {
    "schema": 1,
    "chassis": {"power_restore_policy": "always-on"},
    "fan": {"mode": "full"},
    "sol": {"channel": 1, "enabled": True, "privilege_level": "operator",
            "volatile_bit_rate": "115.2", "non_volatile_bit_rate": "115.2",
            "character_accumulate_level_ms": 50, "character_send_threshold": 80,
            "retry_count": 0, "retry_interval_ms": 0},
    "sel": {"logging_enabled": True},
    "lan": {"channel": 1, "ip_source": "dhcp", "ip_address": "192.0.2.7", "netmask": "255.255.0.0",
            "default_gateway": "192.0.2.1", "vlan": "disabled", "cipher_suite_0": "disabled"},
    "channel_access": {"channel": 1, "access_mode": "always available", "privilege_limit": "administrator"},
    "sensor_thresholds": {"FAN1": {"lower": [150, 225, 300]}, "CPU1 Temp": {"upper": [86, 89, 91]}},
}


class ParserTests(unittest.TestCase):
    def test_channel_access(self):
        a = ipmictl.parse_channel_access(" 02 04")
        self.assertEqual(a["access_mode"], "always available")
        self.assertEqual(a["privilege_limit"], "administrator")

    def test_sensors(self):
        s = {x["name"]: x for x in ipmictl.parse_sensors(SENSOR)}
        self.assertNotIn("P1-DIMMA2 TEMP", s)
        self.assertEqual(s["CPU2 Temp"]["status"], 2)
        self.assertEqual(s["FAN2"]["status"], 3)
        self.assertEqual(s["FAN1"]["thresholds"]["lcr"], 225.0)
        self.assertEqual(s["PS1 Status"]["type"], "psu")
        self.assertEqual(s["PS1 Status"]["status"], 0)
        self.assertEqual(s["PS2 Status"]["status"], 2)  # failure bit
        self.assertEqual(s["Chassis Intru"]["type"], "intrusion")

    def test_classify(self):
        e = ipmictl.parse_sel_elist(SEL_LIST)
        self.assertEqual([ipmictl.classify_sel(x) for x in e], ["critical", "info", "info"])
        self.assertEqual(ipmictl.classify_sel(
            {"sensor": "Memory", "event": "Correctable ECC (@DIMMH1(CPU2))", "direction": "Asserted"}), "ecc")
        self.assertEqual(ipmictl.classify_sel(
            {"sensor": "Memory", "event": "Uncorrectable ECC", "direction": "Asserted"}), "critical")
        self.assertEqual(ipmictl.classify_sel(
            {"sensor": "Temp", "event": "Upper Non-critical going high", "direction": "Asserted"}), "warning")
        self.assertEqual(ipmictl.classify_sel(
            {"sensor": "PS1 Status", "event": "Power Supply AC lost", "direction": "Asserted"}), "critical")

    def test_clock(self):
        self.assertFalse(ipmictl.sel_clock_set("S+ 70/176 17:10:41"))
        self.assertTrue(ipmictl.sel_clock_set("11/06/2026 10:05:00"))


class DiffTests(unittest.TestCase):
    def test_no_drift(self):
        live = ipmictl.read_live(ipmictl.Ipmi(FakeBmc()))
        self.assertEqual(ipmictl.diff(DESIRED, live), [])
        self.assertEqual(live["lan"]["cipher_suite_0"], "disabled")

    def test_drift_and_commands(self):
        live = ipmictl.read_live(ipmictl.Ipmi(FakeBmc({("raw", "0x30", "0x45", "0x00"): " 02\n"})))
        want = dict(DESIRED, lan=dict(DESIRED["lan"], ip_source="static"),
                    sensor_thresholds={"FAN1": {"lower": [300, 450, 600]}})
        items = {"%s.%s" % (d["section"], d["key"]): d for d in ipmictl.diff(want, live)}
        self.assertEqual(set(items), {"fan.mode", "lan.ip_source", "sensor_thresholds.FAN1.lower"})
        self.assertEqual(ipmictl.commands_for(items["fan.mode"]), [["raw", "0x30", "0x45", "0x01", "0x01"]])
        self.assertEqual(ipmictl.commands_for(items["lan.ip_source"]), [])
        self.assertEqual(ipmictl.commands_for(items["sensor_thresholds.FAN1.lower"]),
                         [["sensor", "thresh", "FAN1", "lower", "300", "450", "600"]])

    def test_sol_units(self):
        d = {"section": "sol", "key": "character_accumulate_level_ms", "want": 50, "have": 25, "applyable": True}
        self.assertEqual(ipmictl.commands_for(d), [["sol", "set", "character-accumulate-level", "10", "1"]])
        d = {"section": "sol", "key": "retry_interval_ms", "want": 100, "have": 0, "applyable": True}
        self.assertEqual(ipmictl.commands_for(d), [["sol", "set", "retry-interval", "10", "1"]])

    def test_validate(self):
        self.assertEqual(ipmictl.validate_desired(DESIRED), [])
        bad = dict(DESIRED, fan={"mode": "turbo"}, lan=dict(DESIRED["lan"], cipher_suite_0="enabled"),
                   bogus={})
        errs = ipmictl.validate_desired(bad)
        self.assertEqual(len(errs), 3, errs)


class GuardTests(unittest.TestCase):
    def test_refuses_dangerous(self):
        for args in (["chassis", "power", "cycle"], ["chassis", "bootdev", "pxe"], ["mc", "reset", "cold"],
                     ["sel", "clear"], ["lan", "set", "1", "ipsrc", "static"], ["user", "set", "password", "2"],
                     ["channel", "setaccess", "1", "2"], ["chassis", "policy", "always-off", "x"],
                     ["raw", "0x30", "0x45", "0x01", "0x09"], ["mc", "setenables", "oem0=on"],
                     ["raw", "0x00", "0x02", "0x00"]):
            with self.assertRaises(ipmictl.Refused, msg=args):
                ipmictl.check_write_allowed(args)

    def test_allows_managed_writes(self):
        for args in (["chassis", "policy", "always-on"], ["raw", "0x30", "0x45", "0x01", "0x01"],
                     ["sol", "set", "enabled", "true", "1"], ["sensor", "thresh", "FAN1", "lower", "1", "2", "3"],
                     ["mc", "setenables", "system_event_log=on"]):
            ipmictl.check_write_allowed(args)

    def test_read_rejects_writes(self):
        with self.assertRaises(ipmictl.Refused):
            ipmictl.Ipmi(FakeBmc()).read("chassis", "power", "status")

    def test_apply_refused_in_ci(self):
        os.environ["CI"] = "true"
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write("schema: 1\nfan: {mode: optimal}\n")
            rc = ipmictl.main(["apply", "--execute", "--desired", f.name])
            self.assertEqual(rc, 3)
        finally:
            del os.environ["CI"]
            os.unlink(f.name)


class SelTests(unittest.TestCase):
    def test_baseline_then_new(self):
        bmc = FakeBmc()
        ipmi = ipmictl.Ipmi(bmc)
        lines, state, new = ipmictl.collect_metrics(ipmi, None, now=1000)
        self.assertEqual(new, [])
        self.assertIn("critical_total=0i", [ln for ln in lines if ln.startswith("bmc_sel ")][0])
        # unchanged SEL: no second elist
        n = bmc.calls.count(("sel", "elist"))
        _, state, new = ipmictl.collect_metrics(ipmi, state, now=1060)
        self.assertEqual(bmc.calls.count(("sel", "elist")), n)
        # one new critical entry
        bmc.out[("sel", "info")] = SEL_INFO.replace("Entries          : 3", "Entries          : 4") \
                                           .replace("10:00:00", "10:10:00")
        bmc.out[("sel", "elist")] = SEL_LIST + (
            "   4 | 11/06/2026 | 10:10:00 AM EST | Power Supply PS1 Status | Failure detected () | Asserted\n")
        lines, state, new = ipmictl.collect_metrics(ipmi, state, now=1120)
        self.assertEqual([e["category"] for e in new], ["critical"])
        sel = [ln for ln in lines if ln.startswith("bmc_sel ")][0]
        self.assertIn("new_critical=1i", sel)
        self.assertIn("critical_total=1i", sel)
        self.assertTrue(any(ln.startswith("bmc_sel_event,category=critical") for ln in lines))

    def test_cleared(self):
        state = {"sig": [10, "x", "Not Available"], "seen": ["a"], "totals": {}}
        info = {"entries": 1, "last_add": "y", "last_del": "11/06/2026 10:00:00"}
        _, new = ipmictl.sel_update(state, info, lambda: ipmictl.parse_sel_elist(
            "   1 | 11/06/2026 | 10:00:00 AM EST | Event Logging Disabled SEL | Log area reset/cleared | Asserted\n"))
        self.assertEqual([e["category"] for e in new], ["info"])

    def test_psu_and_fan_lines(self):
        lines, _, _ = ipmictl.collect_metrics(ipmictl.Ipmi(FakeBmc()), None, now=1000)
        ps2 = [ln for ln in lines if "sensor=PS2_Status" in ln][0]
        self.assertIn("fault=1i", ps2)
        self.assertIn("status=2i", ps2)
        fan2 = [ln for ln in lines if "sensor=FAN2" in ln][0]
        self.assertIn("type=fan", fan2)
        self.assertIn("status=3i", fan2)

    def test_config_line(self):
        lines, _, _ = ipmictl.collect_metrics(ipmictl.Ipmi(FakeBmc()), None, desired=DESIRED, now=1000)
        self.assertIn("bmc_config check_ok=1i,drift_items=0i 1000000000000", lines)


class DefaultsTests(unittest.TestCase):
    """The desired state in the role defaults validates once rendered."""

    def test_role_defaults(self):
        import yaml
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", "defaults", "main.yml")) as f:
            text = f.read()
        defaults = yaml.safe_load(text)
        flat = {k: v for k, v in defaults.items() if not isinstance(v, (dict, list))}
        flat["ipmi_address"] = "192.0.2.7"

        def render(v):
            if isinstance(v, dict):
                return {k: render(x) for k, x in v.items()}
            if isinstance(v, list):
                return [render(x) for x in v]
            if isinstance(v, str) and v.startswith("{{"):
                name = v.strip("{} ").split("|")[0].strip()
                return flat[name]
            return v

        desired = render(defaults["ipmi_desired"])
        self.assertEqual(ipmictl.validate_desired(desired), [])
        self.assertEqual(desired["chassis"]["power_restore_policy"], "always-on")


if __name__ == "__main__":
    unittest.main()
