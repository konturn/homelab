"""Tests for the CI image class (docs/AUTOMERGE.md, "CI images"): job image
lines in .gitlab-ci.yml, the post-merge pipeline watch, the revert and the
rollback hold, and the real policy, renovate.json, CODEOWNERS and CI file.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402
import policy  # noqa: E402
from policy import FileChange  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]
RENOVATE = "project_4_bot_af78a2ea227eba9e10006515fbde375e"
CI = policy.CI_PATH
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64

BASE = f"""default:
  tags:
    - $RUNNER_TAG

variables:
  GIT_DEPTH: 5

.ansible:
  image: willhallonline/ansible:2.21.4-alpine-3.22@{D1}
  before_script:
    - echo hi

nginx:validate:
  stage: validate
  image: nginx:1.31.1-alpine3.23-perl@{D1}
  script:
    - nginx -t

kaniko:build:
  image:
    name: gcr.io/kaniko-project/executor:v1.23.2-debug@{D1}
    entrypoint: [""]
  script:
    - build

.renovate_image: &renovate_image
  name: renovate/renovate:44.167.7@{D1}
  entrypoint: [""]

renovate:
  image: *renovate_image
  script:
    - renovate

.not_an_image: &other
  name: alpine:3.22@{D1}

deploy:
  extends: .ansible
  script:
    - deploy

runner:check:
  image: gitlab/gitlab-runner:v18.5.0@{D1}
  script:
    - true
"""

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH],
    "denylist": {"images": {"gitlab/gitlab-runner": "runs every CI job"}},
    "ci_images": {"enabled": True, "authors": [RENOVATE], "files": [CI],
                  "denylist": {"willhallonline/ansible": "runs the sweep"}},
}
CODEOWNERS = "/.gitlab-ci.yml @root @bot\n/ci/ @root\n"


def edit(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


def evaluate(head, *, base=BASE, author=RENOVATE, allow=ALLOW, files=None, labels=()):
    files = files or [FileChange(path=CI)]
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=list(labels), files=files,
        composes={CI: (base, head)}, allowlist=allow, codeowners_text=CODEOWNERS, merger="bot")


NGINX_989 = (f"nginx:1.31.1-alpine3.23-perl@{D1}", f"nginx:1.31.2-alpine3.23-perl@{D2}")


class EligibilityTests(unittest.TestCase):
    def test_mr_989_nginx_validate_patch(self):
        v = evaluate(edit(BASE, *NGINX_989))
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.ci_image)
        self.assertFalse(v.fast_track)
        self.assertEqual(v.deploy_jobs, [])
        self.assertEqual(v.ci_images, ["nginx"])
        self.assertEqual([(c.service, c.kind) for c in v.changes], [("nginx:validate", "patch")])
        self.assertIn("class: CI image", " ".join(v.notes))

    def test_image_mapping_name(self):
        v = evaluate(edit(BASE, f"executor:v1.23.2-debug@{D1}", f"executor:v1.24.0-debug@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.changes[0].service, "kaniko:build")
        self.assertEqual(v.changes[0].kind, "minor")

    def test_anchored_template_used_as_image(self):
        v = evaluate(edit(BASE, f"renovate:44.167.7@{D1}", f"renovate:44.168.0@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.changes[0].service, ".renovate_image")

    def test_digest_only_and_digest_pin(self):
        v = evaluate(edit(BASE, f"nginx:1.31.1-alpine3.23-perl@{D1}", f"nginx:1.31.1-alpine3.23-perl@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.changes[0].kind, "digest")
        base = edit(BASE, f"nginx:1.31.1-alpine3.23-perl@{D1}", "nginx:1.31.1-alpine3.23-perl")
        v = evaluate(edit(base, "nginx:1.31.1-alpine3.23-perl", f"nginx:1.31.1-alpine3.23-perl@{D2}"), base=base)
        self.assertTrue(v.eligible, v.reasons)

    def test_name_not_under_an_image_is_refused(self):
        v = evaluate(edit(BASE, f"alpine:3.22@{D1}", f"alpine:3.22@{D2}"))
        self.assertFalse(v.eligible)
        self.assertIn("not a job image line", " ".join(v.reasons))

    def test_script_or_other_line_refused(self):
        v = evaluate(edit(edit(BASE, *NGINX_989), "nginx -t", "nginx -T"))
        self.assertFalse(v.eligible)
        v = evaluate(edit(BASE, "GIT_DEPTH: 5", "GIT_DEPTH: 6"))
        self.assertFalse(v.eligible)

    def test_line_added_refused(self):
        v = evaluate(edit(BASE, "  stage: validate\n", "  stage: validate\n  when: manual\n"))
        self.assertFalse(v.eligible)
        self.assertIn("line count changed", " ".join(v.reasons))

    def test_image_name_change_refused(self):
        v = evaluate(edit(BASE, f"nginx:1.31.1-alpine3.23-perl@{D1}", f"nginxinc/nginx:1.31.2@{D2}"))
        self.assertFalse(v.eligible)

    def test_unpinned_refused(self):
        v = evaluate(edit(BASE, f"nginx:1.31.1-alpine3.23-perl@{D1}", "nginx:1.31.2-alpine3.23-perl"))
        self.assertFalse(v.eligible)
        self.assertIn("not pinned by digest", " ".join(v.reasons))

    def test_major_refused(self):
        v = evaluate(edit(BASE, f"nginx:1.31.1-alpine3.23-perl@{D1}", f"nginx:2.0.0-alpine3.23-perl@{D2}"))
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)
        self.assertTrue(v.manual)
        v = evaluate(edit(BASE, *NGINX_989), labels=["major"])
        self.assertFalse(v.eligible)

    def test_denylisted_images(self):
        v = evaluate(edit(BASE, f"alpine-3.22@{D1}", f"alpine-3.22@{D2}"))      # willhallonline/ansible
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)
        v = evaluate(edit(BASE, f"gitlab-runner:v18.5.0@{D1}", f"gitlab-runner:v18.5.1@{D2}"))
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)
        self.assertTrue(v.manual)

    def test_other_authors_refused(self):
        v = evaluate(edit(BASE, *NGINX_989), author="moltbot")
        self.assertFalse(v.eligible)
        self.assertIn("may not auto-merge CI image updates", " ".join(v.reasons))

    def test_mixed_with_compose_or_other_file_refused(self):
        v = evaluate(edit(BASE, *NGINX_989),
                     files=[FileChange(path=CI), FileChange(path=policy.COMPOSE_PATH)])
        self.assertFalse(v.eligible)
        v = evaluate(edit(BASE, *NGINX_989), files=[FileChange(path=CI), FileChange(path="ci/x.py")])
        self.assertFalse(v.eligible)

    def test_class_disabled(self):
        allow = copy.deepcopy(ALLOW)
        allow["ci_images"]["enabled"] = False
        v = evaluate(edit(BASE, *NGINX_989), allow=allow)
        self.assertFalse(v.eligible)
        self.assertIn("outside the auto-merge scope: .gitlab-ci.yml", " ".join(v.reasons))
        self.assertIn("CODEOWNERS-protected", " ".join(v.reasons))

    def test_malformed_section_fails_closed(self):
        for sec in ({"enabled": True, "denylist": {"x": ""}}, ["x"], {"enabled": True, "files": "x"}):
            with self.subTest(sec=sec):
                allow = copy.deepcopy(ALLOW)
                allow["ci_images"] = sec
                self.assertFalse(evaluate(edit(BASE, *NGINX_989), allow=allow).eligible)

    def test_freeze_label(self):
        v = evaluate(edit(BASE, *NGINX_989), labels=["freeze"])
        self.assertFalse(v.eligible)
        self.assertFalse(v.manual)


GUARD_JOBS = ["ci-image:ansible-canary", "automerge:test"]
ALLOW_G = {
    **ALLOW,
    "ci_images": {"enabled": True, "authors": [RENOVATE], "files": [CI],
                  "denylist": {"docker.io/willhallonline/ansible": "the lagging sweep copy"},
                  "guarded": {"willhallonline/ansible": {
                      "except_tags": ["latest"], "updates": ["digest", "patch"],
                      "required_jobs": GUARD_JOBS, "reason": "runs the deploys"}}},
}
BASE_G = BASE + f"""
.ansible_sweep:
  extends: .ansible
  image: docker.io/willhallonline/ansible:2.21.4-alpine-3.22@{D1}

automerge:sweep:
  extends: .ansible_sweep
  script:
    - sweep

netgear:test:
  image: willhallonline/ansible:latest@{D1}
  script:
    - test
"""
ANSIBLE_LINE = f"willhallonline/ansible:2.21.4-alpine-3.22@{D1}\n  before_script"


class _BlockerGitLab:
    def __init__(self, jobs=()):
        self.jobs = list(jobs)

    def get_all(self, path, **params):
        return self.jobs


class GuardedImageTests(unittest.TestCase):
    """ci_images.guarded: the .ansible image line takes digest and patch
    updates after the canary and unit suites passed; its :latest lines are
    ordinary CI image updates; the lagging sweep copy is never auto-merged."""

    def ev(self, head, base=BASE_G):
        return evaluate(head, base=base, allow=ALLOW_G)

    def test_ansible_digest_update_needs_the_jobs(self):
        v = self.ev(edit(BASE_G, ANSIBLE_LINE, ANSIBLE_LINE.replace(D1, D2)))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual([(c.service, c.kind) for c in v.changes], [(".ansible", "digest")])
        self.assertEqual(v.required_jobs, GUARD_JOBS)
        self.assertIn("needs passed MR pipeline jobs", " ".join(v.notes))

    def test_ansible_patch_update_allowed(self):
        v = self.ev(edit(BASE_G, ANSIBLE_LINE,
                         ANSIBLE_LINE.replace(f"2.21.4-alpine-3.22@{D1}", f"2.21.5-alpine-3.22@{D2}")))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.changes[0].kind, "patch")
        self.assertEqual(v.required_jobs, GUARD_JOBS)

    def test_ansible_minor_update_is_manual(self):
        v = self.ev(edit(BASE_G, ANSIBLE_LINE,
                         ANSIBLE_LINE.replace(f"2.21.4-alpine-3.22@{D1}", f"2.22.0-alpine-3.22@{D2}")))
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)
        self.assertIn("guarded", " ".join(v.reasons))

    def test_latest_lines_are_ordinary(self):
        v = self.ev(edit(BASE_G, f"ansible:latest@{D1}", f"ansible:latest@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual([(c.service, c.kind) for c in v.changes], [("netgear:test", "digest")])
        self.assertEqual(v.required_jobs, [])

    def test_moving_off_or_onto_the_exempt_tag_is_guarded(self):
        base = edit(BASE_G, f"ansible:latest@{D1}", f"ansible:2.21.4-alpine-3.22@{D1}\n  # x")
        base = base.replace("\n  # x", "")
        head = edit(base, f"netgear:test:\n  image: willhallonline/ansible:2.21.4-alpine-3.22@{D1}",
                    f"netgear:test:\n  image: willhallonline/ansible:latest@{D2}")
        v = self.ev(head, base=base)
        self.assertFalse(v.eligible)

    def test_sweep_copy_is_never_auto_merged(self):
        old = f"docker.io/willhallonline/ansible:2.21.4-alpine-3.22@{D1}"
        v = self.ev(edit(BASE_G, old, old.replace(D1, D2)))
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)
        # nor together with the .ansible line
        head = edit(edit(BASE_G, old, old.replace(D1, D2)), ANSIBLE_LINE, ANSIBLE_LINE.replace(D1, D2))
        self.assertFalse(self.ev(head).eligible)

    def test_sweep_runs_outside_the_guarded_image(self):
        self.assertIn("automerge:sweep",
                      policy.ci_image_jobs(BASE_G, ["docker.io/willhallonline/ansible"]))
        self.assertNotIn("automerge:sweep", policy.ci_image_jobs(BASE_G, ["willhallonline/ansible"]))
        self.assertIn("deploy", policy.ci_image_jobs(BASE_G, ["willhallonline/ansible"]))

    def test_required_jobs_gate_the_merge(self):
        v = self.ev(edit(BASE_G, ANSIBLE_LINE, ANSIBLE_LINE.replace(D1, D2)))
        mr = {"sha": "abc", "head_pipeline": {"id": 5, "sha": "abc", "status": "success"}}
        ok = [{"id": i, "name": n, "status": "success"} for i, n in enumerate(GUARD_JOBS)]
        self.assertEqual(automerge.required_job_blockers(_BlockerGitLab(ok), mr, v), [])
        missing = [j for j in ok if j["name"] != "ci-image:ansible-canary"]
        self.assertIn("required job ci-image:ansible-canary is missing",
                      automerge.required_job_blockers(_BlockerGitLab(missing), mr, v))
        failed = ok + [{"id": 99, "name": "automerge:test", "status": "failed"}]
        self.assertTrue(automerge.required_job_blockers(_BlockerGitLab(failed), mr, v))

    def test_malformed_guarded_fails_closed(self):
        good = ALLOW_G["ci_images"]["guarded"]["willhallonline/ansible"]
        bad = [
            "x",
            {"willhallonline/ansible": "x"},
            {"willhallonline/ansible": {**good, "reason": ""}},
            {"willhallonline/ansible": {**good, "updates": []}},
            {"willhallonline/ansible": {**good, "updates": ["major"]}},
            {"willhallonline/ansible": {**good, "required_jobs": []}},
            {"willhallonline/ansible": {**good, "required_jobs": ["not a job"]}},
            {"willhallonline/ansible": {**good, "except_tags": "latest"}},
            {"willhallonline/ansible": {**good, "surprise": 1}},
        ]
        for g in bad:
            with self.subTest(g=g):
                allow = copy.deepcopy(ALLOW_G)
                allow["ci_images"]["guarded"] = g
                with self.assertRaises(policy.PolicyError):
                    policy.ci_images_config(allow)
                v = evaluate(edit(BASE_G, *NGINX_989), base=BASE_G, allow=allow)
                self.assertFalse(v.eligible)
        allow = copy.deepcopy(ALLOW_G)
        allow["ci_images"]["denylist"]["willhallonline/ansible"] = "both"
        with self.assertRaises(policy.PolicyError):
            policy.ci_images_config(allow)


class BlockScalarTests(unittest.TestCase):
    def test_image_line_inside_a_script_is_not_a_job_image(self):
        # found by test_properties: `image: ...` inside `script: - |` (or
        # `script: |`) was taken for the job's image
        for script in ("  script:\n    - |\n", "  script: |\n", "  script: >-\n"):
            with self.subTest(script=script):
                base = BASE + f"\nbuild:\n{script}      image: nginx:1.31.1-alpine3.23-perl@{D1}\n      run\n"
                self.assertNotIn(len(BASE.split("\n")) + 2, policy.ci_image_lines(base))
                head = base.replace(f"      image: nginx:1.31.1-alpine3.23-perl@{D1}",
                                    f"      image: nginx:1.31.2-alpine3.23-perl@{D2}")
                v = evaluate(head, base=base)
                self.assertFalse(v.eligible)
        # the job image lines of BASE are all still found
        self.assertEqual(sorted(set(policy.ci_image_lines(BASE).values())),
                         [".ansible", ".renovate_image", "kaniko:build", "nginx:validate", "runner:check"])


class LineBreakTests(unittest.TestCase):
    def test_line_break_in_comment_refused(self):
        old, new = NGINX_989
        for brk in ("\r", "\x85", "\u2028"):
            with self.subTest(brk=repr(brk)):
                v = evaluate(edit(BASE, old, f"{new} # x{brk}  script: [evil]"))
                self.assertFalse(v.eligible)
                self.assertIn("line break", " ".join(v.reasons))


class ImageJobsTests(unittest.TestCase):
    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_resolution(self):
        self.assertEqual(policy.ci_image_jobs(BASE, ["nginx"]), ["nginx:validate"])
        self.assertEqual(policy.ci_image_jobs(BASE, ["willhallonline/ansible"]), ["deploy"])
        self.assertEqual(policy.ci_image_jobs(BASE, ["renovate/renovate"]), ["renovate"])
        self.assertEqual(policy.ci_image_jobs(BASE, ["gcr.io/kaniko-project/executor"]), ["kaniko:build"])
        self.assertEqual(policy.ci_image_jobs(BASE, ["alpine"]), [])


class WatchTests(unittest.TestCase):
    users = ["nginx:validate"]

    def test_pending(self):
        for p in (None, {"status": "running"}, {"status": "pending"}, {"status": "created"}):
            self.assertEqual(automerge.ci_watch_verdict(p, [], self.users)[0], "pending")

    def test_passed(self):
        jobs = [{"name": "nginx:validate", "status": "success"},
                {"name": "router:deploy", "status": "manual"},
                {"name": "x", "status": "failed", "allow_failure": True}]
        self.assertEqual(automerge.ci_watch_verdict({"status": "success"}, jobs, self.users), ("passed", []))

    def test_revert_on_failed_user_job(self):
        jobs = [{"name": "nginx:validate", "status": "failed"}, {"name": "other", "status": "failed"}]
        self.assertEqual(automerge.ci_watch_verdict({"status": "failed"}, jobs, self.users),
                         ("revert", ["nginx:validate"]))

    def test_unrelated_failure(self):
        jobs = [{"name": "other", "status": "failed"}, {"name": "nginx:validate", "status": "success"}]
        self.assertEqual(automerge.ci_watch_verdict({"status": "failed"}, jobs, self.users),
                         ("unrelated", ["other"]))

    def test_canceled(self):
        self.assertEqual(automerge.ci_watch_verdict({"status": "canceled"}, [], self.users)[0], "unverified")

    def test_canceled_after_the_image_jobs_passed(self):
        # !989: deploy-scope:test was canceled by a newer main pipeline after
        # nginx:validate had passed
        jobs = [{"name": "nginx:validate", "status": "success"}, {"name": "deploy-scope:test", "status": "canceled"}]
        self.assertEqual(automerge.ci_watch_verdict({"status": "canceled"}, jobs, self.users),
                         ("passed", ["nginx:validate"]))
        jobs[0]["status"] = "canceled"
        self.assertEqual(automerge.ci_watch_verdict({"status": "canceled"}, jobs, self.users)[0], "unverified")

    def test_revert_text(self):
        head = edit(BASE, *NGINX_989)
        changes = policy.diff_ci_images(BASE, head)
        main = head + "\nlater:\n  script: [x]\n"
        self.assertEqual(automerge.revert_ci_text(main, changes), BASE + "\nlater:\n  script: [x]\n")
        self.assertIsNone(automerge.revert_ci_text(BASE, changes))   # line no longer on main


class FakeGitLab:
    """Records writes; answers the reads ci_watch and ci_rollback make."""

    def __init__(self, files, pipeline, jobs, merged):
        self.dry_run = False
        self.files, self.pipeline, self.jobs, self.merged = files, pipeline, jobs, merged
        self.writes = []

    def get_all(self, path, **params):
        if path.endswith("/merge_requests") and params.get("labels") == automerge.LABEL_CI_WATCH:
            return self.merged
        if path.endswith("/merge_requests") and params.get("labels") in (automerge.LABEL_DEP_WATCH,
                                                                          automerge.LABEL_RUNNER_WATCH):
            return []           # dependency and runner image merges: their own test files
        if "/jobs" in path:
            return self.jobs
        if path.endswith("/labels"):
            return []
        raise AssertionError(path)

    def get(self, path, **params):
        if path.endswith("/pipelines"):
            return [self.pipeline] if self.pipeline else []
        if "/repository/commits/" in path:
            return {"parent_ids": ["base"]}
        if path.endswith("/merge_requests"):
            return []
        if "/repository/branches/" in path:
            raise RuntimeError("HTTP 404")
        raise AssertionError(path)

    def file_at(self, path, ref):
        return self.files[ref]

    def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if path.endswith("/issues"):
            return {"iid": 77}
        if path.endswith("/merge_requests"):
            return {"iid": 1099}
        return {}

    def ensure_labels(self):
        pass


@unittest.skipIf(yaml is None, "PyYAML not installed")
class CiWatchFlowTests(unittest.TestCase):
    def setUp(self):
        self.head = edit(BASE, *NGINX_989)
        self.mr = {"iid": 989, "title": "update nginx", "merge_commit_sha": "merge", "web_url": "u"}
        self.mailed = []
        self._mail = automerge.mail_root
        automerge.mail_root = lambda s, b: self.mailed.append(s) or True

    def tearDown(self):
        automerge.mail_root = self._mail

    def gl(self, status, jobs):
        return FakeGitLab({"base": BASE, "merge": self.head, "main": self.head},
                          {"id": 5, "status": status, "web_url": "p"}, jobs, [self.mr])

    def test_pending_keeps_watch(self):
        gl = self.gl("running", [])
        self.assertEqual(automerge.ci_watch(gl, ALLOW), [989])
        self.assertEqual(gl.writes, [])

    def test_success_drops_watch(self):
        gl = self.gl("success", [{"name": "nginx:validate", "status": "success"}])
        self.assertEqual(automerge.ci_watch(gl, ALLOW), [])
        self.assertIn(("PUT", "projects/4/merge_requests/989", {"remove_labels": automerge.LABEL_CI_WATCH}),
                      gl.writes)
        self.assertFalse(any(p.endswith("/issues") for _, p, _ in gl.writes))

    def test_failure_in_user_job_reverts_and_holds(self):
        gl = self.gl("failed", [{"name": "nginx:validate", "status": "failed"}])
        automerge.ci_watch(gl, ALLOW)
        commit = next(b for m, p, b in gl.writes if p.endswith("/repository/commits"))
        self.assertEqual(commit["branch"], "automerge-revert/989")
        self.assertEqual(commit["actions"][0]["content"], BASE)
        issue = next(b for m, p, b in gl.writes if p.endswith("/issues"))
        self.assertEqual(issue["labels"], automerge.LABEL_ROLLBACK)
        # the issue holds the next update of nginx in CI files, nothing else
        scope = automerge.rollback_scope(issue)
        self.assertEqual(scope, {("ci", "nginx")})
        holds = [({"iid": 77}, scope)]
        self.assertIn("#77", automerge.rollback_gate(holds, evaluate(self.head)))
        other = evaluate(edit(BASE, f"renovate:44.167.7@{D1}", f"renovate:44.168.0@{D2}"))
        self.assertIsNone(automerge.rollback_gate(holds, other))
        compose = SimpleNamespace(deploy_jobs=["router"], changes=[SimpleNamespace(service="nginx")])
        self.assertIsNone(automerge.rollback_gate(holds, compose))
        self.assertIn(("PUT", "projects/4/merge_requests/989", {"add_labels": automerge.LABEL_ROLLBACK}),
                      gl.writes)
        self.assertTrue(self.mailed)

    def test_unrelated_failure_not_reverted(self):
        gl = self.gl("failed", [{"name": "router:deploy", "status": "failed"}])
        automerge.ci_watch(gl, ALLOW)
        self.assertFalse(any(p.endswith("/issues") or p.endswith("/repository/commits") for _, p, _ in gl.writes))
        note = next(b for m, p, b in gl.writes if p.endswith("/notes"))
        self.assertIn("Not reverted", note["body"])


class MergeTests(unittest.TestCase):
    def test_merge_message_and_labels(self):
        v = evaluate(edit(BASE, *NGINX_989))
        gl = FakeGitLab({}, None, [], [])
        mr = {"iid": 989, "sha": "abc" * 10, "source_branch": "renovate/nginx-1.x", "target_branch": "main",
              "detailed_merge_status": "not_approved"}
        self.assertTrue(automerge.do_merge(gl, mr, v))
        merge = next(b for m, p, b in gl.writes if p.endswith("/merge"))
        msg = merge["merge_commit_message"]
        self.assertIn("Automerge-Policy: ci-image", msg)
        self.assertNotIn("Automerge-Deploy", msg)
        self.assertNotIn("image-update", msg)
        labels = gl.writes[0][2]["add_labels"].split(",")
        self.assertEqual(sorted(labels), sorted([automerge.LABEL_MERGED, automerge.LABEL_CI_IMAGE,
                                                 automerge.LABEL_CI_WATCH]))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in (cls.allow.get("services") or {}).items()}
        cls.ci = (REPO / CI).read_text()
        cls.rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]

    def test_config(self):
        cfg = policy.ci_images_config(self.allow)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["files"], [CI])
        self.assertEqual(cfg["authors"], [RENOVATE])
        # the lagging sweep copy is never auto-merged; the .ansible image is guarded
        self.assertIn("docker.io/willhallonline/ansible", cfg["denylist"])
        g = cfg["guarded"]["willhallonline/ansible"]
        self.assertEqual(g["except_tags"], ["latest"])
        self.assertEqual(sorted(g["updates"]), ["digest", "patch"])
        self.assertIn("ci-image:ansible-canary", g["required_jobs"])

    def test_guarded_required_jobs_run_on_a_ci_file_change(self):
        # Every required job exists and runs in an MR pipeline that changes
        # only .gitlab-ci.yml, or the sweep could never merge the update.
        class _L(yaml.SafeLoader):
            pass
        _L.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
        doc = yaml.load(self.ci, Loader=_L)
        for spec in policy.ci_images_config(self.allow)["guarded"].values():
            for job in spec["required_jobs"]:
                with self.subTest(job=job):
                    self.assertIn(job, doc)
                    rules = [r for r in doc[job].get("rules", []) if isinstance(r, dict)]
                    self.assertTrue(any("merge_request_event" in str(r.get("if", ""))
                                        and CI in (r.get("changes") or []) for r in rules), job)
                    self.assertNotIn("when", doc[job])
                    self.assertFalse(doc[job].get("allow_failure"), job)

    def test_known_image_lines_are_found(self):
        lines = self.ci.split("\n")
        found = {lines[i].strip().split(":", 1)[1].strip().split(":")[0] for i in policy.ci_image_lines(self.ci)}
        for name in ("willhallonline/ansible", "nginx", "renovate/renovate", "alpine", "python"):
            self.assertIn(name, found)

    def test_mr_989_replayed_on_the_real_file(self):
        lines = self.ci.split("\n")
        idx = next(i for i, j in policy.ci_image_lines(self.ci).items() if j == "nginx:validate")
        ref = policy.parse_image_ref(lines[idx].split("image:", 1)[1])
        new = policy.ImageRef(ref.name, re.sub(r"^(\d+\.\d+\.)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1),
                                               ref.tag), D2)
        head = "\n".join(lines[:idx] + [lines[idx].replace(str(ref), str(new))] + lines[idx + 1:])
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=CI)],
                               composes={CI: (self.ci, head)}, allowlist=self.allow,
                               codeowners_text=(REPO / "CODEOWNERS").read_text(), merger="automerge-bot")
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.ci_image)
        self.assertEqual(policy.ci_image_jobs(head, ["nginx"]), ["nginx:validate"])

    def test_sweep_runs_in_the_lagging_image(self):
        # The deploys run in the guarded .ansible image; the jobs that watch
        # and revert its updates run in the lagging docker.io/ copy.
        deploy = policy.ci_image_jobs(self.ci, ["willhallonline/ansible"])
        sweep = policy.ci_image_jobs(self.ci, ["docker.io/willhallonline/ansible"])
        self.assertIn("router:deploy-service", deploy)
        for job in ("automerge:sweep", "automerge:eligibility", "automerge:report"):
            self.assertIn(job, sweep)
            self.assertNotIn(job, deploy)
        lines = self.ci.split("\n")
        ansible = [policy.parse_image_ref(lines[i].split(":", 1)[1]) for i, j in policy.ci_image_lines(self.ci).items()
                   if j in (".ansible", ".ansible_sweep")]
        self.assertEqual(len(ansible), 2)
        self.assertEqual(ansible[0].tag, ansible[1].tag)     # same tag, digest lags

    def test_renovate_rules_match_policy(self):
        cls = [r for r in self.rules if r.get("description", "").startswith("CI image class (")]
        self.assertEqual(len(cls), 1)
        r = cls[0]
        self.assertEqual(r["matchManagers"], ["gitlabci"])
        self.assertEqual(r["matchFileNames"], policy.ci_images_config(self.allow)["files"])
        self.assertEqual(r["matchPackageNames"][0], "/.+/")
        excluded = {n[1:] for n in r["matchPackageNames"][1:]}
        want = set(policy.denylist(self.allow)["images"]) | set(policy.ci_images_config(self.allow)["denylist"])
        self.assertEqual(excluded, want)
        self.assertNotIn("automerge", r)
        deny = [r for r in self.rules if r.get("description", "").startswith("CI image denylist")]
        self.assertEqual(len(deny), 1)
        self.assertEqual(sorted(deny[0]["matchPackageNames"]),
                         sorted(policy.ci_images_config(self.allow)["denylist"]))
        self.assertIn("automerge-manual", deny[0]["addLabels"])
        # Renovate never proposes the lagging sweep copy ...
        off = [r for r in self.rules if r.get("enabled") is False
               and "docker.io/willhallonline/ansible" in (r.get("matchDepNames") or [])]
        self.assertEqual(len(off), 1)
        # ... and labels minors and majors of guarded images manual
        guard = [r for r in self.rules if r.get("description", "").startswith("CI image guarded")]
        self.assertEqual(len(guard), 1)
        g = policy.ci_images_config(self.allow)["guarded"]
        self.assertEqual(sorted(guard[0]["matchPackageNames"]), sorted(g))
        self.assertEqual(sorted(guard[0]["matchUpdateTypes"]), ["major", "minor"])
        self.assertIn("automerge-manual", guard[0]["addLabels"])
        for spec in g.values():
            self.assertNotIn("minor", spec["updates"])
            for tag in spec["except_tags"]:
                self.assertIn(tag, guard[0]["matchCurrentValue"])

    def test_codeowners_lists_the_bot(self):
        rules = policy.parse_codeowners((REPO / "CODEOWNERS").read_text())
        for path in policy.ci_images_config(self.allow)["files"]:
            own = None
            for pat, o in rules:
                if policy.codeowners_match(pat, path):
                    own = o
            self.assertIn("@root", own)
            self.assertIn("@project_4_bot_9927c1d39b8f3ba11b15bfa664d8b6d6", own)

    def test_ci_image_merges_make_host_deploys_manual(self):
        m = re.search(r"\.automerged_commit_manual:\n  rules:\n    - if: '.*?=~ /(Automerge-Policy: [^/]*)/'", self.ci)
        self.assertTrue(m)
        trailer = automerge.CI_TRAILER
        self.assertTrue(re.search(m.group(1), trailer))
        # and never selects a per-service deploy
        self.assertNotIn("Automerge-Deploy", trailer + automerge.CI_CLASS_TRAILER)


if __name__ == "__main__":
    unittest.main()
