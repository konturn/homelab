"""Agent actions (README "Agent actions"): the executor, the router's
/agent API and the control service's approvals, run against fakes."""

import copy
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from router import agent_api
from router import config as router_config
from router.config import ConfigError, parse_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    path = os.path.join(HERE, "executor", name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_") + "_agent", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


X = load("event-router-executor")
C = load("event-router-control")

NOAH = "n" * 40 + "1"
OPS = "o" * 40 + "2"
MATRIX = "openclaw-noah=auto,auto-fyi,approve;openclaw-ops=auto,approve,deny"
SECRET = "s3cr3t-value-123456"


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


class R:
    def __init__(self, out="", rc=0, err=""):
        self.stdout, self.returncode, self.stderr = out, rc, err


class Env:
    """A host with fakes for docker, systemctl, the JIT socket and GitLab."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.shared = os.path.join(d, "shared")
        os.makedirs(self.shared)
        with open(os.path.join(self.shared, "windows.json"), "w") as f:
            json.dump({"windows": []}, f)
        raw = {
            "approval_ttl": "10m", "max_pending": 2,
            "collect": {"max_per_hour": 5, "containers": ["gitlab"], "units": ["telegraf.service"]},
            "restart_container": {"cooldown": "30m", "max_per_day": 3, "max_per_day_total": 4,
                                  "no_deploy_touching": "2h",
                                  "containers": ["plex", "iperf3", "radarr", "sonarr", "airplay_*",
                                                 # a config that lists denied names: the
                                                 # executor's own lists still refuse them
                                                 "mosquitto", "openclaw-gateway", "gitlab", "vault"],
                                  "auto_approve": {}},
            "reset_failed": {"cooldown": "10m", "max_per_day": 3, "max_per_day_total": 12,
                             "units": ["nut-driver@*.service", "er-selftest-*.service", "libvirtd.service"]},
        }
        self.cfg = {
            "router_uid": 2470, "repo_mode": "active", "compose_project": "docker",
            "override_file": os.path.join(d, "mode"), "shared_dir": self.shared,
            "state_file": os.path.join(d, "state.json"), "jit_socket": os.path.join(d, "jit.sock"),
            "restart_denylist": ["gitlab", "vault", "pihole", "event-router", "docker-socket-proxy"],
            "playbooks": {}, "limits": {"per_target_max": 2, "per_target_window": 21600, "global_per_hour": 6},
            "gitlab": {"url": "https://gitlab.invalid", "project_id": 4, "deploy_jobs": ["router:deploy"],
                       "orphan_jobs": {}, "grace": 300},
            "dependencies": {"persistent_data": {"kind": "mount", "needs": [], "probe": {}}},
            "agent_tokens": {"openclaw-noah": sha(NOAH), "openclaw-ops": sha(OPS)},
            "approval_matrix": MATRIX,
            "agent_metrics_file": os.path.join(d, "agent.influx"),
            "agent_actions": X.agent_config(raw),
        }
        self.ran = []          # every command that is not a read
        self.reads = []        # every command, reads included
        self.prompts, self.notes = [], []
        self.failed_units = set()
        self.created = 0
        self.deploys = []
        X.probe = lambda spec: True
        X.deploy_running = lambda cfg: list(self.deploys)
        X.container_created = lambda name: self.created
        X.gitleaks_secrets = lambda text: []

        def docker(*args, timeout=30):
            self.reads.append(("docker",) + args)
            if args[0] == "inspect" and len(args) > 2:
                return R("docker\n") if args[-1] in KNOWN else R("", 1, "No such object")
            if args[0] == "inspect":
                return R(json.dumps([{"Name": "/" + args[1], "Created": "2026-01-01T00:00:00Z",
                                      "Config": {"Image": "x", "Env": [f"API_KEY={SECRET}", "TZ=UTC"],
                                                 "Labels": {"com.docker.compose.project": "docker",
                                                            "traefik.secret": SECRET}},
                                      "State": {"Status": "running", "Running": True},
                                      "Mounts": [{"Destination": "/config", "RW": True,
                                                  "Source": "/persistent_data/x"}]}]))
            self.ran.append(("docker",) + args)
            return R()
        X.docker = docker

        class Sub:
            SubprocessError = X.subprocess.SubprocessError
            PIPE = X.subprocess.PIPE
            STDOUT = X.subprocess.STDOUT

            @staticmethod
            def run(args, **kw):
                env.reads.append(tuple(args))
                if args[:2] == ["systemctl", "is-failed"]:
                    return R(rc=0 if args[-1] in env.failed_units else 1)
                if args[:2] == ["docker", "logs"]:
                    lines = [f"2026-11-16T10:00:{i:02d}Z line {i} token={SECRET} "
                             f"Authorization: Bearer abcdefghijklmnop{i}" for i in range(400)]
                    return R(("\n".join(lines) + "\n").encode())
                if args[:2] == ["docker", "ps"] or (
                        args[0] in ("systemctl", "zpool", "df") and args[1] not in ("reset-failed",)):
                    return R(f"{' '.join(args)}: ok password={SECRET}\n".encode())
                env.ran.append(tuple(args))
                return R()
        env = self
        X.subprocess = Sub

        def jit(cfg, method, path, body):
            if path == "/v1/prompts":
                self.prompts.append(body)
            elif path == "/v1/notify":
                self.notes.append(body["text"])
            return 201
        X.jit = jit

    def req(self, action, target, token=OPS, **kw):
        r = {"action": action, "target": target, "token": token}
        r.update(kw)
        return r

    def run(self, req, now=None):
        rec = {}
        try:
            reply = X.dispatch(self.cfg, {"agent": req}, now or time.time(), rec)
        except X.Refused as e:
            reply = {"ok": False, "refused": e.why, "detail": e.detail}
        return reply, rec

    def state(self):
        with open(self.cfg["state_file"]) as f:
            return json.load(f)

    def set_approval(self, apr, **fields):
        st = self.state()
        st["approvals"][apr].update(fields)
        with open(self.cfg["state_file"], "w") as f:
            json.dump(st, f)

    def close(self):
        self.tmp.cleanup()


KNOWN = {"plex", "iperf3", "radarr", "sonarr", "airplay_kitchen", "gitlab", "mosquitto", "openclaw-gateway",
         "vault"}


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_unknown_or_missing_token(self):
        for tok in ("", "x" * 40, NOAH + "x", None, 7):
            reply, _ = self.env.run(self.env.req("collect", "host", token=tok))
            self.assertEqual(reply.get("refused"), "auth", tok)
            self.assertFalse(reply["ok"])
        self.assertEqual(self.env.ran, [])

    def test_requester_comes_from_the_token_only(self):
        # A field naming a requester is not part of the schema: refused.
        reply, _ = self.env.run(dict(self.env.req("restart_container", "plex"), requester="openclaw-noah"))
        self.assertEqual(reply["refused"], "not_allowed")
        self.assertEqual(self.env.ran, [])
        # ops's token is ops: a restart waits for a tap, nothing runs.
        reply, rec = self.env.run(self.env.req("restart_container", "plex", reason="hung"))
        self.assertEqual(rec["requester"], "openclaw-ops")
        self.assertTrue(reply["pending"])
        self.assertFalse(reply["ok"])
        self.assertEqual(self.env.ran, [])

    def test_an_approval_belongs_to_its_requester(self):
        reply, _ = self.env.run(self.env.req("restart_container", "plex"))
        apr = reply["approval"]
        self.env.set_approval(apr, status="approved", decided=time.time())
        # noah presenting ops's approval for the same target: refused.
        reply, _ = self.env.run(self.env.req("restart_container", "plex", token=NOAH, approval=apr))
        self.assertEqual(reply["refused"], "not_allowed")
        # ops presenting it for another target: refused.
        reply, _ = self.env.run(self.env.req("restart_container", "radarr", approval=apr))
        self.assertEqual(reply["refused"], "not_allowed")
        self.assertEqual(self.env.ran, [])

    def test_journal_record_has_no_token_or_output(self):
        rec = {"event": "request"}
        X.dispatch(self.env.cfg, {"agent": self.env.req("collect", "host")}, time.time(), rec)
        self.assertNotIn(OPS, json.dumps(rec))
        self.assertNotIn("output", json.dumps(rec))
        self.assertEqual(rec["requester"], "openclaw-ops")

    def test_metrics_line(self):
        self.env.run(self.env.req("collect", "host"))
        X.audit_metric(self.env.cfg, "openclaw-ops", "collect", "host", "ok", 0.5)
        X.audit_metric(self.env.cfg, None, "collect", "bad target, x=1", "auth", 0.1)
        with open(self.env.cfg["agent_metrics_file"]) as f:
            lines = f.read().splitlines()
        self.assertTrue(lines[0].startswith("event_router_agent_action,requester=openclaw-ops,action=collect,"
                                            "target=host,result=ok count=1i"))
        self.assertIn("requester=none,action=collect,target=invalid,result=auth", lines[1])


class TestAllowlists(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_denied_containers_never_restart_even_if_configured(self):
        for target in ("mosquitto", "openclaw-gateway", "gitlab", "vault", "jit-approval-svc", "pihole",
                       "homeassistant", "lr4-broker", "runner-abc-build", "nextcloud"):
            for tok in (NOAH, OPS):
                reply, _ = self.env.run(self.env.req("restart_container", target, token=tok))
                self.assertEqual(reply["refused"], "not_allowed", (target, reply))
        self.assertEqual(self.env.ran, [])
        self.assertEqual(self.env.prompts, [])

    def test_collect_allowlist(self):
        for target in ("container:vault", "container:openclaw-gateway", "container:bitwarden",
                       "container:jit-approval-svc", "container:nextcloud", "unit:ssh.service",
                       "unit:libvirtd.service", "unit:virtqemud.service", "files:/etc/shadow", "everything"):
            reply, _ = self.env.run(self.env.req("collect", target))
            self.assertEqual(reply["refused"], "not_allowed", target)
        reply, _ = self.env.run(self.env.req("collect", "container:gitlab"))      # collect-only list
        self.assertTrue(reply["ok"])
        reply, _ = self.env.run(self.env.req("collect", "container:airplay_kitchen"))   # restart list glob
        self.assertTrue(reply["ok"])

    def test_reset_failed_allowlist_and_vm_units(self):
        self.env.failed_units |= {"libvirtd.service", "ssh.service", "nut-driver@apc.service"}
        for unit in ("libvirtd.service", "ssh.service", "nut-driver@apc", "nut-driver@*.service"):
            reply, _ = self.env.run(self.env.req("reset_failed", unit))
            self.assertEqual(reply["refused"], "not_allowed", unit)
        self.assertEqual(self.env.ran, [])
        reply, _ = self.env.run(self.env.req("reset_failed", "nut-driver@apc.service"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.env.ran, [("systemctl", "reset-failed", "--", "nut-driver@apc.service")])

    def test_reset_failed_only_clears_failed_units_and_never_starts(self):
        reply, _ = self.env.run(self.env.req("reset_failed", "er-selftest-1.service"))
        self.assertEqual(reply["refused"], "not_failed")
        self.env.failed_units.add("er-selftest-1.service")
        reply, _ = self.env.run(self.env.req("reset_failed", "er-selftest-1.service", token=NOAH))
        self.assertTrue(reply["ok"])
        for cmd in self.env.ran:
            self.assertNotIn("start", cmd)
            self.assertNotIn("restart", cmd)

    def test_tier_matrix(self):
        self.env.cfg["approval_matrix"] = "openclaw-noah=auto,auto-fyi,approve;openclaw-ops=deny,deny,deny"
        reply, _ = self.env.run(self.env.req("collect", "host"))
        self.assertEqual(reply["refused"], "tier")
        reply, _ = self.env.run(self.env.req("collect", "host", token=NOAH))
        self.assertTrue(reply["ok"])
        self.env.cfg["approval_matrix"] = ""          # unknown requester row: deny
        reply, _ = self.env.run(self.env.req("collect", "host", token=NOAH))
        self.assertEqual(reply["refused"], "tier")

    def test_matrix_never_auto_at_tier_3(self):
        self.assertEqual(X.parse_matrix("a=auto,auto,auto")["a"], ["auto", "auto", "deny"])
        self.assertEqual(X.parse_matrix("a=auto,yes,approve")["a"], ["auto", "deny", "approve"])


INJECTIONS = ["plex;reboot", "plex && reboot", "-f", "--all", "$(id)", "`id`", "plex\nreboot", "../plex",
              "plex ", " plex", "PLEX", "plex|sh", "container:-f", "container:plex;id", "unit:--all",
              "unit:-H", "unit:a.service;id", "unit:../../etc/passwd", "host;id", "container:", "unit:",
              "nut-driver@apc.service;id", "-nut-driver@apc.service", "nut-driver@apc.service\n",
              "airplay_*", "*", "?"]


class TestInjection(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.failed_units.add("nut-driver@apc.service")

    def tearDown(self):
        self.env.close()

    def assert_no_unsafe_command(self):
        for cmd in self.env.reads:
            for a in cmd:
                self.assertFalse(any(c in a for c in ";|&$`\n") and not a.startswith("{{"), cmd)
                self.assertNotIn("*", a.replace("{{", "").replace("}}", ""), cmd)

    def test_known_injections(self):
        for target in INJECTIONS:
            for action in X.AGENT_ACTIONS:
                for tok in (NOAH, OPS):
                    reply, _ = self.env.run(self.env.req(action, target, token=tok))
                    self.assertFalse(reply.get("ok"), (action, target, reply))
        self.assertEqual(self.env.ran, [])
        self.assertEqual(self.env.prompts, [])
        self.assert_no_unsafe_command()

    @settings(max_examples=300, deadline=None)
    @given(action=st.sampled_from(X.AGENT_ACTIONS), target=st.text(max_size=40),
           reason=st.one_of(st.none(), st.text(max_size=400)), tok=st.sampled_from([NOAH, OPS]))
    def test_arbitrary_targets_only_reach_allowlisted_names(self, action, target, reason, tok):
        env = Env()
        try:
            env.failed_units.add(target)
            env.run(env.req(action, target, token=tok, reason=reason))
            # Whatever acted named exactly the target, and the target is an
            # allowlisted plain name.
            units = env.cfg["agent_actions"]["reset_failed"]["units"]
            for cmd in env.ran:
                self.assertEqual(cmd[-1], target)
                self.assertTrue(target in KNOWN or (X.UNIT.match(target) and X.matches(target, units)), cmd)
                self.assertFalse(set(target) & set(";|&$`*?[ \n"), target)
        finally:
            env.close()


class TestRestart(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.now = time.time()

    def tearDown(self):
        self.env.close()

    def test_noah_restarts_at_once_and_noah_is_told(self):
        reply, _ = self.env.run(self.env.req("restart_container", "iperf3", token=NOAH, reason="test"), self.now)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.env.ran, [("docker", "restart", "--time", "30", "iperf3")])
        self.assertEqual(len(self.env.notes), 1)
        self.assertIn("openclaw-noah", self.env.notes[0])
        self.assertIn("iperf3", self.env.notes[0])

    def test_ops_waits_for_a_tap(self):
        reply, _ = self.env.run(self.env.req("restart_container", "plex", reason="hung <b>"), self.now)
        apr = reply["approval"]
        self.assertTrue(reply["pending"])
        self.assertEqual(len(self.env.prompts), 1)
        self.assertEqual(self.env.prompts[0]["id"], apr)
        self.assertIn("&lt;b&gt;", self.env.prompts[0]["text"])
        # Asking again before the tap: still pending, no second prompt, nothing ran.
        reply, _ = self.env.run(self.env.req("restart_container", "plex", approval=apr), self.now + 5)
        self.assertTrue(reply["pending"])
        self.assertEqual(len(self.env.prompts), 1)
        self.assertEqual(self.env.ran, [])
        # Noah taps Approve (control service), then the agent asks again.
        C.jit = lambda cfg, m, p, b=None: (200, {})
        C.decide_approval(self.env.cfg, {"id": apr, "action": "approve"}, self.now + 10)
        reply, _ = self.env.run(self.env.req("restart_container", "plex", approval=apr), self.now + 20)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(self.env.ran, [("docker", "restart", "--time", "30", "plex")])
        self.assertIn("approved by you", self.env.notes[-1])
        # An approval is good once.
        reply, _ = self.env.run(self.env.req("restart_container", "plex", approval=apr), self.now + 4000)
        self.assertEqual(reply["refused"], "approval")

    def test_denied_and_expired_taps(self):
        C.jit = lambda cfg, m, p, b=None: (200, {})
        reply, _ = self.env.run(self.env.req("restart_container", "plex"), self.now)
        a1 = reply["approval"]
        C.decide_approval(self.env.cfg, {"id": a1, "action": "deny"}, self.now + 5)
        reply, _ = self.env.run(self.env.req("restart_container", "plex", approval=a1), self.now + 6)
        self.assertEqual(reply["refused"], "approval")
        reply, _ = self.env.run(self.env.req("restart_container", "radarr"), self.now)
        a2 = reply["approval"]
        C.expire_approvals(self.env.cfg, self.now + 601)
        self.assertEqual(self.env.state()["approvals"][a2]["status"], "expired")
        C.decide_approval(self.env.cfg, {"id": a2, "action": "approve"}, self.now + 602)   # too late
        reply, _ = self.env.run(self.env.req("restart_container", "radarr", approval=a2), self.now + 603)
        self.assertEqual(reply["refused"], "approval")
        # A pending request past its expiry is refused even before control sees it.
        reply, _ = self.env.run(self.env.req("restart_container", "sonarr"), self.now)
        reply, _ = self.env.run(self.env.req("restart_container", "sonarr", approval=reply["approval"]),
                                self.now + 700)
        self.assertEqual(reply["refused"], "approval")
        self.assertEqual(self.env.ran, [])

    def test_pending_cap(self):
        for t in ("plex", "radarr"):
            self.assertTrue(self.env.run(self.env.req("restart_container", t), self.now)[0]["pending"])
        reply, _ = self.env.run(self.env.req("restart_container", "sonarr"), self.now)
        self.assertEqual(reply["refused"], "rate_limit")

    def test_auto_approve_subset_is_off_by_default(self):
        self.assertEqual(self.env.cfg["agent_actions"]["restart_container"]["auto_approve"], {})
        self.env.cfg["agent_actions"]["restart_container"]["auto_approve"] = {"openclaw-ops": ["iperf3"]}
        reply, _ = self.env.run(self.env.req("restart_container", "iperf3"), self.now)
        self.assertTrue(reply["ok"])
        reply, _ = self.env.run(self.env.req("restart_container", "plex"), self.now)
        self.assertTrue(reply["pending"])

    def test_cooldown_and_daily_caps(self):
        n = lambda t, dt: self.env.run(self.env.req("restart_container", t, token=NOAH), self.now + dt)[0]
        self.assertTrue(n("plex", 0)["ok"])
        r = n("plex", 60)
        self.assertEqual(r["refused"], "cooldown")
        # the playbooks' per-target window (2 per 6h) applies too
        self.assertTrue(n("plex", 1801)["ok"])
        self.assertEqual(n("plex", 3700)["refused"], "rate_limit")
        # per-container daily cap of 3, after the 6h window
        self.assertTrue(n("plex", 6 * 3600 + 10)["ok"])
        self.assertEqual(n("plex", 6 * 3600 + 1900)["refused"], "rate_limit")
        # total daily cap (4)
        self.assertTrue(n("radarr", 7 * 3600)["ok"])
        self.assertEqual(n("sonarr", 7 * 3600 + 10)["refused"], "rate_limit")
        self.assertEqual(len([c for c in self.env.ran if c[1] == "restart"]), 4)

    def test_existing_guards_apply(self):
        noah = lambda t="plex": self.env.run(self.env.req("restart_container", t, token=NOAH), self.now)[0]
        self.env.created = self.now - 600
        r = noah()
        self.assertEqual(r["refused"], "recent_deploy")
        self.env.created = 0
        self.env.deploys = ["router:deploy"]
        self.assertEqual(noah()["refused"], "deploy")
        self.env.deploys = []
        with open(self.env.cfg["override_file"], "w") as f:
            f.write("notify-only\n")
        self.assertEqual(noah()["refused"], "kill_switch")
        # collect is read-only: the kill switch does not stop it
        self.assertTrue(self.env.run(self.env.req("collect", "host", token=NOAH))[0]["ok"])
        os.unlink(self.env.cfg["override_file"])
        with open(os.path.join(self.env.shared, "windows.json"), "w") as f:
            json.dump({"windows": [{"id": "maint-1", "scope": ["site"], "expires_at": self.now + 60}]}, f)
        self.assertEqual(noah()["refused"], "maintenance")
        self.assertEqual(self.env.ran, [])

    def test_once_per_incident_shared_with_playbooks(self):
        st = {"incidents": {"inc-0123456789ab": self.now}, "history": []}
        with open(self.env.cfg["state_file"], "w") as f:
            json.dump(st, f)
        r = self.env.run(self.env.req("restart_container", "plex", token=NOAH, incident="inc-0123456789ab"),
                         self.now)[0]
        self.assertEqual(r["refused"], "duplicate")


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_container_redacted_and_bounded(self):
        reply, _ = self.env.run(self.env.req("collect", "container:plex"))
        out = reply["output"]
        self.assertTrue(reply["ok"])
        self.assertNotIn(SECRET, out)
        self.assertIn("API_KEY=<redacted>", out)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertNotIn("traefik", out)
        self.assertNotIn("/persistent_data/x", out)
        self.assertLessEqual(len(out.encode()), X.MAX_OUTPUT + 20)
        self.assertIn("line 399", out)          # the newest lines are kept
        self.assertEqual(self.env.ran, [])

    def test_host_and_unit(self):
        reply, _ = self.env.run(self.env.req("collect", "host"))
        for want in ("zpool status -x", "df -hT", "docker ps", "systemctl --failed"):
            self.assertIn(want.split()[0], reply["output"])
        self.assertNotIn(SECRET, reply["output"])
        reply, _ = self.env.run(self.env.req("collect", "unit:telegraf.service"))
        self.assertIn("systemctl status --no-pager --full --lines=40 -- telegraf.service", reply["output"])
        self.assertEqual(self.env.ran, [])

    def test_rate_limit(self):
        for _ in range(5):
            self.assertTrue(self.env.run(self.env.req("collect", "host"))[0]["ok"])
        self.assertEqual(self.env.run(self.env.req("collect", "host"))[0]["refused"], "rate_limit")
        self.assertTrue(self.env.run(self.env.req("collect", "host", token=NOAH))[0]["ok"])   # per requester

    def test_redact(self):
        text = ("Authorization: Bearer abc.def.ghijklmnop\nhttps://user:pw123@host/x\n"
                "glpat-abcdefghijklmnopqrstuvwx ghp_abcdefghijklmnopqrstuvwxyz0123\n"
                "password=hunter22 \"api_key\": \"k123456\" hvs.CAESIabcdefghijklmnopqrstuvwxyz\n"
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefgh\n"
                "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\nok line")
        out = X.redact(text, known=["k123456"])
        for s in ("abc.def", "pw123", "glpat-abc", "ghp_abc", "hunter22", "k123456", "hvs.CAES", "eyJhbGci", "AAAA"):
            self.assertNotIn(s, out, s)
        self.assertIn("ok line", out)


class TestRouterAPI(unittest.TestCase):
    class Exec:
        def __init__(self):
            self.got = []

        def agent(self, req):
            self.got.append(req)
            return {"ok": True, "detail": "collected host", "output": "x"}

    class Eng:
        def __init__(self, ex):
            self.executor = ex
            self.logged = []

        def _log(self, rec):
            self.logged.append(rec)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "agent-tokens.json")
        with open(self.path, "w") as f:
            json.dump({"openclaw-noah": sha(NOAH), "openclaw-ops": sha(OPS)}, f)
        self.tokens = agent_api.Tokens(self.path)
        self.ex = self.Exec()
        self.eng = self.Eng(self.ex)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, header, action="collect", body=None):
        return agent_api.handle(self.eng, self.tokens, header, action,
                                {"target": "host"} if body is None else body)

    def test_auth(self):
        for h in ("", "Bearer ", "Bearer " + "x" * 40, NOAH, "Basic " + NOAH):
            self.assertEqual(self.call(h)[0], 401)
        self.assertEqual(self.ex.got, [])
        code, reply = self.call("Bearer " + OPS)
        self.assertEqual(code, 200)
        self.assertEqual(self.ex.got[0]["token"], OPS)
        self.assertEqual(self.eng.logged[-1]["requester"], "openclaw-ops")
        self.assertNotIn(OPS, json.dumps(self.eng.logged))
        self.assertNotIn("output", json.dumps(self.eng.logged))

    def test_tokens_file_reloaded(self):
        self.assertEqual(self.call("Bearer " + OPS)[0], 200)
        with open(self.path, "w") as f:
            json.dump({"openclaw-noah": sha(NOAH)}, f)
        os.utime(self.path, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.call("Bearer " + OPS)[0], 401)
        os.unlink(self.path)
        self.assertEqual(self.call("Bearer " + NOAH)[0], 401)

    def test_schema(self):
        bad = [{"target": "host", "requester": "openclaw-noah"}, {"target": "host", "token": NOAH},
               {"target": "plex;id"}, {"target": 3}, {"target": "plex", "incident": "x"},
               {"target": "plex", "approval": "apr-1"}, {"target": "plex", "reason": "x" * 301}, []]
        for body in bad:
            self.assertEqual(self.call("Bearer " + OPS, "restart_container", body)[0], 400, body)
        self.assertEqual(self.call("Bearer " + OPS, "shell", {"target": "host"})[0], 400)
        self.assertEqual(self.ex.got, [])

    def test_status_codes(self):
        for reply, code in (({"ok": False, "pending": True, "approval": "apr-0123456789ab"}, 202),
                            ({"ok": False, "refused": "not_allowed"}, 403),
                            ({"ok": False, "refused": "cooldown"}, 429),
                            ({"ok": False, "refused": "recent_deploy", "retry_after": 60}, 409),
                            ({"ok": False, "error": "boom"}, 502)):
            self.ex.agent = lambda req, r=reply: r
            self.assertEqual(self.call("Bearer " + OPS, "restart_container", {"target": "plex"})[0], code)


class TestConfig(unittest.TestCase):
    def base(self):
        with open(os.path.join(HERE, "config.yml")) as f:
            import yaml
            return yaml.safe_load(f)

    def test_repo_config(self):
        cfg = parse_config(self.base())
        rc = cfg["agent_actions"]["restart_container"]
        self.assertEqual(rc["auto_approve"], {})            # default off
        for d in router_config.AGENT_RESTART_DENIED:
            self.assertFalse(X.matches(d, rc["containers"]), d)

    def test_same_lists_as_executor(self):
        self.assertEqual(router_config.AGENT_RESTART_DENIED, X.AGENT_RESTART_DENIED)
        self.assertEqual(router_config.COLLECT_DENIED, X.COLLECT_DENIED)
        self.assertEqual(router_config.UNIT_DENIED, X.UNIT_DENIED)

    def test_rejects(self):
        cases = [
            ("restart_container", "containers", ["mosquitto"]),
            ("restart_container", "containers", ["openclaw-*"]),
            ("restart_container", "containers", ["*"]),
            ("restart_container", "containers", ["runner-*"]),
            ("restart_container", "containers", ["Plex;id"]),
            ("collect", "containers", ["vault"]),
            ("collect", "containers", ["jit-*"]),
            ("reset_failed", "units", ["libvirtd.service"]),
            ("reset_failed", "units", ["*.service"]),
            ("restart_container", "auto_approve", {"openclaw-evil": ["plex"]}),
            ("restart_container", "auto_approve", {"openclaw-ops": ["airplay_*"]}),
            ("restart_container", "auto_approve", {"openclaw-ops": ["gitlab"]}),
        ]
        for part, key, value in cases:
            raw = self.base()
            raw["agent_actions"][part][key] = value
            with self.assertRaises(ConfigError, msg=(part, key, value)):
                parse_config(raw)

    def test_executor_reads_the_rendered_shape(self):
        aa = parse_config(self.base())["agent_actions"]
        raw = copy.deepcopy(self.base()["agent_actions"])
        x = X.agent_config(raw)
        self.assertEqual(x["restart_container"]["cooldown"], aa["restart_container"]["cooldown"])
        self.assertEqual(x["approval_ttl"], aa["approval_ttl"])
        self.assertEqual(x["reset_failed"]["units"], aa["reset_failed"]["units"])


if __name__ == "__main__":
    unittest.main()
