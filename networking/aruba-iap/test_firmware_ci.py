#!/usr/bin/env python3
"""Offline tests for the Aruba Instant firmware_ci.py (no GitLab, no APs, no network)."""
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import firmware_ci as fc  # noqa: E402
import firmware_night  # noqa: E402
import iapfw  # noqa: E402


class FakeGitLab:
    def __init__(self, gets=None):
        self.gets = gets or {}
        self.writes = []

    def get(self, path, **params):
        for k, v in self.gets.items():
            if path.endswith(k):
                if isinstance(v, Exception):
                    raise v
                return v
        return []

    def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if path.endswith("/issues"):
            return {"iid": 7, "web_url": "https://gitlab.example/issues/7"}
        if path.endswith("/merge_requests"):
            return {"iid": 9, "web_url": "https://gitlab.example/mr/9"}
        return {}


def fake_am(gl):
    mails = []
    return types.SimpleNamespace(PROJECT="4", gitlab_from_env=lambda dry_run: gl,
                                 mail_root=lambda s, b: mails.append((s, b)) or True), mails


def ci_yaml():
    import yaml

    class Loader(yaml.SafeLoader):
        pass
    Loader.add_constructor("!reference", lambda loader, node: None)
    return yaml.load((HERE.parents[1] / ".gitlab-ci.yml").read_text(), Loader=Loader)


class TestPlan(unittest.TestCase):
    def test_scheduled_run_uses_the_window(self):
        p = fc.rollout_plan({"CI_PIPELINE_SOURCE": "schedule", "IAP_TFTP_SERVER": "192.0.2.1"})
        self.assertEqual(p, {"window": fc.DEFAULT_WINDOW, "scheduled": True, "dry_run": False, "tftp": "192.0.2.1"})

    def test_started_by_hand_runs_now(self):
        p = fc.rollout_plan({"CI_PIPELINE_SOURCE": "api", "IAP_FIRMWARE_DRY_RUN": "1"})
        self.assertEqual((p["window"], p["scheduled"], p["dry_run"]), (None, False, True))

    def test_bad_window(self):
        with self.assertRaises(iapfw.FwError):
            fc.rollout_plan({"CI_PIPELINE_SOURCE": "schedule", "IAP_FIRMWARE_WINDOW": "dawn"})

    def test_ci_jobs(self):
        ci = ci_yaml()
        job = ci["aruba-iap:firmware"]
        env = job["variables"]
        self.assertEqual(env["IAP_FIRMWARE_WINDOW"], fc.DEFAULT_WINDOW)
        self.assertRegex(env["TZ"], r"^EST5EDT,M3\.2\.0,M11\.1\.0$")   # the window is local time
        self.assertEqual(env["IAP_TFTP_SERVER"], "10.4.0.1")
        self.assertEqual(job["image"]["docker"]["user"], "root")         # binds UDP 69
        self.assertIn("IAP_TASK == \"firmware\"", job["rules"][0]["if"])
        self.assertIn("IAP_TASK == \"firmware-check\"", ci["aruba-iap:firmware-check"]["rules"][0]["if"])
        # neither the daily drift check nor the apply job runs in a firmware pipeline
        self.assertIn('IAP_TASK == "drift"', ci["aruba-iap:drift"]["rules"][0]["if"])

    def test_window_stays_clear_of_the_backup_window(self):
        a, b = fc.DEFAULT_WINDOW.split("-")
        self.assertGreaterEqual(a, "04:00")
        self.assertLess(a, b)


class TestBumpMr(unittest.TestCase):
    def run_bump(self, res, gets=None):
        gl = FakeGitLab(gets)
        am, mails = fake_am(gl)
        new = {"version": "8.13.1.1_94000", "image": "ArubaInstant_Draco_8.13.1.1_94000", "sha256": "c" * 64}
        bumped = []
        with mock.patch.object(fc, "_am", return_value=am), \
                mock.patch.object(iapfw, "check_latest", return_value=res), \
                mock.patch.object(iapfw, "bump", side_effect=lambda *a, **k: bumped.append(k) or {"old": "x", "new": new}), \
                redirect_stdout(io.StringIO()):
            rc = fc.cmd_bump_mr(types.SimpleNamespace(dry_run=False, image_dir="/nonexistent"))
        return rc, gl, mails, bumped

    def result(self, **kw):
        base = {"model": "AP-515", "declared": "8.13.1.0_93688", "branch": "8.13", "image_dir": "/d", "vendor": None,
                "vendor_error": "no portal credentials", "staged": [], "latest": "8.13.1.0_93688", "newer": False,
                "source": None}
        return {**base, **kw}

    def test_cannot_check_is_a_warning(self):
        rc, gl, mails, bumped = self.run_bump(self.result())
        self.assertEqual((rc, gl.writes, mails, bumped), (iapfw.EXIT_CANNOT_CHECK, [], [], []))

    def test_staged_image_opens_one_mr(self):
        res = self.result(newer=True, latest="8.13.1.1_94000", source="staged",
                          image="/d/ArubaInstant_Draco_8.13.1.1_94000",
                          staged=[{"version": "8.13.1.1_94000", "path": "/d/ArubaInstant_Draco_8.13.1.1_94000"}])
        rc, gl, mails, bumped = self.run_bump(res, gets={"/branches/aruba-iap-firmware%2F8.13.1.1_94000":
                                                         RuntimeError("404")})
        self.assertEqual(rc, 0)
        commit = [w for w in gl.writes if w[1].endswith("/repository/commits")][0][2]
        self.assertEqual(commit["branch"], "aruba-iap-firmware/8.13.1.1_94000")
        self.assertEqual(sorted(a["file_path"] for a in commit["actions"]),
                         ["ci/reports/firmware.py", "networking/aruba-iap/firmware.yml"])
        mr = [w for w in gl.writes if w[1].endswith("/merge_requests")][0][2]
        self.assertEqual(mr["labels"], fc.LABEL)
        for s in ("both APs at once", "5-10 minutes", iapfw.ROUTER_IMAGE_DIR, "c" * 64):
            self.assertIn(s, mr["description"])
        self.assertEqual(len(mails), 1)

    def test_existing_mr_is_not_duplicated(self):
        res = self.result(newer=True, latest="8.13.1.1_94000", source="staged", image="/d/x")
        rc, gl, mails, bumped = self.run_bump(res, gets={"/merge_requests": [{"iid": 3, "state": "closed",
                                                                            "web_url": "u"}]})
        self.assertEqual((rc, gl.writes, mails, bumped), (0, [], [], []))


class TestRollout(unittest.TestCase):
    def run_rollout(self, rc, output, env=None, issues=None, night=None, now_in_window=True):
        gl = FakeGitLab({"/issues": issues or []})
        am, mails = fake_am(gl)
        script = f"import sys; sys.stdout.write({output!r}); sys.exit({rc})"
        real_popen = fc.subprocess.Popen
        seen = []

        def popen(cmd, **kw):
            seen.append(cmd)
            return real_popen([sys.executable, "-c", script], **kw)
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(env or {"CI_PIPELINE_SOURCE": "schedule"}, FIRMWARE_NIGHT_DIR=night or os.path.join(tmp, "n"))
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(fc, "_am", return_value=am), mock.patch.object(fc.subprocess, "Popen", popen), \
                    mock.patch.object(fc, "in_window", return_value=now_in_window), redirect_stdout(io.StringIO()) as out:
                cwd = os.getcwd()
                os.chdir(tmp)
                try:
                    got = fc.cmd_rollout(types.SimpleNamespace(image_dir="/c"))
                    alerted = Path(fc.ALERTED).exists()
                finally:
                    os.chdir(cwd)
        return got, gl, mails, seen, alerted, out.getvalue()

    def test_open_issue_pauses_rollouts(self):
        rc, gl, mails, seen, _, _ = self.run_rollout(0, "", issues=[{"iid": 5, "title": "t", "web_url": "u"}])
        self.assertEqual((rc, seen, gl.writes, mails), (0, [], [], []))

    def test_outside_the_window_does_nothing(self):
        rc, gl, mails, seen, _, out = self.run_rollout(0, "", now_in_window=False)
        self.assertEqual((rc, seen), (0, []))
        self.assertIn("outside the maintenance window", out)

    def test_command_line(self):
        _, _, _, seen, _, _ = self.run_rollout(0, "", env={"CI_PIPELINE_SOURCE": "schedule",
                                                           "IAP_TFTP_SERVER": "192.0.2.1"})
        cmd = seen[0]
        self.assertEqual(cmd[2:5], [str(HERE / "iapctl.py"), "firmware", "upgrade"])
        self.assertIn("192.0.2.1", cmd)
        self.assertNotIn("--dry-run", cmd)

    def test_failure_opens_issue_mails_and_claims_the_night(self):
        with tempfile.TemporaryDirectory() as night:
            out = ("[  10s]    upgrade started; watching show upgrade info\n"
                   "!! every AP came back running its old release: the new image did not take. DID NOT UPGRADE.\n")
            rc, gl, mails, _, alerted, _ = self.run_rollout(1, out, night=night)
            self.assertEqual(rc, 1)
            issue = [w for w in gl.writes if w[1].endswith("/issues")][0][2]
            self.assertIn(fc.LABEL_FAILED, issue["labels"])
            self.assertIn("DID NOT UPGRADE", issue["description"])
            self.assertEqual(len(mails), 1)
            self.assertIn("FAILED", mails[0][0])
            self.assertTrue(alerted)
            self.assertEqual(json.loads((Path(night) / "last.json").read_text())["family"], "aruba-iap")

    def test_refusal_before_touching_the_aps_leaves_the_night_free(self):
        with tempfile.TemporaryDirectory() as night:
            rc, gl, mails, _, _, _ = self.run_rollout(1, "ERROR: image ArubaInstant_x is not staged\n", night=night)
            self.assertEqual(rc, 1)
            self.assertEqual(len(mails), 1)
            self.assertFalse((Path(night) / "last.json").exists())

    def test_success_mails_only_when_upgraded(self):
        rc, gl, mails, _, _, _ = self.run_rollout(0, "== already running 8.13.1.0_93688 on all 2 APs; nothing to do\n")
        self.assertEqual((rc, gl.writes, mails), (0, [], []))
        rc, gl, mails, _, _, _ = self.run_rollout(0, "== upgraded 2 APs to 8.13.1.1_94000 (Wi-Fi outage about 6 min)\n")
        self.assertEqual((rc, len(mails)), (0, 1))

    def test_dry_run_neither_opens_issues_nor_mails_nor_locks(self):
        with tempfile.TemporaryDirectory() as night:
            with firmware_night.NightLock("netgear", False, directory=night):   # a NETGEAR rollout is running
                rc, gl, mails, seen, _, _ = self.run_rollout(1, "ERROR: x\n", env={"IAP_FIRMWARE_DRY_RUN": "1"},
                                                             night=night)
        self.assertEqual((rc, gl.writes, mails), (1, [], []))
        self.assertIn("--dry-run", seen[0])

    def test_no_rollout_on_a_night_netgear_used(self):
        with tempfile.TemporaryDirectory() as night:
            with firmware_night.NightLock("netgear", False, directory=night) as lk:
                lk.used("upgraded")
            rc, gl, mails, seen, _, out = self.run_rollout(0, "", night=night)
            self.assertEqual((rc, seen, gl.writes, mails), (0, [], [], []))
            self.assertIn("one rollout per night", out)

    def test_never_alongside_a_running_rollout(self):
        with tempfile.TemporaryDirectory() as night, firmware_night.NightLock("netgear", False, directory=night):
            rc, _, _, seen, _, out = self.run_rollout(0, "", env={"CI_PIPELINE_SOURCE": "web"}, night=night)
            self.assertEqual((rc, seen), (0, []))
            self.assertIn("another firmware rollout is running", out)


class TestNight(unittest.TestCase):
    def test_blocking_use(self):
        b = firmware_night.blocking_use
        self.assertIsNone(b(None, "aruba-iap", "2026-11-06"))
        self.assertIsNone(b({"family": "netgear", "night": "2026-11-05"}, "aruba-iap", "2026-11-06"))
        self.assertIsNone(b({"family": "aruba-iap", "night": "2026-11-06"}, "aruba-iap", "2026-11-06"))
        self.assertIn("netgear", b({"family": "netgear", "night": "2026-11-06"}, "aruba-iap", "2026-11-06"))

    def test_night_is_the_local_date(self):
        self.assertEqual(firmware_night.night(time.strptime("2026-11-06 05:10", "%Y-%m-%d %H:%M")), "2026-11-06")


if __name__ == "__main__":
    unittest.main()
