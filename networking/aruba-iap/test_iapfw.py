#!/usr/bin/env python3
"""Offline tests for iapfw.py (Instant firmware): parsers against captured CLI
output in testdata/, firmware.yml, release finding, bump, the upgrade decisions
and a simulated cluster for `upgrade` (dry run, success, fallback, rollback)."""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import iapfw as f  # noqa: E402

TESTDATA = HERE / "testdata"
V = f.parse_version


def fixture(name: str) -> str:
    return (TESTDATA / name).read_text()


def fw_for(version: str, sha256=None, aps=2) -> dict:
    v = V(version)
    return f.validate_firmware({"model": "AP-515", "image_class": "Draco", "branch": v.branch, "version": version,
                                "image": f.image_name("Draco", v), "sha256": sha256, "aps": aps})


class TestVersions(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(V("8.13.1.0_93688 LSR (Digitally Signed - Production Build)"),
                         f.Version("8.13.1.0", 93688, "LSR"))
        self.assertEqual(V("AOS-8 (MODEL: 515), Version 8.13.1.0 LSR"), f.Version("8.13.1.0", None, "LSR"))
        self.assertEqual(V("8.9.0.3_83448 (Digitally Signed - Production Build)"), f.Version("8.9.0.3", 83448))
        self.assertIsNone(V("no version here"))
        self.assertEqual(str(V("8.13.1.0_93688 LSR")), "8.13.1.0_93688")
        self.assertEqual(V("8.13.1.0_93688").branch, "8.13")

    def test_order_and_identity(self):
        self.assertLess(V("8.9.0.3_83448").key, V("8.13.1.0_93688").key)   # numeric, not text order
        self.assertLess(V("8.13.1.0_93688").key, V("8.13.1.1_94000").key)
        self.assertTrue(V("8.13.1.0 LSR").same_release(V("8.13.1.0_93688")))  # show version has no build
        self.assertFalse(V("8.13.1.0_93688").same_release(V("8.13.1.0_93690")))

    def test_image_names(self):
        self.assertEqual(f.parse_image_name("ArubaInstant_Draco_8.13.1.1_94000"), ("Draco", V("8.13.1.1_94000")))
        self.assertEqual(f.parse_image_name("/x/ArubaInstant_Hercules_8.10.0.6_88888")[0], "Hercules")
        for bad in ("ArubaInstant_Draco_8.13.1.1", "ArubaInstant_Draco_8.13.1.1_94000.zip", "Draco_8.13.1.1_9"):
            self.assertIsNone(f.parse_image_name(bad), bad)
        self.assertEqual(f.image_name("Draco", V("8.13.1.1_94000")), "ArubaInstant_Draco_8.13.1.1_94000")


class TestParsers(unittest.TestCase):
    def test_show_image_version(self):
        img = f.parse_image_version(fixture("show-image-version.txt"))
        self.assertEqual((str(img["running"]), img["running"].train, str(img["backup"])),
                         ("8.13.1.0_93688", "LSR", "8.9.0.3_83448"))
        self.assertEqual((img["primary_partition"], img["backup_partition"], img["classes"]), ("0", "1", ["Draco"]))

    def test_show_upgrade_info(self):
        u = f.parse_upgrade_info(fixture("show-upgrade-info.txt"))
        self.assertEqual([(a["ip"], a["class"], a["status"], a["image_info"], a["error"]) for a in u["aps"]],
                         [("10.4.128.11", "Draco", "image-ok", "From Seed", "none"),
                          ("10.4.128.12", "Draco", "image-ok", "From Seed", "none")])
        self.assertFalse(u["in_process"])
        self.assertTrue(u["auto_reboot"])
        self.assertEqual(u["settings"]["Switch Partition"], "enable")
        self.assertEqual(f.upgrade_failures(u), [])

    def test_show_aps(self):
        aps = f.parse_aps(fixture("show-aps.txt"))
        self.assertEqual([(a["name"], a["ip"], a["conductor"], a["mode"]) for a in aps],
                         [("movie-ap", "10.4.128.11", True, "access"), ("office-ap", "10.4.128.12", False, "access")])

    def test_failures_are_seen(self):
        text = fixture("show-upgrade-info.txt").replace("image-ok  From Seed   none\nAuto",
                                                        "image-ok  From Seed   tftp timeout\nAuto")
        self.assertEqual(len(f.upgrade_failures(f.parse_upgrade_info(text))), 1)

    def test_garbage(self):
        with self.assertRaises(f.FwError):
            f.parse_image_version("% Parse error\n")
        with self.assertRaises(f.FwError):
            f.parse_aps("nothing\n")


class TestFirmwareFile(unittest.TestCase):
    def test_repo_file(self):
        fw = f.load_firmware()
        self.assertEqual((fw["model"], fw["image_class"], fw["branch"], fw["aps"]), ("AP-515", "Draco", "8.13", 2))
        self.assertEqual(fw["image"], f.image_name("Draco", fw["parsed"]))

    def test_validation(self):
        ok = {"model": "AP-515", "image_class": "Draco", "branch": "8.13", "version": "8.13.1.0_93688",
              "image": "ArubaInstant_Draco_8.13.1.0_93688", "sha256": None, "aps": 2}
        f.validate_firmware(ok)
        for change in ({"version": "8.13.1.0"}, {"branch": "8.12"}, {"image": "ArubaInstant_Hercules_8.13.1.0_93688"},
                       {"image": "ArubaInstant_Draco_8.13.1.1_93688"}, {"sha256": "ABC"}, {"aps": 0}):
            with self.assertRaises(f.FwError, msg=change):
                f.validate_firmware({**ok, **change})

    def test_replace_keeps_layout(self):
        text = (HERE / "firmware.yml").read_text()
        new = f.replace_firmware(text, {"version": "8.13.1.1_94000", "image": "ArubaInstant_Draco_8.13.1.1_94000",
                                        "sha256": "a" * 64})
        self.assertIn('version: "8.13.1.1_94000"\n', new)
        self.assertIn('sha256: "' + "a" * 64 + '"\n', new)
        self.assertEqual([ln for ln in text.splitlines() if ln.startswith("#")],
                         [ln for ln in new.splitlines() if ln.startswith("#")])


class TestFindingReleases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def stage(self, name, size=9 << 20):
        p = self.tmp / name
        with open(p, "wb") as fh:
            fh.truncate(size)
        return p

    def test_vendor_cannot_be_checked_without_credentials(self):
        res = f.check_latest(fw_for("8.13.1.0_93688"), self.tmp, lambda path: None)
        self.assertFalse(res["newer"])
        self.assertIn("no portal credentials", res["vendor_error"])
        self.assertIn("CANNOT CHECK", "\n".join(f.format_check(res)))

    def test_credentials_do_not_make_up_a_result(self):
        res = f.check_latest(fw_for("8.13.1.0_93688"), self.tmp, lambda path: {"username": "u", "password": "p"})
        self.assertIn("not implemented", res["vendor_error"])
        self.assertFalse(res["newer"])

    def test_staged_images_on_the_branch_only(self):
        for n in ("ArubaInstant_Draco_8.13.1.1_94000", "ArubaInstant_Draco_8.13.2.0_95000",
                  "ArubaInstant_Draco_8.14.0.0_96000",        # other branch
                  "ArubaInstant_Hercules_8.13.1.5_94500",     # other AP class
                  "ArubaInstant_Draco_8.13.0.9_92000",        # older
                  "notes.txt"):
            self.stage(n)
        res = f.check_latest(fw_for("8.13.1.0_93688"), self.tmp, lambda path: None)
        self.assertEqual([s["version"] for s in res["staged"]], ["8.13.2.0_95000", "8.13.1.1_94000"])
        self.assertEqual((res["newer"], res["latest"], res["source"]), (True, "8.13.2.0_95000", "staged"))


class TestBump(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fw = self.tmp / "firmware.yml"
        shutil.copy(HERE / "firmware.yml", self.fw)
        self.kl = self.tmp / "firmware.py"
        shutil.copy(f.KNOWN_LATEST_PATH, self.kl)

    def image(self, name, content=b"\x01"):
        p = self.tmp / name
        with open(p, "wb") as fh:
            fh.write(content)
            fh.truncate(9 << 20)
        return p

    def test_bump_from_image(self):
        img = self.image("ArubaInstant_Draco_8.13.1.1_94000")
        with redirect_stdout(io.StringIO()):
            out = f.bump(self.fw, image=img, known_latest=self.kl, log=print)
        got = f.load_firmware(self.fw)
        self.assertEqual((got["version"], got["image"], got["sha256"]),
                         ("8.13.1.1_94000", "ArubaInstant_Draco_8.13.1.1_94000", f.sha256_file(img)))
        self.assertEqual(out["old"], "8.13.1.0_93688")
        self.assertRegex(self.kl.read_text(), r'"AP-515": \("8\.13\.1\.1", "\d{4}-\d\d-\d\d"\),')

    def test_bump_from_portal_page(self):
        f.bump(self.fw, version="8.13.1.1_94000", sha256="B" * 64, known_latest=None, log=lambda m: None)
        self.assertEqual(f.load_firmware(self.fw)["sha256"], "b" * 64)

    def test_refusals(self):
        cases = [
            dict(image=self.image("ArubaInstant_Hercules_8.13.1.1_94000")),   # other class
            dict(image=self.image("ArubaInstant_Draco_8.14.0.0_96000")),      # other branch
            dict(image=self.image("ArubaInstant_Draco_8.13.1.0_93688")),      # not newer
            dict(image=self.image("ArubaInstant_Draco_8.13.1.1_94000"), sha256="0" * 64),  # hash differs
            dict(version="8.13.1.1", sha256="a" * 64),                        # no build
            dict(version="8.13.1.1_94000"),                                   # no hash
        ]
        before = self.fw.read_text()
        for kw in cases:
            with self.assertRaises(f.FwError, msg=kw):
                f.bump(self.fw, known_latest=None, log=lambda m: None, **kw)
        self.assertEqual(self.fw.read_text(), before)

    def test_tiny_file_is_not_an_image(self):
        p = self.tmp / "ArubaInstant_Draco_8.13.1.1_94000"
        p.write_bytes(b"<html>login</html>")
        with self.assertRaises(f.FwError):
            f.bump(self.fw, image=p, known_latest=None, log=lambda m: None)


# --------------------------------------------------------------------------- simulated cluster

def render_aps(aps):
    rows = [f"{'Name':<11}{'IP Address':<14}{'Mode':<8}{'Type':<13}", f"{'----':<11}{'----------':<14}{'----':<8}{'----':<13}"]
    for i, a in enumerate(aps):
        rows.append(f"{a['name']:<11}{a['ip'] + ('*' if i == 0 else ''):<14}{'access':<8}{'515(indoor)':<13}")
    return "show aps\n\n%d Access Points\n---------------\n%s\nmovie-ap# " % (len(aps), "\n".join(rows))


def render_upgrade(aps, status="image-ok", in_process="No", auto="enable"):
    head = (f"{'Mac':<19}{'IP Address':<13}{'AP Class':<10}{'Status':<16}{'Error Detail':<12}\n"
            f"{'---':<19}{'----------':<13}{'--------':<10}{'------':<16}{'------------':<12}")
    rows = [f"{'02:00:00:00:00:0%d' % (i + 1):<19}{a['ip']:<13}{'Draco':<10}{status:<16}{'none':<12}"
            for i, a in enumerate(aps)]
    return ("show upgrade info\n\nswarm upgrade status\n--------------------\n%s\n%s\n"
            "Auto reboot           :%s\nUpgrade in process    :%s\nmovie-ap# " % (head, "\n".join(rows), auto, in_process))


def render_image(ap):
    return ("show image version\n\nPrimary Partition                 :0\n"
            f"Primary Partition Build Version   :{ap['running']} LSR (Digitally Signed - Production Build)\n"
            "Backup Partition                  :1\n"
            f"Backup Partition Build Version    :{ap['backup']} (Digitally Signed - Production Build)\n"
            "AP Images Classes\n-----------------\nClass\n-----\nDraco\nmovie-ap# ")


class FakeCluster:
    """Two APs behind a virtual controller. `outcome` decides what the reboot does."""
    VC = "10.4.128.10"

    def __init__(self, outcome="ok", new="8.13.1.1_94000", down_polls=2):
        self.aps = [{"name": "movie-ap", "ip": "10.4.128.11", "running": "8.13.1.0_93688", "backup": "8.9.0.3_83448"},
                    {"name": "office-ap", "ip": "10.4.128.12", "running": "8.13.1.0_93688", "backup": "8.9.0.3_83448"}]
        self.outcome, self.new, self.down_polls = outcome, new, down_polls
        self.sent, self.upgrade_url, self.rebooting, self.down_left = [], None, False, 0
        self.status, self.in_process = "image-ok", "No"

    def connect(self, host, user, pw):
        if self.down_left > 0:
            self.down_left -= 1
            raise OSError("connection refused")
        return FakeSession(self, host)

    def reboot(self):
        for a in self.aps:
            if self.outcome == "ok" or (self.outcome == "mixed" and a["name"] == "movie-ap"):
                a["backup"], a["running"] = a["running"], self.new
        self.down_left = self.down_polls

    def switch_partition(self, ip):
        a = next(x for x in self.aps if x["ip"] == ip)
        a["running"], a["backup"] = a["backup"], a["running"]
        self.down_left = 1


class FakeShell:
    def __init__(self, sess):
        self.sess, self.out = sess, ""

    def send(self, data):
        line = data.strip()
        c = self.sess.cluster
        c.sent.append((self.sess.host, line))
        if line.startswith("upgrade-image "):
            c.upgrade_url = line.split(None, 1)[1]
            c.status, c.in_process = "image-downloading", "Yes"
        elif line == "switch-partition-reboot":
            c.switch_partition(self.sess.host)
        self.out = line + "\nmovie-ap# "

    def recv_ready(self):
        return bool(self.out)

    def recv(self, n):
        out, self.out = self.out, ""
        return out.encode()


class FakeSession:
    def __init__(self, cluster, host):
        self.cluster, self.host = cluster, host
        self.shell = FakeShell(self)

    def cmd(self, line, timeout=120):
        c = self.cluster
        c.sent.append((self.host, line))
        if line == "show aps":
            return render_aps(c.aps)
        if line == "show upgrade info":
            if c.upgrade_url and not c.rebooting:
                c.rebooting = True
                return render_upgrade(c.aps, "image-ok", "Yes")
            if c.rebooting and c.upgrade_url and c.status != "done":
                c.status = "done"
                c.reboot()
                raise OSError("session closed")   # the controller reboots
            return render_upgrade(c.aps, "image-ok", "No")
        if line == "show image version":
            return render_image(next(a for a in c.aps if a["ip"] == self.host))
        raise AssertionError(f"unexpected command {line!r}")

    def close(self):
        pass


class FakeTftp:
    sent = 0

    def __init__(self, addr, path):
        self.addr, self.path = addr, path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestDecisions(unittest.TestCase):
    def state(self, versions=("8.13.1.0_93688", "8.13.1.0_93688"), in_process=False, auto=True, classes="Draco"):
        aps = [{"name": f"ap{i}", "ip": f"10.0.0.{i}", "conductor": i == 0, "running": V(v) if v else None,
                "backup": V("8.9.0.3_83448"), "image_class": classes} for i, v in enumerate(versions)]
        return {"aps": aps, "upgrade": {"aps": [], "in_process": in_process, "auto_reboot": auto}}

    def test_noop_when_current(self):
        self.assertEqual(f.plan_upgrade(fw_for("8.13.1.0_93688"), self.state())["action"], "noop")

    def test_upgrade_when_behind(self):
        p = f.plan_upgrade(fw_for("8.13.1.1_94000"), self.state())
        self.assertEqual((p["action"], p["behind"]), ("upgrade", ["ap0", "ap1"]))
        # half done (an earlier run): still upgrade the cluster
        p = f.plan_upgrade(fw_for("8.13.1.1_94000"), self.state(("8.13.1.1_94000", "8.13.1.0_93688")))
        self.assertEqual((p["action"], p["behind"]), ("upgrade", ["ap1"]))

    def test_refusals(self):
        fw = fw_for("8.13.1.1_94000")
        for st, why in ((self.state(("8.13.1.0_93688",)), "expects 2"),
                        (self.state(("8.13.1.0_93688", None)), "cannot read"),
                        (self.state(("8.13.2.0_95000", "8.13.1.0_93688")), "not downgrading"),
                        (self.state(in_process=True), "already in process"),
                        (self.state(auto=False), "auto reboot"),
                        (self.state(classes="Hercules"), "class")):
            p = f.plan_upgrade(fw, st)
            self.assertEqual(p["action"], "refuse")
            self.assertIn(why, " ".join(p["reasons"]))

    def test_after_upgrade(self):
        fw = fw_for("8.13.1.1_94000")
        before = self.state()

        def after(*vers, backup="8.13.1.0_93688"):
            st = self.state(vers)
            for a in st["aps"]:
                a["backup"] = V(backup)
            return st
        self.assertEqual(f.after_upgrade(fw, before, after("8.13.1.1_94000", "8.13.1.1_94000"))["result"], "ok")
        self.assertEqual(f.after_upgrade(fw, before, after("8.13.1.0_93688", "8.13.1.0_93688"))["result"], "fell-back")
        v = f.after_upgrade(fw, before, after("8.13.1.1_94000", "8.13.1.0_93688"))
        self.assertEqual((v["result"], v["rollback"]), ("mixed", ["ap0"]))
        v = f.after_upgrade(fw, before, after("8.13.1.1_94000", "8.13.1.0_93688", backup="8.9.0.3_83448"))
        self.assertEqual(v["rollback"], [])   # old release no longer on the backup partition
        self.assertEqual(f.after_upgrade(fw, before, after("8.13.1.1_94000"))["result"], "missing")


class TestUpgrade(unittest.TestCase):
    NEW = "8.13.1.1_94000"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        img = self.tmp / f.image_name("Draco", V(self.NEW))
        with open(img, "wb") as fh:
            fh.write(b"image")
            fh.truncate(9 << 20)
        self.fw = fw_for(self.NEW, sha256=f.sha256_file(img))
        self.drift_calls = 0

    def drift(self, rc=0):
        def fn():
            self.drift_calls += 1
            return rc
        return fn

    def run_up(self, cluster, dry_run=False, drift_rc=0, fw=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = f.run_upgrade(fw or self.fw, self.tmp, "192.0.2.1", dry_run, (FakeCluster.VC, "u", "p"),
                               cluster.connect, self.drift(drift_rc),
                               timing=f.Timing(download=10, down=5, boot=10, settle=1, poll=0),
                               tftp_factory=FakeTftp, sleep=lambda s: None, log=print)
        return rc, buf.getvalue()

    def upgrades_sent(self, cluster):
        return [s for s in cluster.sent if s[1].startswith(("upgrade-image", "switch-partition"))]

    def test_dry_run_sends_nothing(self):
        c = FakeCluster()
        rc, out = self.run_up(c, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.upgrades_sent(c), [])
        self.assertIn("upgrade-image tftp://192.0.2.1/ArubaInstant_Draco_8.13.1.1_94000", out)
        self.assertIn("dry run", out)
        self.assertEqual(self.drift_calls, 1)

    def test_current_cluster_is_a_noop(self):
        c = FakeCluster()
        rc, out = self.run_up(c, fw=fw_for("8.13.1.0_93688"))
        self.assertEqual((rc, self.upgrades_sent(c), self.drift_calls), (0, [], 0))
        self.assertIn("nothing to do", out)

    def test_drift_blocks_the_upgrade(self):
        c = FakeCluster()
        rc, out = self.run_up(c, drift_rc=2)
        self.assertEqual((rc, self.upgrades_sent(c)), (1, []))

    def test_image_must_match_its_hash(self):
        c = FakeCluster()
        with self.assertRaises(f.FwError):
            self.run_up(c, fw=fw_for(self.NEW, sha256="0" * 64))
        os.remove(self.tmp / f.image_name("Draco", V(self.NEW)))
        with self.assertRaises(f.FwError) as e:
            self.run_up(c)
        self.assertIn("not staged", str(e.exception))
        self.assertEqual(self.upgrades_sent(c), [])

    def test_success(self):
        c = FakeCluster("ok")
        rc, out = self.run_up(c)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.upgrades_sent(c), [(FakeCluster.VC, "upgrade-image tftp://192.0.2.1/"
                                                  "ArubaInstant_Draco_8.13.1.1_94000")])
        self.assertIn("== upgraded 2 APs to 8.13.1.1_94000", out)
        self.assertEqual(self.drift_calls, 2)   # before and after
        # re-running is safe: nothing more is sent
        rc, out = self.run_up(c)
        self.assertEqual((rc, len(self.upgrades_sent(c))), (0, 1))

    def test_drift_after_upgrade_keeps_the_release(self):
        c = FakeCluster("ok")
        calls = iter([0, 2])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = f.run_upgrade(self.fw, self.tmp, "192.0.2.1", False, (FakeCluster.VC, "u", "p"), c.connect,
                               lambda: next(calls), timing=f.Timing(10, 5, 10, 10, 0), tftp_factory=FakeTftp,
                               sleep=lambda s: None, log=print)
        self.assertEqual(rc, f.EXIT_DRIFT)
        self.assertNotIn("switch-partition", str(c.sent))

    def test_image_did_not_take(self):
        c = FakeCluster("fallback")
        rc, out = self.run_up(c)
        self.assertEqual(rc, 1)
        self.assertIn("DID NOT UPGRADE", out)
        self.assertNotIn("switch-partition", str(c.sent))

    def test_mixed_versions_roll_back(self):
        c = FakeCluster("mixed")
        rc, out = self.run_up(c)
        self.assertEqual(rc, 1)
        self.assertIn(("10.4.128.11", "switch-partition-reboot"), c.sent)
        self.assertEqual([a["running"] for a in c.aps], ["8.13.1.0_93688", "8.13.1.0_93688"])
        self.assertIn("rolled back", out)


if __name__ == "__main__":
    unittest.main()
