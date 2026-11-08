"""Configuration and the playbook registry (sections 4.4, 4.5, 7).

`validate()` is also the CI validator (`python -m router validate`): it
rejects unknown keys, actions outside the allowlist, hard-denied action types
and targets, unknown nodes and alert rules, and overlapping matches.
"""

import fnmatch
import glob
import os
import re

import yaml

from .modes import MODES
from .util import parse_duration

# Action types the executor implements (4.5). Anything else is rejected; the
# rest of the allowlist (restart_systemd_unit, run_pipeline_schedule,
# collect, cycle_pdu_outlet) is added with the executor support for it, never
# here alone.
ALLOWED_ACTIONS = ("restart_container", "cancel_ci_job", "retry_ci_job")
# CI actions: they act on GitLab jobs, not on the host, so the deploy lock
# (S7) does not hold them back: cancelling frees the lock, retrying queues.
CI_ACTIONS = ("cancel_ci_job", "retry_ci_job")
# What an outcome record's `fix` may name (8.2): the whole allowlist of 4.5,
# including types no playbook can use yet, so the agent can say "this would
# have been a restart_systemd_unit" and the review can propose the executor
# support for it.
OUTCOME_FIX_ACTIONS = ALLOWED_ACTIONS + ("restart_systemd_unit", "run_pipeline_schedule", "collect")

# Hard-denied (4.5): not expressible, whatever the data says. Kept in code,
# not in config, so no learned change can touch them (8.3).
HARD_DENIED_ACTIONS = (
    "power_off", "shutdown", "reboot", "ups_command", "cycle_pdu_outlet",
    "ha_service_call", "lock", "unlock", "door", "garage", "alarm",
    "router_reboot", "switch_port", "s2500_uplink", "firewall", "iptables",
    "delete", "delete_volume", "delete_snapshot", "restic_unlock",
    "restic_forget", "prune", "zpool", "zfs_destroy", "vault_unseal",
    "unban", "clear_ban", "docker_rm", "compose_down",
)
# Never restarted by any playbook: decision 7 (GitLab, Vault, Pi-hole), the
# router itself and the socket proxy. config.yml's restart_denylist must
# contain at least these.
HARD_DENIED_TARGETS = ("gitlab", "vault", "pihole", "event-router", "docker-socket-proxy")
# Throwaway containers, never restarted by any playbook: gitlab-runner's
# docker executor (runner-<token>-project-N-concurrent-M-...-build) and test
# containers. Restarting one is meaningless at best and can wedge a CI job.
# Glob patterns; the executor has the same list.
EPHEMERAL_TARGETS = ("runner-*", "canary-*", "e2e-*")

TARGET_LABELS = ("container_name", "container")   # labels a target may come from
SOURCES = ("grafana", "reconciler", "gitlab")
KINDS = ("alert", "ci_orphan", "ci_orphan_cancelled", "pipeline_failed", "ci_job_failed")
# Kinds whose action target is a job id taken from the event.
JOB_KINDS = {"cancel_ci_job": ("ci_orphan",), "retry_ci_job": ("ci_job_failed", "ci_orphan_cancelled")}
PB_KEYS = {"id", "description", "mode", "enabled", "match", "node",
           "preconditions", "action", "verify", "limits", "on_success",
           "on_failure"}
VERIFY_KINDS = ("alert_resolved", "ci_job_finished", "ci_job_succeeded")
# Checked by the executor on the host, where the facts are (14, phase 3):
# no_deploy_touching: the target container was not (re)created by a deploy
# in this span (compose recreates exactly the services a deploy changed).
PRECONDITIONS = ("no_deploy_touching",)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def ephemeral(name):
    """The EPHEMERAL_TARGETS pattern a container name matches, or None."""
    for pattern in EPHEMERAL_TARGETS:
        if fnmatch.fnmatchcase(name or "", pattern):
            return pattern
    return None


class ConfigError(ValueError):
    pass


def _dur(v, what):
    try:
        return parse_duration(v)
    except ValueError as e:
        raise ConfigError(f"{what}: {e}") from None


def load_config(path):
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw)


def parse_config(raw):
    known = {"mode", "limits", "correlation", "maintenance", "quiet_hours",
             "reconcile_interval", "step_interval", "restart_denylist",
             "page_classes", "ci_orphan", "deploy_jobs", "verify_grace",
             "openclaw", "gitlab_events", "digest_only"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"config: unknown keys {sorted(unknown)}")
    if raw.get("mode") not in MODES:
        raise ConfigError(f"config: mode must be one of {MODES}")
    lim = raw.get("limits") or {}
    cfg = {
        "mode": raw["mode"],
        "global_actions_per_hour": int(lim.get("global_actions_per_hour", 6)),
        "default_limits": _limits(lim.get("default") or {}, "limits.default",
                                  {"max_runs": 1, "per": "6h", "cooldown": "30m"}),
        "breaker_reset_after": _dur(lim.get("breaker_reset_after", "24h"), "limits.breaker_reset_after"),
        "burst_events": int((raw.get("correlation") or {}).get("burst_events", 5)),
        "burst_within": _dur((raw.get("correlation") or {}).get("burst_within", "2m"), "correlation.burst_within"),
        "max_window": _dur((raw.get("maintenance") or {}).get("max_duration", "12h"), "maintenance.max_duration"),
        "reconcile_interval": _dur(raw.get("reconcile_interval", "5m"), "reconcile_interval"),
        "step_interval": _dur(raw.get("step_interval", "15s"), "step_interval"),
        "restart_denylist": list(raw.get("restart_denylist") or []),
        "quiet_hours": raw.get("quiet_hours") or {},
        "page_classes": raw.get("page_classes") or {},
        "ci_orphan": raw.get("ci_orphan") or {},
        "deploy_jobs": list(raw.get("deploy_jobs") or []),
    }
    cfg["openclaw"] = _openclaw(raw.get("openclaw") or {})
    cfg["gitlab_events"] = _gitlab_events(raw.get("gitlab_events") or {})
    cfg["digest_only"] = [_selector(r, f"digest_only[{i}]", extra={"reason"})
                          for i, r in enumerate(raw.get("digest_only") or [])]
    for i, r in enumerate(cfg["digest_only"]):
        if not r.get("reason"):
            raise ConfigError(f"digest_only[{i}]: needs a reason")
    missing = set(HARD_DENIED_TARGETS) - set(cfg["restart_denylist"])
    if missing:
        raise ConfigError(f"config: restart_denylist must include {sorted(missing)}")
    for name, cls in cfg["page_classes"].items():
        if set(cls) - {"urgent", "rules"}:
            raise ConfigError(f"page_classes.{name}: unknown keys")
        for r in cls.get("rules") or []:
            if set(r) - {"rule_uid", "labels"} or "rule_uid" not in r:
                raise ConfigError(f"page_classes.{name}: each rule needs rule_uid (and optional labels)")
    jobs = cfg["ci_orphan"].get("jobs") or {}
    cfg["ci_orphan"] = {
        "project_id": int(cfg["ci_orphan"].get("project_id", 4)),
        "jobs": {str(k): _dur(v, f"ci_orphan.jobs.{k}") for k, v in jobs.items()},
        "grace": _dur(cfg["ci_orphan"].get("grace", "5m"), "ci_orphan.grace"),
    }
    return cfg


def _openclaw(raw):
    """Residue forwarding to OpenClaw (phase 3, doc 6.1 and 7.4). With
    `forward: false` the router behaves as in phases 1-2: OpenClaw gets every
    alert straight from Grafana and the router only records."""
    keys = {"forward", "url", "max_age", "jit_after", "ack_window"}
    if set(raw) - keys:
        raise ConfigError(f"openclaw: unknown keys {sorted(set(raw) - keys)}")
    if not isinstance(raw.get("forward", False), bool):
        raise ConfigError("openclaw.forward must be true or false")
    return {"forward": raw.get("forward", False),
            "url": str(raw.get("url") or "http://openclaw-gateway:18789/hooks/event-router"),
            "max_age": _dur(raw.get("max_age", "6h"), "openclaw.max_age"),
            "jit_after": _dur(raw.get("jit_after", "2m"), "openclaw.jit_after"),
            "ack_window": _dur(raw.get("ack_window", "6h"), "openclaw.ack_window")}


def _gitlab_events(raw):
    keys = {"projects", "branch"}
    if set(raw) - keys:
        raise ConfigError(f"gitlab_events: unknown keys {sorted(set(raw) - keys)}")
    projects = raw.get("projects") or [4]
    if not all(isinstance(p, int) and not isinstance(p, bool) for p in projects):
        raise ConfigError("gitlab_events.projects must be project ids")
    return {"projects": list(projects), "branch": str(raw.get("branch") or "main")}


def _selector(raw, what, extra=()):
    """{source, kind, rule_uid, labels}: exact values, as in playbook matches."""
    keys = {"source", "kind", "rule_uid", "labels"} | set(extra)
    if not isinstance(raw, dict) or set(raw) - keys:
        raise ConfigError(f"{what}: takes {sorted(keys)}")
    if raw.get("source") not in SOURCES:
        raise ConfigError(f"{what}: source must be one of {SOURCES}")
    if raw.get("kind", "alert") not in KINDS:
        raise ConfigError(f"{what}: kind must be one of {KINDS}")
    labels = raw.get("labels") or {}
    if not isinstance(labels, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                for k, v in labels.items()):
        raise ConfigError(f"{what}: labels must map names to exact string values")
    if raw.get("source") == "grafana" and not raw.get("rule_uid"):
        raise ConfigError(f"{what}: a grafana selector needs rule_uid")
    return dict(raw, kind=raw.get("kind", "alert"), labels=labels, rule_uid=raw.get("rule_uid", ""))


def selector_matches(sel, ev):
    if ev.source != sel["source"] or ev.kind != sel["kind"]:
        return False
    if sel["rule_uid"] and ev.rule_uid != sel["rule_uid"]:
        return False
    return all(ev.labels.get(k) == v for k, v in sel["labels"].items())


def _limits(raw, what, default=None):
    raw = dict(default or {}, **(raw or {}))
    if set(raw) - {"max_runs", "per", "cooldown"}:
        raise ConfigError(f"{what}: unknown keys {sorted(set(raw) - {'max_runs', 'per', 'cooldown'})}")
    try:
        max_runs = int(raw["max_runs"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError(f"{what}: max_runs must be an integer") from None
    if max_runs < 1:
        raise ConfigError(f"{what}: max_runs must be >= 1")
    return {"max_runs": max_runs, "per": _dur(raw.get("per"), f"{what}.per"),
            "cooldown": _dur(raw.get("cooldown", "0s"), f"{what}.cooldown")}


class Playbook:
    def __init__(self, raw, source=""):
        self.raw = raw
        self.source = source
        if not isinstance(raw, dict):
            raise ConfigError(f"{source}: a playbook must be a mapping")
        unknown = set(raw) - PB_KEYS
        if unknown:
            raise ConfigError(f"{source}: unknown keys {sorted(unknown)}")
        self.id = raw.get("id")
        if not isinstance(self.id, str) or not _NAME.match(self.id):
            raise ConfigError(f"{source}: id must be a lowercase name")
        where = f"playbook {self.id}"
        self.description = str(raw.get("description") or "")
        self.mode = raw.get("mode", "shadow")
        if self.mode not in ("shadow", "active"):
            raise ConfigError(f"{where}: mode must be shadow or active")
        self.enabled = raw.get("enabled", True)
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"{where}: enabled must be true or false")

        m = raw.get("match")
        if not isinstance(m, dict) or set(m) - {"source", "kind", "rule_uid", "labels"}:
            raise ConfigError(f"{where}: match takes source, kind, rule_uid, labels")
        self.match_source = m.get("source")
        if self.match_source not in SOURCES:
            raise ConfigError(f"{where}: match.source must be one of {SOURCES}")
        self.match_kind = m.get("kind", "alert")
        if self.match_kind not in KINDS:
            raise ConfigError(f"{where}: match.kind must be one of {KINDS}")
        self.match_rule = m.get("rule_uid", "")
        if self.match_source == "grafana" and not self.match_rule:
            raise ConfigError(f"{where}: a grafana match needs rule_uid")
        self.match_labels = m.get("labels") or {}
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in self.match_labels.items()):
            raise ConfigError(f"{where}: match.labels must map names to exact string values")

        node = raw.get("node")
        if isinstance(node, dict):
            if set(node) != {"from_label"} or node["from_label"] not in TARGET_LABELS:
                raise ConfigError(f"{where}: node.from_label must be one of {TARGET_LABELS}")
            self.node, self.node_label = None, node["from_label"]
        elif isinstance(node, str) and node:
            self.node, self.node_label = node, None
        else:
            raise ConfigError(f"{where}: node is required")

        self.preconditions = []
        for p in raw.get("preconditions") or []:
            if not isinstance(p, dict) or len(p) != 1 or next(iter(p)) not in PRECONDITIONS:
                raise ConfigError(f"{where}: preconditions take only {PRECONDITIONS}")
            k, v = next(iter(p.items()))
            self.preconditions.append((k, _dur(v, f"{where}: {k}")))
        if self.preconditions and (raw.get("action") or {}).get("type") != "restart_container":
            raise ConfigError(f"{where}: no_deploy_touching applies to restart_container")

        a = raw.get("action")
        if not isinstance(a, dict) or set(a) - {"type", "target", "target_from_label", "target_from_event"}:
            raise ConfigError(f"{where}: action takes type and one target field")
        self.action = a.get("type")
        if self.action in HARD_DENIED_ACTIONS:
            raise ConfigError(f"{where}: action {self.action} is hard-denied")
        if self.action not in ALLOWED_ACTIONS:
            raise ConfigError(f"{where}: action {self.action!r} is not in the allowlist {ALLOWED_ACTIONS}")
        targets = [k for k in ("target", "target_from_label", "target_from_event") if k in a]
        if len(targets) != 1:
            raise ConfigError(f"{where}: exactly one of target, target_from_label, target_from_event")
        self.target = a.get("target")
        self.target_label = a.get("target_from_label")
        self.target_event = a.get("target_from_event")
        if self.action == "restart_container":
            if self.target_event:
                raise ConfigError(f"{where}: restart_container takes target or target_from_label")
            if self.target_label and self.target_label not in TARGET_LABELS:
                raise ConfigError(f"{where}: target_from_label must be one of {TARGET_LABELS}")
            if self.target is not None and (not isinstance(self.target, str) or not _NAME.match(self.target)):
                raise ConfigError(f"{where}: target must be a container name")
            if self.target in HARD_DENIED_TARGETS:
                raise ConfigError(f"{where}: target {self.target} is hard-denied")
            if self.target and ephemeral(self.target):
                raise ConfigError(f"{where}: target {self.target} is an ephemeral container")
        if self.action in JOB_KINDS:
            kinds = JOB_KINDS[self.action]
            if self.target_event != "job_id" or self.match_kind not in kinds:
                raise ConfigError(f"{where}: {self.action} takes target_from_event: job_id and matches kind "
                                  f"{' or '.join(kinds)}")
        elif self.target_event:
            raise ConfigError(f"{where}: target_from_event is only for CI job actions")

        v = raw.get("verify")
        if not isinstance(v, dict) or len(v) != 1 or next(iter(v)) not in VERIFY_KINDS:
            raise ConfigError(f"{where}: verify is mandatory, one of {VERIFY_KINDS}")
        self.verify_kind, vt = next(iter(v.items()))
        if (self.verify_kind == "alert_resolved") != (self.match_source == "grafana"):
            raise ConfigError(f"{where}: alert_resolved verifies grafana alerts; CI playbooks use "
                              f"ci_job_finished or ci_job_succeeded")
        self.verify_within = _dur(vt, f"{where}: verify")
        if "limits" not in raw:
            raise ConfigError(f"{where}: limits are mandatory")
        self.limits = _limits(raw["limits"], f"{where}: limits")
        self.on_success = (raw.get("on_success") or {"notify": "digest"}).get("notify", "digest")
        self.on_failure = (raw.get("on_failure") or {"notify": "page"}).get("notify", "page")
        for k, val in (("on_success", self.on_success), ("on_failure", self.on_failure)):
            if val not in ("digest", "page", "agent"):
                raise ConfigError(f"{where}: {k}.notify must be digest, page or agent")

    def matches(self, ev):
        if not self.enabled:
            return False
        if ev.source != self.match_source or ev.kind != self.match_kind:
            return False
        if self.match_rule and ev.rule_uid != self.match_rule:
            return False
        return all(ev.labels.get(k) == v for k, v in self.match_labels.items())

    def node_for(self, ev, graph):
        if self.node:
            return self.node
        value = ev.labels.get(self.node_label, "")
        return graph.node_for_container(value) if value else ""

    def target_for(self, ev):
        """The action target: fixed, or a label/event field validated as a
        plain name. Never free text."""
        if self.target:
            return self.target
        if self.target_label:
            value = ev.labels.get(self.target_label, "")
            return value if _NAME.match(value or "") else ""
        value = ev.labels.get(self.target_event, "")
        return value if re.match(r"^[0-9]{1,12}$", value or "") else ""


def load_playbooks(directory):
    pbs = []
    for path in sorted(glob.glob(os.path.join(directory, "*.yml"))):
        with open(path) as f:
            data = yaml.safe_load(f)
        items = data if isinstance(data, list) else [data]
        for raw in items:
            pbs.append(Playbook(raw, os.path.basename(path)))
    ids = [p.id for p in pbs]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate playbook ids: {sorted(dupes)}")
    return pbs


def match_playbook(pbs, ev):
    """One event matches at most one playbook (enforced by validate())."""
    hits = [p for p in pbs if p.matches(ev)]
    return hits[0] if hits else None


def _overlap(a, b):
    if (a.match_source, a.match_kind, a.match_rule) != (b.match_source, b.match_kind, b.match_rule):
        return False
    shared = set(a.match_labels) & set(b.match_labels)
    return all(a.match_labels[k] == b.match_labels[k] for k in shared)


def rule_uids(rules_dir):
    uids = set()
    for path in glob.glob(os.path.join(rules_dir, "*.yml")):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        for g in data.get("groups") or []:
            for r in g.get("rules") or []:
                if r.get("uid"):
                    uids.add(r["uid"])
    return uids


def compose_services(path):
    """Service and container names in a (Jinja-templated) compose file."""
    names = set()
    in_services = False
    with open(path) as f:
        lines = f.readlines()
    for line in lines:
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and re.match(r"^\S", line) and not line.startswith("{"):
            in_services = False
        m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if in_services and m:
            names.add(m.group(1))
        m = re.match(r"^\s+container_name:\s*['\"]?([A-Za-z0-9_.-]+)", line)
        if m:
            names.add(m.group(1))
    return names


def validate(cfg, pbs, graph, rules=None, services=None):
    """All problems as a list of strings (empty = valid)."""
    errs = list(graph.errors())
    for p in pbs:
        where = f"playbook {p.id} ({p.source})"
        if p.node and p.node not in graph.nodes:
            errs.append(f"{where}: node {p.node} is not in dependencies.yml")
        if rules is not None and p.match_rule and p.match_rule not in rules:
            errs.append(f"{where}: rule_uid {p.match_rule} is not an alert rule")
        if p.action == "restart_container" and p.target:
            if p.target in cfg["restart_denylist"]:
                errs.append(f"{where}: target {p.target} is on the restart denylist")
            if services is not None and p.target not in services:
                errs.append(f"{where}: target {p.target} is not a compose service")
    for i, a in enumerate(pbs):
        for b in pbs[i + 1:]:
            if _overlap(a, b):
                errs.append(f"playbooks {a.id} and {b.id} can match the same event")
    if rules is not None:
        for name, cls in cfg["page_classes"].items():
            for r in cls.get("rules") or []:
                if r["rule_uid"] not in rules:
                    errs.append(f"page_classes.{name}: rule_uid {r['rule_uid']} is not an alert rule")
    for sel in cfg["digest_only"]:
        if rules is not None and sel["rule_uid"] and sel["rule_uid"] not in rules:
            errs.append(f"digest_only: rule_uid {sel['rule_uid']} is not an alert rule")
        if sel["rule_uid"] in page_rule_uids(cfg):
            errs.append(f"digest_only: {sel['rule_uid']} is a page class; it cannot be digest-only")
        hits = [p.id for p in pbs if p.match_source == sel["source"] and p.match_kind == sel["kind"]
                and p.match_rule == sel["rule_uid"]
                and all(p.match_labels.get(k, v) == v for k, v in sel["labels"].items())]
        if hits:
            errs.append(f"digest_only {sel}: overlaps playbooks {hits}")
    for pattern in cfg["deploy_jobs"]:
        if not isinstance(pattern, str) or not pattern:
            errs.append("deploy_jobs: entries must be job names")
    return errs


def page_class(cfg, ev):
    """(class name, urgent) if the event is in an always-page class (6.3)."""
    for name, cls in cfg["page_classes"].items():
        for r in cls.get("rules") or []:
            if ev.rule_uid == r["rule_uid"] and all(
                    fnmatch.fnmatchcase(ev.labels.get(k, ""), v)
                    for k, v in (r.get("labels") or {}).items()):
                return name, bool(cls.get("urgent"))
    return None, False


def page_rule_uids(cfg):
    return {r["rule_uid"] for cls in cfg["page_classes"].values() for r in cls.get("rules") or []}


def digest_only(cfg, ev):
    for sel in cfg["digest_only"]:
        if selector_matches(sel, ev):
            return sel
    return None
