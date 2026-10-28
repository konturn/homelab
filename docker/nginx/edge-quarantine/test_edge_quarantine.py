"""Offline tests for edge-quarantine (no nginx, docker, iptables or network)."""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import unittest

def slurp(path):
    with open(path) as f:
        return f.read()


HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("edge_quarantine", os.path.join(HERE, "edge-quarantine"))
_spec = importlib.util.spec_from_loader("edge_quarantine", _loader)
eq = importlib.util.module_from_spec(_spec)
_loader.exec_module(eq)


class FakeHost:
    """Stands in for docker, nginx-safe-reload, iptables, curl and logger."""

    def __init__(self):
        self.calls = []
        self.reload_ok = True
        self.chain = None          # None = chain absent, else list of "-A ..." lines
        self.hooked = False
        self.quarantined_hosts = set()   # what "nginx" serves 503 for (from the map)
        self.map_path = None

    def _served(self):
        text = slurp(self.map_path) if self.map_path and os.path.exists(self.map_path) else ""
        return [re.compile(l.split()[0][1:]) for l in text.splitlines() if l.startswith("~")]

    def run(self, cmd, timeout=60):
        self.calls.append(cmd)
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")
        fail = lambda err="": subprocess.CompletedProcess(cmd, 1, "", err)
        if cmd[0] == "reload":
            return ok("reloaded") if self.reload_ok else fail("[emerg] bad")
        if cmd[0] == "docker":
            return ok("true\n")
        if cmd[0] == "logger":
            return ok()
        if cmd[0] == "iptables":
            a = cmd[2:]
            if a[0] == "-S":
                return ok("\n".join([f"-N {a[1]}"] + self.chain) + "\n") if self.chain is not None else fail("no chain")
            if a[0] == "-N":
                self.chain = []
                return ok()
            if a[0] == "-F":
                self.chain = []
                return ok()
            if a[0] == "-A":
                self.chain.append(" ".join(a))   # as `iptables -S` prints it
                return ok()
            if a[0] == "-C":
                return ok() if self.hooked else fail()
            if a[0] == "-I":
                self.hooked = True
                return ok()
        if cmd[0] == "curl":
            url = cmd[-1]
            host_path = url.split("://", 1)[1]
            host, _, path = host_path.partition("/")
            hit = any(r.match(host + "/" + path) for r in self._served())
            hdr = "HTTP/2 503\r\nx-edge-quarantine: 1\r\n" if hit else "HTTP/2 200\r\nserver: nginx\r\n"
            return ok(hdr)
        raise AssertionError(f"unexpected command {cmd}")


class EdgeQuarantineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = dict(eq.DEFAULTS, state_dir=os.path.join(d, "state"), map_dir=os.path.join(d, "map"),
                        reload_cmd=["reload"], verify_addr="10.0.0.1", audit_log=os.path.join(d, "audit.log"))
        self.host = FakeHost()
        self.host.map_path = os.path.join(d, "map", eq.MAP_NAME)
        self._run = eq.run
        eq.run = self.host.run

    def tearDown(self):
        eq.run = self._run
        self.tmp.cleanup()

    def spec(self, **kw):
        return eq.parse_spec(kw.get("hosts", []), kw.get("paths", []), kw.get("ports", []))

    def audit(self):
        with open(self.cfg["audit_log"]) as f:
            return [json.loads(l) for l in f]

    # ---------------------------------------------------------------- validation

    def test_parse_spec_validates(self):
        s = self.spec(hosts=["Blog.Example.com."], paths=["example.com:/media/"], ports=["eth0:tcp:32400"])
        self.assertEqual(s, {"hosts": ["blog.example.com"], "paths": [["example.com", "/media/"]],
                             "ports": [["eth0", "tcp", 32400]]})
        for bad in (dict(hosts=["a b.com"]), dict(hosts=["x;y.com"]), dict(hosts=["localhost"]),
                    dict(paths=["example.com:/"]), dict(paths=["example.com"]), dict(paths=["example.com:/a b"]),
                    dict(ports=["eth0:icmp:1"]), dict(ports=["eth0:tcp:0"]), dict(ports=["e;th0:tcp:1"]), dict()):
            with self.assertRaises(eq.UsageError, msg=bad):
                self.spec(**bad)

    def test_map_keys_match_only_their_host_and_prefix(self):
        k = re.compile(eq.map_key("blog.example.com")[1:])
        self.assertTrue(k.match("blog.example.com/"))
        self.assertTrue(k.match("blog.example.com/wp-login.php"))
        self.assertFalse(k.match("blogxexample.com/"))
        self.assertFalse(k.match("blog.example.com.evil.net/"))
        self.assertFalse(k.match("www.blog.example.com/"))
        k = re.compile(eq.map_key("example.com", "/media/")[1:])
        self.assertTrue(k.match("example.com/media/x"))
        self.assertFalse(k.match("example.com/mediax"))
        self.assertFalse(k.match("example.com/"))

    def test_render_map_is_nginx_safe(self):
        st = {"entries": {"b": {"hosts": ["b.example.com"], "since": "t"},
                          "a": {"paths": [["example.com", "/p/"]], "since": "t"},
                          "c": {"ports": [["eth0", "tcp", 1]], "since": "t"}}}
        text = eq.render_map(st)
        lines = [l for l in text.splitlines() if not l.startswith("#")]
        self.assertEqual(lines, ["~^example\\.com/p/ 1;", "~^b\\.example\\.com/ 1;"])
        for l in lines:
            self.assertTrue(re.fullmatch(r"~\^[A-Za-z0-9\\./_~-]+ 1;", l), l)

    # ---------------------------------------------------------------- on / off

    def test_on_off_roundtrip_idempotent_and_audited(self):
        spec = self.spec(hosts=["canary.example.com"])
        rc, res = eq.cmd_on(self.cfg, "canary", spec, "drill", "tester")
        self.assertEqual(rc, 0, res)
        self.assertTrue(res["changed"])
        self.assertEqual(res["verify"][0]["status"], 503)
        self.assertIn("canary\\.example\\.com", slurp(self.host.map_path))
        st = eq.load_state(self.cfg)
        self.assertEqual(st["entries"]["canary"]["hosts"], ["canary.example.com"])
        self.assertEqual(os.stat(os.path.join(self.cfg["state_dir"], "state.json")).st_mode & 0o777, 0o600)
        reloads = sum(1 for c in self.host.calls if c[0] == "reload")
        # second identical call: no reload, no change
        rc, res = eq.cmd_on(self.cfg, "canary", spec, "drill again", "tester")
        self.assertEqual((rc, res["changed"]), (0, False))
        self.assertEqual(sum(1 for c in self.host.calls if c[0] == "reload"), reloads)
        rc, res = eq.cmd_off(self.cfg, "canary", "done", "tester")
        self.assertEqual((rc, res["changed"]), (0, True))
        self.assertTrue(res["verify"][0]["ok"])
        self.assertEqual(eq.load_state(self.cfg)["entries"], {})
        rc, res = eq.cmd_off(self.cfg, "canary", "done", "tester")
        self.assertEqual((rc, res["changed"]), (0, False))
        self.assertEqual([a["action"] for a in self.audit()], ["on", "off"])
        # no ports anywhere: the firewall was never touched beyond the existence check
        self.assertFalse([c for c in self.host.calls if c[0] == "iptables" and c[2] in ("-N", "-I", "-A")])

    def test_rejected_reload_restores_previous_map_and_state(self):
        eq.cmd_on(self.cfg, "one", self.spec(hosts=["one.example.com"]), "r", "t")
        before_map = slurp(self.host.map_path)
        before_state = eq.load_state(self.cfg)
        self.host.reload_ok = False
        rc, res = eq.cmd_on(self.cfg, "two", self.spec(hosts=["two.example.com"]), "r", "t")
        self.assertEqual(rc, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(slurp(self.host.map_path), before_map)
        self.assertEqual(eq.load_state(self.cfg), before_state)
        self.assertEqual(self.audit()[-1]["result"], "failed")

    def test_failed_first_reload_removes_new_map(self):
        self.host.reload_ok = False
        rc, _ = eq.cmd_on(self.cfg, "one", self.spec(hosts=["one.example.com"]), "r", "t")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.host.map_path))

    def test_ports_use_own_chain_and_are_removed(self):
        spec = self.spec(hosts=["media.example.com"], ports=["wan0:tcp:32400"])
        rc, res = eq.cmd_on(self.cfg, "media", spec, "r", "t")
        self.assertEqual(rc, 0, res)
        self.assertTrue(self.host.hooked)
        self.assertEqual(len(self.host.chain), 1)
        self.assertIn("--dport 32400", self.host.chain[0])
        self.assertIn("-i wan0", self.host.chain[0])
        self.assertIn("--ctstate NEW", self.host.chain[0])
        # idempotent: same state -> no flush
        n = len(self.host.calls)
        eq.cmd_on(self.cfg, "media", spec, "r", "t")
        self.assertFalse([c for c in self.host.calls[n:] if c[0] == "iptables" and c[2] == "-F"])
        eq.cmd_off(self.cfg, "media", "r", "t")
        self.assertEqual(self.host.chain, [])

    def test_verification_failure_is_reported(self):
        self.host.map_path = "/nonexistent"   # nginx "serves" nothing
        rc, res = eq.cmd_on(self.cfg, "x", self.spec(hosts=["x.example.com"]), "r", "t")
        self.assertEqual(rc, 2)
        self.assertFalse(res["ok"])
        self.assertEqual(self.audit()[-1]["result"], "applied, verification failed")

    def test_render_restores_map_from_state(self):
        eq.cmd_on(self.cfg, "a", self.spec(hosts=["a.example.com"], ports=["wan0:tcp:1"]), "r", "t")
        os.unlink(self.host.map_path)       # e.g. conf dir recreated
        self.host.chain, self.host.hooked = None, False   # e.g. reboot
        rc, res = eq.cmd_render(self.cfg)
        self.assertEqual(rc, 0, res)
        self.assertTrue(res["changed"])
        self.assertIn("a\\.example\\.com", slurp(self.host.map_path))
        self.assertTrue(self.host.hooked)
        rc, res = eq.cmd_render(self.cfg)
        self.assertEqual((rc, res["changed"]), (0, False))

    def test_cli_usage_errors(self):
        self.assertEqual(eq.main(["--json", "on", "Bad_Name", "--host", "a.example.com", "--reason", "r",
                                  "--actor", "t"]), 3)
        self.assertEqual(eq.main(["--json", "on", "ok", "--reason", "r", "--actor", "t"]), 3)

    def test_clean_text(self):
        self.assertEqual(eq.clean_text("a\n\tb\x00c" + "x" * 400)[:5], "a bc" + "x")
        self.assertLessEqual(len(eq.clean_text("x" * 400)), eq.TEXT_MAX)


if __name__ == "__main__":
    unittest.main()
