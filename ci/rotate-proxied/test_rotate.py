"""Offline tests for rotate.py (secrets:rotate-proxied). Run:
python3 -m unittest discover -s ci/rotate-proxied -p 'test_*.py' -v

The rotations run against in-memory fakes of Vault, the router and the
services' HTTP APIs; every test that rotates also checks that no old or new
value reached stdout/stderr.
"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rotate  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

OLD_TAUTULLI = "0123456789abcdef0123456789abcdef"
OLD_RADARR = "fedcba9876543210fedcba9876543210"
OMBI_KEY = "aaaabbbbccccddddeeeeffff00001111"


class FakeVault:
    def __init__(self, data, world=None):
        self.world = world
        self.data = {p: dict(v) for p, v in data.items()}
        self.patches = []
        self.fail_patches = 0

    def read(self, path):
        d = dict(self.data[path])
        for v in d.values():
            rotate.secret(v)
        return d

    def patch(self, path, fields, tries=5, sleep=None):
        if self.fail_patches:
            self.fail_patches -= 1
            raise rotate.RotationError(f"Vault patch {path} failed: HTTP 500")
        self.patches.append((path, dict(fields)))
        if self.world is not None:
            self.world.events.append(("vault", sorted(fields)))
        for k, v in fields.items():
            rotate.secret(v)
            if v is None:
                self.data[path].pop(k, None)
            else:
                self.data[path][k] = v
        return len(self.patches)


class FakeRouter:
    key = "tmp"

    def __init__(self, world):
        self.world = world
        self.commands = []

    def run(self, command, stdin=None, check=True, timeout=300):
        self.commands.append((command, stdin))
        w = self.world
        if command.startswith("date -u"):
            return "2026-11-16T12:00:00Z\n"
        if "docker kill -s HUP jit-proxy" in command:
            return ""
        if "docker logs" in command:
            return "0\n"
        if "influx query" in command:
            return "#datatype,string\n,result,table,_time\n,,0,2026-11-16T12:01:00Z\n"
        if "docker stop -t 30 tautulli" in command:
            w.tautulli = stdin.strip()
            w.events.append(("service", "tautulli"))
            return ""
        if "radarr/config.xml" in command:
            return w.radarr + "\n"
        if "docker inspect" in command and "ombi" in command:
            return "10.3.0.20\n"
        raise AssertionError(f"unexpected router command: {command}")

    def tunnel(self, target, port):
        return 40000

    def close(self):
        pass


class FakeHttp:
    def __init__(self, world):
        self.world = world
        self.calls = []

    def request(self, method, url, *, headers=None, body=None, json_body=None, content_type=None):
        self.calls.append((method, url))
        w = self.world
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        headers = headers or {}
        R = rotate.Response
        if u.netloc == "tautulli.lab.nkontur.com":
            if q.get("apikey") == w.tautulli:
                return R(200, json.dumps({"response": {"result": "success"}}).encode())
            return R(401, b'{"response": {"result": "error", "message": "Invalid apikey"}}')
        if u.netloc == "radarr.lab.nkontur.com":
            if headers.get("X-Api-Key") != w.radarr:
                return R(401, b"")
            if u.path == "/api/v3/system/status":
                return R(200, b"{}")
            if u.path == "/api/v3/command" and json_body == {"name": "ResetApiKey"}:
                w.radarr = "1234567890abcdef1234567890abcdef"
                return R(201, b"{}")
        if u.netloc == "127.0.0.1:40000":  # Ombi through the tunnel
            if headers.get("ApiKey") != OMBI_KEY:
                return R(401, b"")
            if u.path == "/api/v1/Settings/radarr" and method == "GET":
                return R(200, json.dumps({"radarr": dict(w.ombi_radarr), "radarr4K": {"enabled": False}}).encode())
            if u.path == "/api/v1/Settings/radarr" and method == "POST":
                w.ombi_radarr = json_body["radarr"]
                return R(200, b"true")
            if u.path == "/api/v1/tester/radarr":
                return R(200, json.dumps({"isValid": json_body.get("apiKey") == w.radarr}).encode())
        raise AssertionError(f"unexpected request {method} {url}")


class World:
    def __init__(self):
        self.tautulli = OLD_TAUTULLI
        self.radarr = OLD_RADARR
        self.ombi_radarr = {"enabled": True, "apiKey": OLD_RADARR, "ip": "radarr", "port": 7878}
        self.events = []


def make_ctx(world, vault):
    deploys = []
    ctx = rotate.Ctx(vault, FakeRouter(world), FakeHttp(world), sleep=lambda s: None,
                     deploy=lambda telegraf, services: deploys.append((telegraf, services)))
    ctx.deploys = deploys
    return ctx


def run_quiet(fn, *a):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(*a)
    return rc, out.getvalue() + err.getvalue()


class AllowlistTests(unittest.TestCase):
    def test_every_listed_service_allowed(self):
        for name in rotate.SERVICES:
            self.assertEqual(rotate.validate_service(name), name)

    def test_plex_refused(self):
        for name in ("plex", "Plex", "PLEX", " plex", "plex\n"):
            with self.assertRaisesRegex(rotate.RotationError, "refused"):
                rotate.validate_service(name)

    def test_plex_not_in_services(self):
        self.assertNotIn("plex", rotate.SERVICES)
        self.assertFalse(any("plex" in n for n in rotate.SERVICES))

    def test_unknown_refused(self):
        for name in ("grafana", "radarr ", "Radarr", "radarr;plex", "../plex", "*", "gitlab"):
            with self.assertRaisesRegex(rotate.RotationError, "refused"):
                rotate.validate_service(name)

    def test_empty_refused(self):
        for name in ("", None):
            with self.assertRaises(rotate.RotationError):
                rotate.validate_service(name)

    def test_cli_exit_codes(self):
        self.assertEqual(run_quiet(rotate.main, ["check", "tautulli"])[0], 0)
        self.assertEqual(run_quiet(rotate.main, ["check", "plex"])[0], 2)
        self.assertEqual(run_quiet(rotate.main, ["check", "nope"])[0], 2)

    def test_run_refused_off_protected_main(self):
        env = {"CI_COMMIT_REF_PROTECTED": "false", "CI_COMMIT_BRANCH": "feature"}
        with mock.patch.dict(os.environ, env):
            rc, out = run_quiet(rotate.main, ["run", "--service", "tautulli"])
        self.assertEqual(rc, 1)
        self.assertIn("protected main", out)

    def test_run_plex_refused_before_anything(self):
        with mock.patch.object(rotate, "Vault") as v:
            rc, out = run_quiet(rotate.main, ["run", "--service", "plex"])
        self.assertEqual(rc, 2)
        v.assert_not_called()


class MaskTests(unittest.TestCase):
    def test_scrub_plain_and_encoded(self):
        m = rotate.Masker()
        m.add("s3cr3t/value+x")
        text = "a s3cr3t/value+x b s3cr3t%2Fvalue%2Bx"
        self.assertNotIn("s3cr3t", m.scrub(text))

    def test_short_values_not_registered(self):
        m = rotate.Masker()
        m.add("abc")
        m.add(None)
        self.assertEqual(len(m), 0)

    def test_http_error_has_no_query(self):
        key = "querysecret0123456789"
        with self.assertRaises(rotate.HttpError) as cm:
            rotate.Http(timeout=0.5).request("GET", f"http://127.0.0.1:9/api/v2?apikey={key}")
        self.assertNotIn(key, str(cm.exception))

    def test_subprocess_output_is_scrubbed(self):
        value = rotate.secret("deploysecret-0123456789")

        class P:
            stdout = io.BytesIO(f"TASK [x] PIHOLE={value}\nok\n".encode())

            def wait(self):
                return 0
        ctx = rotate.Ctx(None, FakeRouter(World()), None)
        with mock.patch.object(rotate.subprocess, "Popen", return_value=P()):
            rc, out = run_quiet(ctx._ansible_deploy, True, ["pihole"])
        self.assertNotIn(value, out)
        self.assertIn("[MASKED]", out)

    def test_no_xtrace_in_ci_job(self):
        ci = (REPO / ".gitlab-ci.yml").read_text()
        m = re.search(r"(?ms)^secrets:rotate-proxied:\n(.*?)(?=^\S)", ci)
        self.assertIsNotNone(m, "secrets:rotate-proxied job missing")
        job = m.group(1)
        self.assertNotRegex(job, r"set -[a-wyz]*x")
        self.assertNotIn("CI_DEBUG_TRACE", job)
        self.assertIn("set +x", job)
        self.assertIn('$CI_COMMIT_BRANCH == "main"', job)
        self.assertIn("vault-admin", (REPO / "ci/rotate-proxied/rotate.py").read_text())

    def test_values_never_in_router_argv(self):
        world = World()
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI}})
        ctx = make_ctx(world, vault)
        run_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        for cmd, _stdin in ctx.router.commands:
            self.assertNotIn(OLD_TAUTULLI, cmd)
            self.assertNotIn(world.tautulli, cmd)


class RotationTests(unittest.TestCase):
    def assertQuiet(self, out, *values):
        for v in values:
            self.assertNotIn(v, out)

    def test_tautulli_chosen(self):
        world = World()
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI, "other": "keep-me-123"}}, world)
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        self.assertEqual(rc, 0, out)
        new = vault.data["docker/tautulli"]["api_key"]
        self.assertNotEqual(new, OLD_TAUTULLI)
        self.assertEqual(world.tautulli, new)
        self.assertEqual(vault.data["docker/tautulli"], {"api_key": new, "other": "keep-me-123"})
        # order: marker, pending value, service change, promote with "applied"
        ev = world.events
        self.assertEqual(ev[:4], [("vault", ["api_key_rotation"]), ("vault", ["api_key_next"]),
                                  ("service", "tautulli"),
                                  ("vault", ["api_key", "api_key_next", "api_key_rotation"])])
        self.assertIn("rejects the old api_key", out)
        self.assertEqual(ctx.deploys, [(True, [])])
        self.assertQuiet(out, OLD_TAUTULLI, new)

    def test_refuses_when_vault_value_already_rejected(self):
        world = World()
        world.tautulli = "ffffffffffffffffffffffffffffffff"
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI}})
        rc, out = _rc_quiet(rotate.rotate, rotate.Tautulli(), make_ctx(world, vault))
        self.assertEqual(rc, 1)
        self.assertEqual(vault.patches, [])
        self.assertIn("not rotating", out)

    def test_resume_recovers_pending(self):
        world = World()
        pending = "99999999999999999999999999999999"
        world.tautulli = pending  # the service took it, the run died before the promote
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI, "api_key_next": pending,
                                               "api_key_rotation": "2026-11-16T11:00:00Z started"}})
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        self.assertEqual(rc, 0, out)
        self.assertEqual(vault.data["docker/tautulli"], {"api_key": pending})
        self.assertFalse(any("docker stop" in c for c, _ in ctx.router.commands))
        self.assertQuiet(out, OLD_TAUTULLI, pending)

    def test_resume_applied_runs_consumers_only(self):
        world = World()
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI,
                                               "api_key_rotation": "2026-11-16T11:00:00Z applied"}})
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        self.assertEqual(rc, 0, out)
        self.assertEqual(world.tautulli, OLD_TAUTULLI)  # not rotated again
        self.assertEqual(ctx.deploys, [(True, [])])
        self.assertEqual(vault.data["docker/tautulli"], {"api_key": OLD_TAUTULLI})

    def test_started_but_unchanged_rotates_fresh(self):
        world = World()
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI,
                                               "api_key_rotation": "2026-11-16T11:00:00Z started"}})
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(world.tautulli, OLD_TAUTULLI)
        self.assertEqual(vault.data["docker/tautulli"]["api_key"], world.tautulli)

    def test_vault_down_before_change_leaves_service_alone(self):
        world = World()
        vault = FakeVault({"docker/tautulli": {"api_key": OLD_TAUTULLI}})
        vault.fail_patches = 1  # the preflight write
        ctx = make_ctx(world, vault)
        rc, out = _rc_quiet(rotate.rotate, rotate.Tautulli(), ctx)
        self.assertEqual(rc, 1)
        self.assertEqual(world.tautulli, OLD_TAUTULLI)
        self.assertFalse(any("docker stop" in c for c, _ in ctx.router.commands))

    def test_radarr_generated_with_ombi_link(self):
        world = World()
        vault = FakeVault({"docker/radarr": {"api_key": OLD_RADARR},
                           "docker/ombi": {"api_key": OMBI_KEY}})
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Radarr(), ctx)
        self.assertEqual(rc, 0, out)
        new = vault.data["docker/radarr"]["api_key"]
        self.assertEqual(new, world.radarr)
        self.assertNotEqual(new, OLD_RADARR)
        self.assertEqual(world.ombi_radarr["apiKey"], new)
        self.assertIn("[ok] Ombi's radarr connection test", out)
        self.assertIn("[ok] radarr rejects the old api_key", out)
        self.assertQuiet(out, OLD_RADARR, new, OMBI_KEY)

    def test_generated_resume_reads_live_key(self):
        world = World()
        world.radarr = "abababababababababababababababab"
        vault = FakeVault({"docker/radarr": {"api_key": OLD_RADARR,
                                             "api_key_rotation": "2026-11-16T11:00:00Z started"},
                           "docker/ombi": {"api_key": OMBI_KEY}})
        ctx = make_ctx(world, vault)
        rc, out = run_quiet(rotate.rotate, rotate.Radarr(), ctx)
        self.assertEqual(rc, 0, out)
        self.assertEqual(vault.data["docker/radarr"]["api_key"], "abababababababababababababababab")
        self.assertQuiet(out, OLD_RADARR, "abababababababababababababababab")

    def test_failed_check_keeps_markers(self):
        world = World()
        world.ombi_radarr["ip"] = "radarr"
        vault = FakeVault({"docker/radarr": {"api_key": OLD_RADARR},
                           "docker/ombi": {"api_key": OMBI_KEY}})
        ctx = make_ctx(world, vault)
        ctx.telegraf_fresh = lambda m, since: False
        rc, out = run_quiet(rotate.rotate, rotate.Radarr(), ctx)
        self.assertEqual(rc, 1)
        self.assertIn("api_key_rotation", vault.data["docker/radarr"])
        self.assertTrue(vault.data["docker/radarr"]["api_key_rotation"].endswith("applied"))


def _rc(fn, *a):
    try:
        return fn(*a)
    except rotate.RotationError as e:
        rotate.log(f"ERROR: {e}")
        return 1


def _rc_quiet(fn, *a):
    return run_quiet(lambda: _rc(fn, *a))


class ComposeImageTests(unittest.TestCase):
    def test_compose_consumers_have_literal_images(self):
        for name, cls in rotate.SERVICES.items():
            for c in cls.compose:
                img = rotate.compose_image(c)
                self.assertTrue(img and "{{" not in img, f"{name}: {c} image {img!r}")


class ArrWarningsOnly(unittest.TestCase):
    def _r(self, status, body):
        return rotate.Response(status, json.dumps(body).encode())

    def test_warning_only_400_passes(self):
        r = self._r(400, [{"propertyName": "TvCategory", "errorMessage": "A category is recommended",
                           "isWarning": True}])
        self.assertEqual(rotate.arr_warnings_only(r), "TvCategory")

    def test_error_400_fails(self):
        r = self._r(400, [{"propertyName": "TvCategory", "isWarning": True},
                          {"propertyName": "Password", "errorMessage": "Authentication failed",
                           "isWarning": False}])
        self.assertEqual(rotate.arr_warnings_only(r), "")

    def test_other_status_or_body_fails(self):
        self.assertEqual(rotate.arr_warnings_only(self._r(500, [{"isWarning": True}])), "")
        self.assertEqual(rotate.arr_warnings_only(self._r(400, [])), "")
        self.assertEqual(rotate.arr_warnings_only(self._r(400, {"message": "x"})), "")
        self.assertEqual(rotate.arr_warnings_only(rotate.Response(400, b"<html>")), "")


if __name__ == "__main__":
    unittest.main()
