"""The host executor and control service (executor/), run against fakes."""

import importlib.machinery
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from router import config as router_config

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    path = os.path.join(HERE, "executor", name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


X = load("event-router-executor")
C = load("event-router-control")
INC = "inc-0123456789ab"


class Env:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.shared = os.path.join(d, "shared")
        os.makedirs(self.shared)
        self.write_windows([])
        self.cfg = {
            "router_uid": 2470, "repo_mode": "active", "compose_project": "docker",
            "override_file": os.path.join(d, "mode"), "shared_dir": self.shared,
            "state_file": os.path.join(d, "state.json"), "jit_socket": os.path.join(d, "jit.sock"),
            "restart_denylist": ["gitlab", "vault", "pihole", "event-router", "docker-socket-proxy", "mosquitto"],
            "limits": {"per_target_max": 2, "per_target_window": 21600, "global_per_hour": 6},
            "gitlab": {"url": "https://gitlab.invalid", "project_id": 4, "deploy_jobs": ["router:deploy"],
                       "orphan_jobs": {"router:deploy": 2700}, "grace": 300},
            "dependencies": {
                "persistent_data": {"kind": "mount", "needs": [], "probe": {"mount": "/persistent_data"}},
                "mosquitto": {"kind": "container", "needs": [], "probe": {"container": "mosquitto"}},
                "mqtt": {"kind": "endpoint", "needs": ["mosquitto"], "probe": {"tcp": "10.0.0.1:8883"}},
                "zigbee2mqtt": {"kind": "container", "needs": ["mqtt", "persistent_data"],
                                "probe": {"container": "zigbee2mqtt"}},
                "gitlab": {"kind": "container", "needs": ["persistent_data"], "probe": {"container": "gitlab"}},
            },
        }
        self.down = set()
        self.deploys = []
        self.ran = []
        self.job = {"status": "running", "name": "router:deploy",
                    "started_at": "2026-01-01T00:00:00Z"}
        X.probe = lambda spec: not (set(map(str, spec.values())) & self.down)
        X.deploy_running = lambda cfg: list(self.deploys)
        X.container_started = lambda name: 0

        class R:
            def __init__(self, out="", rc=0):
                self.stdout, self.returncode, self.stderr = out, rc, ""

        def docker(*args, timeout=30):
            if args[0] == "inspect":
                return R("docker\n")
            self.ran.append(args)
            return R()
        X.docker = docker

        def gitlab(cfg, method, path):
            if method == "POST":
                self.ran.append(("cancel", path))
                return {}
            return dict(self.job)
        X.gitlab = gitlab

    def write_windows(self, windows):
        with open(os.path.join(self.shared, "windows.json"), "w") as f:
            json.dump({"windows": windows}, f)

    def req(self, action="restart_container", target="zigbee2mqtt", inc=INC):
        return {"action_type": action, "target": target, "incident_id": inc}

    def close(self):
        self.tmp.cleanup()


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.now = time.time()

    def tearDown(self):
        self.env.close()

    def handle(self, **kw):
        return X.handle(self.env.cfg, self.env.req(**kw), self.now)

    def refused(self, why, **kw):
        with self.assertRaises(X.Refused) as cm:
            self.handle(**kw)
        self.assertEqual(cm.exception.why, why, cm.exception.detail)

    def test_restart(self):
        self.assertEqual(self.handle(), "restarted zigbee2mqtt")
        self.assertEqual(self.env.ran[0][:1], ("restart",))

    def test_same_lists_as_router(self):
        self.assertEqual(set(X.ACTIONS), set(router_config.ALLOWED_ACTIONS))
        self.assertEqual(set(X.HARD_DENIED_TARGETS), set(router_config.HARD_DENIED_TARGETS))

    def test_only_exact_requests(self):
        for bad in ({}, {"action_type": "restart_container", "target": "x"},
                    dict(self.env.req(), extra="1"), dict(self.env.req(), action_type="shell"),
                    dict(self.env.req(), target="gitlab"), dict(self.env.req(), target="a;b"),
                    dict(self.env.req(), incident_id="x")):
            with self.assertRaises(X.Refused):
                X.handle(self.env.cfg, bad, self.now)
        self.refused("not_allowed", target="mosquitto")          # config denylist
        self.assertEqual(self.env.ran, [])

    def test_kill_switch(self):
        with open(self.env.cfg["override_file"], "w") as f:
            f.write("notify-only\n")
        self.refused("kill_switch")
        os.unlink(self.env.cfg["override_file"])
        with open(os.path.join(self.env.shared, "runtime_mode"), "w") as f:
            json.dump({"mode": "shadow"}, f)
        self.refused("kill_switch")
        with open(os.path.join(self.env.shared, "runtime_mode"), "w") as f:
            f.write("{garbage")
        self.refused("kill_switch")

    def test_maintenance_includes_dependents(self):
        self.env.write_windows([{"id": "maint-1", "scope": ["mosquitto"], "expires_at": self.now + 60}])
        self.refused("maintenance")
        self.env.write_windows([{"id": "maint-1", "scope": ["mosquitto"], "expires_at": self.now - 1}])
        self.handle()

    def test_windows_unreadable_fails_closed(self):
        os.unlink(os.path.join(self.env.shared, "windows.json"))
        self.refused("maintenance")

    def test_dependency_down(self):
        self.env.down = {"mosquitto"}
        self.refused("dependency")

    def test_deploy(self):
        self.env.deploys = ["router:deploy"]
        self.refused("deploy")

    def test_cancel_orphan_ignores_deploy_lock(self):
        self.env.deploys = ["router:deploy"]
        self.assertEqual(self.handle(action="cancel_ci_job", target="101"), "cancelled job 101")

    def test_cancel_refuses_young_or_other_jobs(self):
        self.env.job = {"status": "running", "name": "router:deploy",
                        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now - 60))}
        self.refused("not_allowed", action="cancel_ci_job", target="101")
        self.env.job = {"status": "running", "name": "netgear:firmware", "started_at": "2026-01-01T00:00:00Z"}
        self.refused("not_allowed", action="cancel_ci_job", target="101")

    def test_never_twice_per_incident_and_rate_limits(self):
        self.handle()
        self.refused("duplicate")
        self.handle(inc="inc-0000000000a1")
        self.refused("rate_limit", inc="inc-0000000000a2")       # 2 per target per 6h
        for i in range(3, 7):
            self.handle(target=f"svc{i}", inc=f"inc-0000000000b{i}")
        self.refused("rate_limit", target="svc9", inc="inc-0000000000c1")   # 6 per hour

    @given(st.dictionaries(st.text(max_size=15), st.one_of(st.text(max_size=40), st.integers()), max_size=5))
    @settings(max_examples=200, deadline=None)
    def test_junk_requests_never_act(self, req):
        try:
            X.handle(self.env.cfg, req, self.now)
        except X.Refused:
            pass
        self.assertEqual(self.env.ran, [])

    @unittest.skipUnless(sys.platform.startswith("linux"), "SO_PEERCRED is Linux-only")
    def test_peer_credentials(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with a, b:
            pid, uid, _ = X.peer_uid(a)
            self.assertEqual((pid, uid), (os.getpid(), os.getuid()))


class TestLoadConfig(unittest.TestCase):
    def test_vars_and_durations(self):
        with tempfile.TemporaryDirectory() as d:
            deps = os.path.join(d, "deps.yml")
            with open(deps, "w") as f:
                f.write('nodes:\n  slzb:\n    kind: device\n    probe: {tcp: "{{ zigbee_radio_address }}:7638"}\n'
                        '  z2m:\n    kind: container\n    needs: [slzb]\n    probe: {container: z2m}\n')
            cfgp = os.path.join(d, "executor.json")
            with open(cfgp, "w") as f:
                json.dump({"dependencies_file": deps, "vars": {"zigbee_radio_address": "192.0.2.7"},
                           "gitlab": {"orphan_jobs": {"router:deploy": "45m"}, "grace": "5m"}}, f)
            cfg = X.load_config(cfgp)
        self.assertEqual(cfg["dependencies"]["slzb"]["probe"], {"tcp": "192.0.2.7:7638"})
        self.assertEqual(cfg["gitlab"]["orphan_jobs"]["router:deploy"], 2700)
        self.assertEqual(X.ancestors(cfg, "z2m"), {"slzb"})


class TestControl(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_effective_mode_matches_executor(self):
        cfg = self.env.cfg
        for host in (None, "notify-only", "shadow", "active", "junk"):
            for rt in (None, "notify-only", "shadow"):
                C.write_override(cfg, host)
                rpath = os.path.join(cfg["shared_dir"], "runtime_mode")
                if rt:
                    with open(rpath, "w") as f:
                        json.dump({"mode": rt, "at": 0}, f)
                elif os.path.exists(rpath):
                    os.unlink(rpath)
                self.assertEqual(C.effective(cfg), X.effective_mode(cfg), (host, rt))

    def test_stop_and_resume(self):
        cfg = self.env.cfg
        C.write_override(cfg, "notify-only")
        self.assertEqual(C.effective(cfg), "notify-only")
        with open(os.path.join(cfg["shared_dir"], "runtime_mode"), "w") as f:
            json.dump({"mode": "notify-only", "at": 0}, f)
        C.write_override(cfg, None)
        C.clear_runtime(cfg)
        self.assertEqual(C.effective(cfg), "active")


if __name__ == "__main__":
    unittest.main()
