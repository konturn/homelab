#!/usr/bin/env python3
"""Offline tests for s2500ctl.py (no switch, no Vault).

testdata/: the running-version line matches the switch's SNMP sysDescr; the rest
follows the ArubaOS 7.x CLI format and is not captured from this switch yet (no
CLI credentials). Replace it with `s2500ctl.py inventory` output once the
credentials are in Vault.
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import s2500ctl as s  # noqa: E402

TESTDATA = HERE / "testdata"
V = s.parse_version


def fixture(name):
    return (TESTDATA / name).read_text()


def at(hhmm):
    return time.strptime("2026-11-07 " + hhmm, "%Y-%m-%d %H:%M")


class TestParsers(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(V("ArubaOS 7.4.1.12 (72393)"), s.Version("7.4.1.12", 72393))
        self.assertEqual(V("7.4.1.12_72393"), s.Version("7.4.1.12", 72393))
        self.assertEqual(V("ArubaOS 7.4.1.12 (Digitally Signed - Production Build)"), s.Version("7.4.1.12"))
        self.assertLess(V("7.4.1.9_1").key, V("7.4.1.12_1").key)
        self.assertTrue(V("7.4.1.12").same_release(V("7.4.1.12_72393")))
        self.assertEqual(s.parse_image_name("ArubaOS_MAS_7.4.1.12_72393"), V("7.4.1.12_72393"))
        self.assertIsNone(s.parse_image_name("ArubaInstant_Draco_8.13.1.0_93688"))

    def test_show_version(self):
        v, model = s.parse_show_version(fixture("show-version.txt"))
        self.assertEqual((v, model), (s.Version("7.4.1.12", 72393), "ArubaS2500-48P-US"))
        # the same parser reads the SNMP sysDescr
        v, model = s.parse_show_version('"ArubaOS (MODEL: ArubaS2500-48P-US), Version 7.4.1.12 (72393)"')
        self.assertEqual(str(v), "7.4.1.12_72393")

    def test_show_image_version(self):
        parts = s.parse_image_version(fixture("show-image-version.txt"))
        self.assertEqual([(p["partition"], p["number"], str(p["parsed"]), p["default_boot"]) for p in parts],
                         [("0:0", 0, "7.4.1.12_72393", True), ("0:1", 1, "7.4.1.11_71710", False)])
        with self.assertRaises(s.S2500Error):
            s.parse_image_version("% Invalid input\n")

    def test_show_boot(self):
        self.assertEqual(s.parse_show_boot(fixture("show-boot.txt")), 0)
        self.assertIsNone(s.parse_show_boot("nothing"))

    def test_firmware_file(self):
        fw = s.load_firmware()
        self.assertEqual((fw["model"], fw["version"], fw["image"]), ("S2500", "7.4.1.12_72393", None))

    def test_known_hosts_is_pinned(self):
        lines = [ln.split() for ln in (HERE / "known_hosts").read_text().splitlines() if ln and not ln.startswith("#")]
        self.assertEqual([(l[0], l[1]) for l in lines], [("10.100.0.1", "ssh-rsa")])


class TestGuards(unittest.TestCase):
    def test_needs_confirmation(self):
        r = s.guard({}, dry_run=False, now=at("12:00"))
        self.assertEqual(len(r), 1)
        self.assertIn("I-AM-HOME", r[0])
        self.assertEqual(s.guard({"S2500_FIRMWARE_CONFIRM": "yes"}, False, at("12:00"))[0][:22], r[0][:22])
        self.assertEqual(s.guard({"S2500_FIRMWARE_CONFIRM": "I-AM-HOME"}, False, at("12:00")), [])

    def test_never_in_the_backup_window(self):
        env = {"S2500_FIRMWARE_CONFIRM": "I-AM-HOME"}
        self.assertIn("backup window", s.guard(env, False, at("03:00"))[0])
        self.assertEqual(s.guard(env, False, at("04:00")), [])

    def test_dry_run_needs_nothing(self):
        self.assertEqual(s.guard({}, True, at("03:00")), [])

    def test_no_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(s.credentials(read=lambda p: None))
            self.assertIsNone(s.credentials(read=lambda p: {"host": "10.100.0.1"}))
            c = s.credentials(read=lambda p: {"username": "u", "password": "p"})
            self.assertEqual((c["host"], c["enable_secret"]), ("10.100.0.1", None))


def state(running="7.4.1.12_72393", boot=0, p0="7.4.1.12_72393", p1="7.4.1.11_71710"):
    parts = [{"partition": "0:0", "number": 0, "parsed": V(p0) if p0 else None, "default_boot": boot == 0},
             {"partition": "0:1", "number": 1, "parsed": V(p1) if p1 else None, "default_boot": boot == 1}]
    return {"running": V(running) if running else None, "model": "ArubaS2500-48P-US", "partitions": parts, "boot": boot}


def fw(version="7.4.1.13_73000", image="ArubaOS_MAS_7.4.1.13_73000", sha256=None):
    return {"model": "S2500", "version": version, "parsed": V(version), "image": image, "sha256": sha256}


class TestDecisions(unittest.TestCase):
    def test_plan(self):
        self.assertEqual(s.plan_upgrade(fw("7.4.1.12_72393"), state())["action"], "noop")
        p = s.plan_upgrade(fw(), state())
        self.assertEqual((p["action"], p["target"], p["old_partition"], p["copy"]), ("upgrade", 1, 0, True))
        # the other partition already holds the release (an earlier run copied it): no second copy
        p = s.plan_upgrade(fw(), state(p1="7.4.1.13_73000"))
        self.assertEqual((p["target"], p["copy"]), (1, False))
        p = s.plan_upgrade(fw(), state(boot=1, p0="7.4.1.11_71710", p1="7.4.1.12_72393"))
        self.assertEqual(p["target"], 0)

    def test_refusals(self):
        for st, why in ((state(running=None), "cannot read"),
                        (state(running="7.4.1.14_74000"), "newer"),
                        (state(boot=None), "boot partition"),
                        (state(p0="7.4.1.11_71710"), "does not hold")):
            p = s.plan_upgrade(fw(), st)
            self.assertEqual(p["action"], "refuse")
            self.assertIn(why, " ".join(p["reasons"]))

    def test_check_latest(self):
        with tempfile.TemporaryDirectory() as d:
            res = s.check_latest(fw("7.4.1.12_72393"), Path(d))
            self.assertFalse(res["newer"])
            self.assertIn("no anonymous release listing", res["vendor_error"])
            Path(d, "ArubaOS_MAS_7.4.1.13_73000").write_bytes(b"x")
            Path(d, "ArubaOS_MAS_7.4.1.11_71710").write_bytes(b"x")
            res = s.check_latest(fw("7.4.1.12_72393"), Path(d))
            self.assertEqual((res["newer"], res["latest"], len(res["staged"])), (True, "7.4.1.13_73000", 1))
        res = s.check_latest(fw("7.4.1.11_71710"), Path("/nonexistent"))
        self.assertEqual(res["latest"], s.LAST_KNOWN_RELEASE)


class FakeShell:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data.strip())

    def recv_ready(self):
        return False


class FakeSession:
    def __init__(self, sw):
        self.sw, self.shell = sw, FakeShell()

    def cmd(self, line, timeout=60, extra=None):
        self.sw.sent.append(line)
        if line == "show version":
            return f"ArubaOS (MODEL: ArubaS2500-48P-US), Version {self.sw.running.replace('_', ' (')})\n(sw) #"
        if line == "show image version":
            return fixture("show-image-version.txt")
        if line == "show boot":
            return fixture("show-boot.txt")
        return line + "\n(sw) #"

    def close(self):
        self.sw.sent.extend(self.shell.sent)


class FakeSwitch:
    def __init__(self, running="7.4.1.12_72393"):
        self.running, self.sent = running, []

    def connect(self, creds):
        return FakeSession(self)


class TestUpgrade(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        img = self.tmp / "ArubaOS_MAS_7.4.1.13_73000"
        img.write_bytes(b"image")
        self.fw = fw(sha256=s.sha256_file(img))

    def run_up(self, sw, dry_run, env, fw_=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = s.run_upgrade(fw_ or self.fw, {"host": "h"}, self.tmp, "192.0.2.2", dry_run, sw.connect, env=env,
                               sleep=lambda x: None, log=print, now=at("12:00"))
        return rc, buf.getvalue()

    def writes(self, sw):
        return [c for c in sw.sent if c.startswith(("copy", "boot system", "reload", "write"))]

    def test_refused_without_confirmation(self):
        sw = FakeSwitch()
        rc, out = self.run_up(sw, False, {})
        self.assertEqual((rc, sw.sent), (s.EXIT_REFUSED, []))
        self.assertIn("I-AM-HOME", out)

    def test_dry_run_sends_nothing(self):
        sw = FakeSwitch()
        rc, out = self.run_up(sw, True, {})
        self.assertEqual(rc, 0)
        self.assertEqual(self.writes(sw), [])
        self.assertIn("would send: copy tftp: 192.0.2.2 ArubaOS_MAS_7.4.1.13_73000 system: partition 1", out)
        self.assertIn("would send: boot system partition 1", out)

    def test_current_is_a_noop(self):
        sw = FakeSwitch()
        rc, out = self.run_up(sw, False, {"S2500_FIRMWARE_CONFIRM": "I-AM-HOME"}, fw_=fw("7.4.1.12_72393"))
        self.assertEqual((rc, self.writes(sw)), (0, []))
        self.assertIn("nothing to do", out)

    def test_image_hash_is_checked(self):
        sw = FakeSwitch()
        with self.assertRaises(s.S2500Error):
            self.run_up(sw, True, {}, fw_=fw(sha256="0" * 64))
        self.assertEqual(self.writes(sw), [])


if __name__ == "__main__":
    unittest.main()
