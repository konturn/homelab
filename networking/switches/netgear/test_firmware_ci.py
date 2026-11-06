#!/usr/bin/env python3
"""Offline tests for firmware_ci.py (no GitLab, no switch, no network)."""
import io
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
import ngswitch as ng  # noqa: E402

NEW = {"version": "6.7.0.60", "url": "https://www.downloads.netgear.com/files/GDC/MS510TX/MS510TX_TXPP_V6.7.0.60.zip",
       "sha256": "a" * 64, "image": "MS510TX_TXPP_V6.7.0.60.bin", "image_sha256": "b" * 64}


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
    am = types.SimpleNamespace(PROJECT="4", gitlab_from_env=lambda dry_run: gl,
                               mail_root=lambda s, b: mails.append((s, b)) or True)
    return am, mails


class TestPlan(unittest.TestCase):
    def test_scheduled_run_uses_window_and_both_switches_in_order(self):
        p = fc.rollout_plan({"CI_PIPELINE_SOURCE": "schedule", "NETGEAR_TFTP_SERVER": "192.0.2.1"})
        self.assertEqual(p, {"targets": ["netgear-movie", "netgear-office"], "window": fc.DEFAULT_WINDOW,
                             "dry_run": False, "tftp": "192.0.2.1"})

    def test_started_by_hand_runs_now(self):
        p = fc.rollout_plan({"CI_PIPELINE_SOURCE": "api", "NETGEAR_FIRMWARE_TARGET": "netgear-office",
                             "NETGEAR_FIRMWARE_DRY_RUN": "1"})
        self.assertEqual((p["targets"], p["window"], p["dry_run"]), (["netgear-office"], None, True))

    def test_bad_input(self):
        with self.assertRaises(ng.NgError):
            fc.rollout_plan({"NETGEAR_FIRMWARE_TARGET": "netgear-garage"})
        with self.assertRaises(ng.NgError):
            fc.rollout_plan({"CI_PIPELINE_SOURCE": "schedule", "NETGEAR_FIRMWARE_WINDOW": "dawn"})

    def test_window_stays_clear_of_the_backup_window(self):
        a, b = fc.DEFAULT_WINDOW.split("-")
        self.assertGreaterEqual(a, "04:00")  # backups run 02:15-04:00
        self.assertLess(a, b)

    def test_ci_job_sets_a_local_time_zone(self):
        # The window is evaluated in local time; without TZ the job container runs in UTC.
        import yaml

        class Loader(yaml.SafeLoader):
            pass
        Loader.add_constructor("!reference", lambda loader, node: None)
        ci = yaml.load((HERE.parents[2] / ".gitlab-ci.yml").read_text(), Loader=Loader)
        env = ci["netgear:firmware"]["variables"]
        self.assertEqual(env["NETGEAR_FIRMWARE_WINDOW"], fc.DEFAULT_WINDOW)
        tz = env.get("TZ", "")
        self.assertRegex(tz, r"^EST5EDT,M3\.2\.0,M11\.1\.0$")
        os.environ["TZ"] = tz
        try:
            time.tzset()
            import calendar
            t = time.localtime(calendar.timegm((2026, 11, 6, 5, 0, 0)))  # 05:00 UTC = 00:00 EST
            self.assertEqual((t.tm_hour, t.tm_min), (0, 0))
        finally:
            del os.environ["TZ"]
            time.tzset()

    def test_order(self):
        self.assertEqual(fc.SWITCH_ORDER[0], "netgear-movie")
        for f in fc.desired_files():
            self.assertTrue(f.exists(), f)


class TestBumpMr(unittest.TestCase):
    def test_description(self):
        old = ng.load_desired(HERE / "netgear-movie.yml")["firmware"]
        d = fc.mr_description("MS510TXPP", old, NEW, "Bug Fixes\n- one", ["a.yml"])
        for s in (NEW["url"], NEW["sha256"], NEW["image_sha256"], "Bug Fixes", "netgear-movie, checks it",
                  "https://www.netgear.com/support/product/ms510txpp/", fc.DEFAULT_WINDOW):
            self.assertIn(s, d)

    def run_bump(self, latest, gets=None):
        gl = FakeGitLab(gets)
        am, mails = fake_am(gl)

        def bump(ns):
            Path(ns.json).write_text('{"new": %s}' % __import__("json").dumps(NEW))
            Path(ns.notes).write_text("notes")
            return 0
        with mock.patch.object(fc, "_am", return_value=am), \
                mock.patch.object(ng, "find_latest", return_value=(latest, NEW["url"])), \
                mock.patch.object(ng, "cmd_firmware_bump", side_effect=bump), redirect_stdout(io.StringIO()):
            rc = fc.cmd_bump_mr(types.SimpleNamespace(dry_run=False, cache_dir="/nonexistent"))
        return rc, gl, mails

    def test_nothing_newer(self):
        rc, gl, mails = self.run_bump("6.7.0.52")
        self.assertEqual((rc, gl.writes, mails), (0, [], []))

    def test_opens_mr_once(self):
        rc, gl, mails = self.run_bump("6.7.0.60", gets={"/branches/netgear-firmware%2F6.7.0.60": RuntimeError("404")})
        self.assertEqual(rc, 0)
        commit = [w for w in gl.writes if w[1].endswith("/repository/commits")][0][2]
        self.assertEqual(commit["branch"], "netgear-firmware/6.7.0.60")
        self.assertEqual(sorted(a["file_path"] for a in commit["actions"]),
                         ["ci/reports/firmware.py", "networking/switches/netgear/netgear-movie.yml",
                          "networking/switches/netgear/netgear-office.yml"])
        mr = [w for w in gl.writes if w[1].endswith("/merge_requests")][0][2]
        self.assertEqual(mr["title"], "netgear: firmware 6.7.0.52 -> 6.7.0.60")
        self.assertEqual(mr["labels"], fc.LABEL)
        self.assertEqual(len(mails), 1)

    def test_existing_mr_is_not_duplicated(self):
        rc, gl, mails = self.run_bump("6.7.0.60", gets={"/merge_requests": [{"iid": 3, "state": "closed",
                                                                            "web_url": "u"}]})
        self.assertEqual((rc, gl.writes, mails), (0, [], []))


class TestRollout(unittest.TestCase):
    def run_rollout(self, rc, output, env=None, issues=None):
        gl = FakeGitLab({"/issues": issues or []})
        am, mails = fake_am(gl)
        script = f"import sys; sys.stdout.write({output!r}); sys.exit({rc})"
        real_popen = fc.subprocess.Popen
        seen = []

        def popen(cmd, **kw):
            seen.append(cmd)
            return real_popen([sys.executable, "-c", script], **kw)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env or {"CI_PIPELINE_SOURCE": "schedule"},
                                                                    clear=True), \
                mock.patch.object(fc, "_am", return_value=am), mock.patch.object(fc.subprocess, "Popen", popen), \
                redirect_stdout(io.StringIO()):
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                got = fc.cmd_rollout(types.SimpleNamespace(cache_dir="/c"))
                alerted = Path(fc.ALERTED).exists()
            finally:
                os.chdir(cwd)
        return got, gl, mails, seen, alerted

    def test_open_issue_pauses_rollouts(self):
        rc, gl, mails, seen, _ = self.run_rollout(0, "", issues=[{"iid": 5, "title": "t", "web_url": "u"}])
        self.assertEqual((rc, seen, gl.writes, mails), (0, [], [], []))

    def test_command_line(self):
        _, _, _, seen, _ = self.run_rollout(0, "", env={"CI_PIPELINE_SOURCE": "schedule",
                                                        "NETGEAR_TFTP_SERVER": "192.0.2.1"})
        cmd = seen[0]
        self.assertIn("--window", cmd)
        self.assertIn("192.0.2.1", cmd)
        self.assertTrue(cmd[-2].endswith("netgear-movie.yml") and cmd[-1].endswith("netgear-office.yml"))

    def test_failure_opens_issue_and_mails(self):
        out = "== firmware rollout: netgear-movie (1/2)\nERROR: netgear-movie: switch came back running 6.7.0.42\n"
        rc, gl, mails, _, alerted = self.run_rollout(1, out)
        self.assertEqual(rc, 1)
        issue = [w for w in gl.writes if w[1].endswith("/issues")][0][2]
        self.assertIn(fc.LABEL_FAILED, issue["labels"])
        self.assertIn("came back running", issue["description"])
        self.assertEqual(len(mails), 1)
        self.assertIn("FAILED", mails[0][0])
        self.assertTrue(alerted)

    def test_success_mails_only_when_something_was_upgraded(self):
        rc, gl, mails, _, _ = self.run_rollout(0, "netgear-movie: already running 6.7.0.52; nothing to upgrade\n")
        self.assertEqual((rc, gl.writes, mails), (0, [], []))
        rc, gl, mails, _, _ = self.run_rollout(0, "netgear-movie: upgraded 6.7.0.42 -> 6.7.0.52 in 200s; no drift\n")
        self.assertEqual(rc, 0)
        self.assertEqual(len(mails), 1)
        self.assertIn("upgraded 6.7.0.42 -> 6.7.0.52", mails[0][1])

    def test_dry_run_neither_opens_issues_nor_mails(self):
        rc, gl, mails, seen, _ = self.run_rollout(1, "ERROR: x\n", env={"NETGEAR_FIRMWARE_DRY_RUN": "1"})
        self.assertEqual((rc, gl.writes, mails), (1, [], []))
        self.assertIn("--dry-run", seen[0])


if __name__ == "__main__":
    unittest.main()
