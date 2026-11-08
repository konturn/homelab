"""Fixtures: a small dependency graph and playbook set shaped like the TLA+
instance (mqtt <- zigbee2mqtt, two playbooks on one container, a page class)."""

import io
import os
import tempfile

from router.config import Playbook, parse_config
from router.deps import Graph
from router.engine import Engine
from router.events import Event
from router.shared import SharedState
from router.store import Store
from router.util import FakeClock

GRAPH = {"nodes": {
    "persistent_data": {"kind": "mount"},
    "gitlab": {"kind": "container", "needs": ["persistent_data"], "probe": {"container": "gitlab"}},
    "mosquitto": {"kind": "container", "probe": {"container": "mosquitto"}},
    "mqtt": {"kind": "endpoint", "needs": ["mosquitto"]},
    "slzb_coordinator": {"kind": "device"},
    "zigbee2mqtt": {"kind": "container", "needs": ["slzb_coordinator", "mqtt", "persistent_data"],
                    "probe": {"container": "zigbee2mqtt"}},
    "app": {"kind": "container", "needs": ["persistent_data"], "probe": {"container": "app"}},
}}

CONFIG = {
    "mode": "active",
    "limits": {"global_actions_per_hour": 6, "default": {"max_runs": 1, "per": "6h", "cooldown": "30m"},
               "breaker_reset_after": "24h"},
    "correlation": {"burst_events": 50, "burst_within": "2m"},
    "maintenance": {"max_duration": "12h"},
    "restart_denylist": ["gitlab", "vault", "pihole", "event-router", "docker-socket-proxy", "mosquitto"],
    "page_classes": {"security": {"urgent": True, "rules": [{"rule_uid": "failed-ssh-logins"}]}},
    "deploy_jobs": ["router:deploy"],
    "ci_orphan": {"project_id": 4, "grace": "5m", "jobs": {"router:deploy": "45m"}},
}

PLAYBOOKS = [
    {"id": "zigbee-silence-restart", "mode": "active",
     "match": {"source": "grafana", "rule_uid": "zigbee-radio-silence"}, "node": "zigbee2mqtt",
     "action": {"type": "restart_container", "target": "zigbee2mqtt"},
     "verify": {"alert_resolved": "10m"}, "limits": {"max_runs": 1, "per": "6h", "cooldown": "30m"}},
    {"id": "restart-once", "mode": "active",
     "match": {"source": "grafana", "rule_uid": "container-health-failure"},
     "node": {"from_label": "container_name"},
     "action": {"type": "restart_container", "target_from_label": "container_name"},
     "verify": {"alert_resolved": "10m"}, "limits": {"max_runs": 1, "per": "6h", "cooldown": "30m"}},
    {"id": "app-log-restart", "mode": "active",
     "match": {"source": "grafana", "rule_uid": "app-log"}, "node": "app",
     "action": {"type": "restart_container", "target": "app"},
     "verify": {"alert_resolved": "10m"}, "limits": {"max_runs": 1, "per": "6h", "cooldown": "30m"}},
    {"id": "ci-orphan-cancel", "mode": "active",
     "match": {"source": "reconciler", "kind": "ci_orphan"}, "node": "gitlab",
     "action": {"type": "cancel_ci_job", "target_from_event": "job_id"},
     "verify": {"ci_job_finished": "2m"}, "limits": {"max_runs": 2, "per": "24h", "cooldown": "5m"}},
]

# Alerts like the TLA+ Fps: mqtt (no playbook), zig, app and appdup (same
# container, different playbooks), sec (page class).
ALERTS = {
    "mqtt": dict(rule_uid="critical-service-down", labels={"container_name": "mosquitto"}),
    "zig": dict(rule_uid="zigbee-radio-silence", labels={}),
    "app": dict(rule_uid="container-health-failure", labels={"container_name": "app"}),
    "appdup": dict(rule_uid="app-log", labels={}),
    "sec": dict(rule_uid="failed-ssh-logins", labels={}),
}


def event(name, status="firing"):
    a = ALERTS[name]
    labels = dict(a["labels"], alertname=name)
    return Event(source="grafana", kind="alert", fp=f"grafana:{name}", status=status,
                 rule_uid=a["rule_uid"], alertname=name, labels=labels)


class FakeGitLab:
    def __init__(self):
        self.deploying = False
        self.jobs = []            # running jobs
        self.status = {}
        self.last_deploy = 0

    def deploy_running(self, names):
        return self.deploying

    def last_deploy_finished(self, names):
        return self.last_deploy

    def running_jobs(self):
        return list(self.jobs)

    def job_status(self, job_id):
        return self.status.get(str(job_id), "running")


class FakeExecutor:
    """Records every call; `check` (if set) inspects the world at call time."""

    def __init__(self):
        self.calls = []
        self.playbooks = []     # the playbook id sent with each call
        self.reply = {"ok": True, "detail": "done"}
        self.check = None

    def execute(self, action, target, incident_id, playbook):
        if self.check:
            self.check(action, target, incident_id)
        self.calls.append((action, target, incident_id))
        self.playbooks.append(playbook)
        return dict(self.reply)


class FakeOpenClaw:
    """OpenClaw's /hooks/event-router: `result` is what the next delivery
    returns ("ok", "down" or "rejected")."""

    def __init__(self):
        self.result = "ok"
        self.delivered = []
        self.attempts = 0

    def deliver(self, payload):
        self.attempts += 1
        if self.result == "ok":
            self.delivered.append(payload)
        return self.result, "fake"


class World:
    def __init__(self, cfg=None, playbooks=None, suppressions=()):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "router.db")
        self.override = os.path.join(self.tmp.name, "mode")
        self.shared_dir = os.path.join(self.tmp.name, "shared")
        self.clock = FakeClock()
        self.gitlab = FakeGitLab()
        self.executor = FakeExecutor()
        self.openclaw = FakeOpenClaw()
        self.suppressions = list(suppressions)
        self.cfg = parse_config(cfg or CONFIG)
        self.pbs = [Playbook(p, "test") for p in (playbooks or PLAYBOOKS)]
        self.graph = Graph.from_dict(GRAPH)
        self.out = io.StringIO()
        self.engine = self.new_engine()

    def new_engine(self):
        """A fresh process on the same database (a router restart)."""
        if getattr(self, "engine", None) is not None:
            self.engine.store.close()
        self.engine = Engine(self.cfg, self.pbs, self.graph, Store(self.db, durable=False), self.clock,
                             executor=self.executor, gitlab=self.gitlab,
                             host_override_path=self.override, shared=SharedState(self.shared_dir),
                             out=self.out, openclaw=self.openclaw, suppressions=self.suppressions)
        return self.engine

    def set_override(self, mode):
        if mode is None:
            if os.path.exists(self.override):
                os.unlink(self.override)
        else:
            with open(self.override, "w") as f:
                f.write(mode)

    def outbox(self):
        import json
        d = os.path.join(self.shared_dir, "outbox")
        return [json.load(open(os.path.join(d, n))) for n in sorted(os.listdir(d))]

    def decisions(self):
        import json
        return [json.loads(line) for line in self.out.getvalue().splitlines()]

    def close(self):
        self.engine.store.close()
        self.tmp.cleanup()
