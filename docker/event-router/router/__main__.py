"""event-router entry point.

  python -m router serve
  python -m router validate --config config.yml --playbooks playbooks \
      --deps ../../inventory/dependencies.yml [--rules DIR] [--compose FILE] [--suppressions FILE]
  python -m router maint open --scope zigbee2mqtt,mqtt --for 2h --reason "..." [--by NAME]
  python -m router maint close <id> | maint list
  python -m router mode show | mode set notify-only|shadow|clear
  python -m router replay payload.json [...]   (shadow decisions on a scratch database)
  python -m router inject-ci-orphan <job_id> [--resolved]   (test event for ci-orphan-cancel)

`maint` and `mode` work on the live database; on the router they run inside
the container (`scripts/event-router`).
"""

import argparse
import json
import os
import sys
import tempfile
import time

from . import suppressions
from .config import (ConfigError, compose_services, load_config, load_playbooks,
                     rule_uids, validate)
from .deps import Graph
from .engine import Engine
from .events import from_grafana
from .shared import SharedState
from .store import Store
from .util import Clock, parse_duration

ENV = os.environ.get


def _paths():
    return dict(config=ENV("CONFIG", "/app/config.yml"), playbooks=ENV("PLAYBOOKS", "/app/playbooks"),
                deps=ENV("DEPENDENCIES", "/config/dependencies.yml"), db=ENV("DB", "/data/router.db"),
                shared=ENV("SHARED_DIR", "/data/shared"), override=ENV("HOST_OVERRIDE", "/override/mode"),
                suppressions=ENV("SUPPRESSIONS", "/app/suppressions.yml"))


def build_engine(p, with_clients=True, out=None):
    from .clients import ExecutorClient, GitLab, OpenClaw
    cfg = load_config(p["config"])
    pbs = load_playbooks(p["playbooks"])
    graph = Graph.load(p["deps"])
    sups = suppressions.load(p["suppressions"])
    errs = validate(cfg, pbs, graph)
    if errs:
        raise ConfigError("; ".join(errs))
    executor = gitlab = openclaw = None
    if with_clients:
        sock = ENV("EXECUTOR_SOCKET", "/run/event-router/executor.sock")
        executor = ExecutorClient(sock) if os.path.exists(sock) else None
        if ENV("GITLAB_TOKEN"):
            gitlab = GitLab(ENV("GITLAB_URL", "https://gitlab.lab.nkontur.com"), ENV("GITLAB_TOKEN"),
                            cfg["ci_orphan"]["project_id"])
        if ENV("OPENCLAW_HOOKS_TOKEN"):
            openclaw = OpenClaw(cfg["openclaw"]["url"], ENV("OPENCLAW_HOOKS_TOKEN"))
    return Engine(cfg, pbs, graph, Store(p["db"]), Clock(), executor=executor, gitlab=gitlab,
                  host_override_path=p["override"], shared=SharedState(p["shared"]), out=out,
                  openclaw=openclaw, suppressions=sups)


def cmd_serve(_args):
    from .server import State, serve
    p = _paths()
    eng = build_engine(p)
    state = State(eng, ENV("GRAFANA_TOKEN"), ENV("CONTROL_TOKEN"),
                  [c.strip() for c in (ENV("ALLOWED_CLIENTS") or "").split(",")],
                  gitlab_token=ENV("GITLAB_WEBHOOK_TOKEN"))
    serve(state, int(ENV("PORT", "8080")))
    eng._log({"decision": "started", "executor": bool(eng.executor), "gitlab": bool(eng.gitlab),
              "openclaw": bool(eng.openclaw), "forwarding": eng.forwarding,
              "suppressions": len(eng.suppressions),
              "playbooks": {pid: pb.mode for pid, pb in eng.playbooks.items()}})
    deps_mtime = os.stat(p["deps"]).st_mtime
    while True:
        state.heartbeat = time.time()
        try:
            m = os.stat(p["deps"]).st_mtime
            if m != deps_mtime:
                eng.graph, deps_mtime = Graph.load(p["deps"]), m
                eng._log({"decision": "dependencies_reloaded"})
            eng.tick()
        except Exception as e:      # never die on one bad decision; log it
            eng._log({"decision": "error", "error": f"{e.__class__.__name__}: {e}"})
        time.sleep(eng.cfg["step_interval"])


def cmd_validate(a):
    try:
        cfg = load_config(a.config)
        pbs = load_playbooks(a.playbooks)
        graph = Graph.load(a.deps)
        errs = validate(cfg, pbs, graph,
                        rules=rule_uids(a.rules) if a.rules else None,
                        services=compose_services(a.compose) if a.compose else None)
        if a.suppressions:
            sups = suppressions.load(a.suppressions)
            errs += suppressions.validate(sups, cfg)
            rules = rule_uids(a.rules) if a.rules else None
            errs += [f"suppression {s['id']}: rule_uid {s['match']['rule_uid']} is not an alert rule"
                     for s in sups if rules is not None and s["match"]["rule_uid"]
                     and s["match"]["rule_uid"] not in rules]
    except (ConfigError, OSError) as e:
        errs = [str(e)]
    for e in errs:
        print(f"ERROR: {e}")
    if errs:
        return 1
    print(f"ok: {len(pbs)} playbooks, mode {cfg['mode']}: "
          + ", ".join(f"{p.id}={p.mode}" for p in pbs))
    return 0


def cmd_maint(a):
    eng = build_engine(_paths(), with_clients=False)
    if a.op == "list":
        print(json.dumps(eng.store.windows(), indent=2))
    elif a.op == "open":
        wid = eng.open_window([s for s in a.scope.split(",") if s], parse_duration(a.duration),
                              a.reason, a.by, mode=a.mode,
                              playbooks=[s for s in (a.playbooks or "").split(",") if s])
        print(wid)
    elif a.op == "close":
        eng.close_window(a.id, a.by)
    return 0


def cmd_mode(a):
    eng = build_engine(_paths(), with_clients=False)
    if a.op == "set":
        eng.set_runtime_mode(a.value, a.by)
    print(json.dumps({"mode": eng.mode(), "repo": eng.cfg["mode"], "host_override": eng.host_override(),
                      "runtime_override": eng.shared.runtime_mode()}))
    return 0


def cmd_replay(a):
    p = _paths()
    with tempfile.TemporaryDirectory() as d:
        p = dict(p, db=os.path.join(d, "replay.db"), override=os.path.join(d, "none"),
                 shared=os.path.join(d, "shared"))
        eng = build_engine(p, with_clients=False)
        eng.cfg = dict(eng.cfg, mode="shadow")
        for path in a.files:
            with open(path) as f:
                for ev in from_grafana(json.load(f)):
                    eng.ingest(ev)
            eng.step()
    return 0


def cmd_inject(a):
    """Queue a synthetic reconciler event, as the 5-minute check would raise
    it, to exercise ci-orphan-cancel. The executor still checks the job
    against GitLab before cancelling anything."""
    from .events import Event
    eng = build_engine(_paths(), with_clients=False)
    ev = Event(source="reconciler", kind="ci_orphan", fp=f"reconciler:ci-orphan:{int(a.job_id)}",
               status="resolved" if a.resolved else "firing",
               alertname="CI job holding a resource group (test)",
               labels={"job_id": str(int(a.job_id)), "job_name": "test", "why": "injected test event"})
    eng.store.inbox_put(time.time(), [ev.to_dict()])
    print("queued", ev.fp, ev.status)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="router")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    v = sub.add_parser("validate")
    v.add_argument("--config", required=True)
    v.add_argument("--playbooks", required=True)
    v.add_argument("--deps", required=True)
    v.add_argument("--rules")
    v.add_argument("--compose")
    v.add_argument("--suppressions")
    m = sub.add_parser("maint")
    m.add_argument("op", choices=["open", "close", "list"])
    m.add_argument("id", nargs="?")
    m.add_argument("--scope", default="")
    m.add_argument("--for", dest="duration", default="1h")
    m.add_argument("--reason", default="")
    m.add_argument("--mode", default="quiet", choices=["quiet", "assist"])
    m.add_argument("--playbooks", default="")
    m.add_argument("--by", default="cli")
    md = sub.add_parser("mode")
    md.add_argument("op", choices=["show", "set"])
    md.add_argument("value", nargs="?")
    md.add_argument("--by", default="cli")
    r = sub.add_parser("replay")
    r.add_argument("files", nargs="+")
    inj = sub.add_parser("inject-ci-orphan")
    inj.add_argument("job_id")
    inj.add_argument("--resolved", action="store_true")
    a = ap.parse_args(argv)
    try:
        return {"serve": cmd_serve, "validate": cmd_validate, "maint": cmd_maint,
                "mode": cmd_mode, "replay": cmd_replay,
                "inject-ci-orphan": cmd_inject}[a.cmd](a) or 0
    except (ValueError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
