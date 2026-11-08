"""Consistency with the rest of the repo (runs in CI from docker/event-router)."""

import os
import unittest

import yaml

from router.config import compose_services, load_config, load_playbooks, rule_uids, validate
from router.deps import Graph
from router.util import parse_duration

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CI = os.path.join(ROOT, ".gitlab-ci.yml")


def ci_jobs():
    class L(yaml.SafeLoader):
        pass
    L.add_constructor("!reference", lambda loader, node: None)
    with open(CI) as f:
        d = yaml.load(f, Loader=L)
    default = (d.get("default") or {}).get("timeout")

    def get(name, key):
        j = d.get(name) or {}
        if key in j:
            return j[key]
        ext = j.get("extends") or []
        for e in ([ext] if isinstance(ext, str) else ext)[::-1]:
            v = get(e, key)
            if v is not None:
                return v
        return None
    return {k: {"timeout": get(k, "timeout") or default, "resource_group": get(k, "resource_group")}
            for k, v in d.items() if isinstance(v, dict) and not k.startswith(".")}


def minutes(v):
    v = str(v).replace(" ", "")
    if v.endswith("h"):
        return int(v[:-1]) * 60
    if v.endswith("m"):
        return int(v[:-1])
    raise ValueError(v)


@unittest.skipUnless(os.path.exists(CI), "needs the full repo")
class TestRepo(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(os.path.join(HERE, "config.yml"))

    def test_playbooks_validate_against_repo(self):
        errs = validate(self.cfg, load_playbooks(os.path.join(HERE, "playbooks")),
                        Graph.load(os.path.join(ROOT, "inventory", "dependencies.yml")),
                        rules=rule_uids(os.path.join(ROOT, "docker", "grafana", "provisioning", "alerting")),
                        services=compose_services(os.path.join(ROOT, "docker", "docker-compose.yml")))
        self.assertEqual(errs, [])

    def test_orphan_timeouts_match_ci(self):
        jobs = ci_jobs()
        for name, timeout in self.cfg["ci_orphan"]["jobs"].items():
            self.assertIn(name, jobs)
            self.assertEqual(jobs[name]["resource_group"], "deploy-router", name)
            self.assertEqual(timeout, minutes(jobs[name]["timeout"]) * 60, name)
        held = {k for k, v in jobs.items() if v["resource_group"] == "deploy-router"}
        self.assertEqual(held, set(self.cfg["ci_orphan"]["jobs"]))
        self.assertEqual(set(self.cfg["deploy_jobs"]), held)

    def test_email_routes_are_the_page_classes(self):
        """Decision 4: Grafana mails page classes only. The email routes in
        policies.yml and page_classes here must name the same (rule uid,
        container, urgent) triples; urgent ones are never muted (decision 6)."""
        import re
        with open(os.path.join(ROOT, "docker", "grafana", "provisioning", "alerting", "policies.yml")) as f:
            pol = yaml.safe_load(f)
        mutes = {m["name"] for m in pol.get("muteTimes") or []}
        routed = set()
        for r in pol["policies"][0]["routes"]:
            if r["receiver"] != "homelab-email-alerts":
                continue
            m = {}
            for matcher in r["matchers"]:
                k, op, v = re.match(r"^(\S+)\s*(=~|=)\s*\"(.*)\"$", matcher).groups()
                m[k] = v.split("|") if op == "=~" else [v]
            self.assertLessEqual(set(m), {"__alert_rule_uid__", "container_name"}, r["matchers"])
            muted = bool(r.get("mute_time_intervals"))
            self.assertLessEqual(set(r.get("mute_time_intervals") or []), mutes)
            for uid in m["__alert_rule_uid__"]:
                for c in m.get("container_name", [None]):
                    routed.add((uid, c, not muted))
        want = set()
        for cls in self.cfg["page_classes"].values():
            for rule in cls["rules"]:
                want.add((rule["rule_uid"], (rule.get("labels") or {}).get("container_name"), bool(cls.get("urgent"))))
        # A route with a container matcher covers every listed container for
        # every listed rule; the config may list fewer pairs than that.
        self.assertLessEqual(want, routed)
        extra = {t for t in routed - want if t[1] is None}
        self.assertEqual(extra, set(), "mailed but not a page class")
        for uid, c, urgent in routed - want:
            self.assertTrue(any(w[1] for w in want if w[0] == uid), (uid, c))

    def test_durations_parse(self):
        self.assertEqual(parse_duration("45m"), self.cfg["ci_orphan"]["jobs"]["router:deploy"])


if __name__ == "__main__":
    unittest.main()
