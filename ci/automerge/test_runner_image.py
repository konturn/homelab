"""Tests for the runner image class (docs/AUTOMERGE.md, "Runner default
image"): the default image lines of base/gitlab-runner/config.toml, the
post-merge watch of router:deploy and the runner:default-image canary, the
revert and the rollback hold, and the real policy, renovate.json, CODEOWNERS,
runner config and CI file.

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
import policy  # noqa: E402
from policy import FileChange  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]
RENOVATE = "project_4_bot_af78a2ea227eba9e10006515fbde375e"
BOT = "@project_4_bot_9927c1d39b8f3ba11b15bfa664d8b6d6"
RUNNER = policy.RUNNER_PATH
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64

BASE = f"""# Default image: pinned by digest.
concurrent = 13
check_interval = 0

[session_server]
  session_timeout = 1800

[[runners]]
  name = "router-protected"
  url = "https://gitlab.lab.nkontur.com/"
  token = "{{{{ gitlab_runner_protected_token }}}}"
  executor = "docker"
  limit = 6
  [runners.docker]
    image = "ubuntu:24.04@{D1}"
    helper_image = "gitlab/gitlab-runner-helper:x86_64-v18.5.0@{D1}"
    network_mode = "gitlab-ci-protected"
    privileged = false
    memory = "4g"
    volumes = ["/cache/gitlab-runner:/cache"]
    [[runners.docker.services]]
      name = "svc"
      image = "redis:7.4.1@{D1}"

[[runners]]
  name = "router-firmware"
  url = "https://gitlab.lab.nkontur.com/"
  token = "{{{{ gitlab_runner_firmware_token }}}}"
  executor = "docker"
  [runners.docker]
    image = "ubuntu:24.04@{D1}"
    network_mode = "host"
    privileged = false

[[runners]]
  name = "main.nkontur.com"
  url = "https://gitlab.lab.nkontur.com/"
  token = "{{{{ lookup('env', 'GITLAB_RUNNER_TOKEN') }}}}"
  executor = "docker"
  [runners.docker]
    image = "ubuntu:24.04@{D1}"
    network_mode = "gitlab-ci"
    privileged = false
"""

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH],
    "denylist": {"images": {"gitlab/gitlab-runner": "runs every CI job",
                            "mikebrady/shairport-sync": "crash skip in renovate.json"}},
    "ci_images": {"enabled": True, "authors": [RENOVATE], "files": [policy.CI_PATH],
                  "denylist": {"willhallonline/ansible": "runs the sweep"}},
    "runner_images": {"enabled": True, "authors": [RENOVATE], "codeowner": BOT, "denylist": {}},
}
CODEOWNERS = f"/base/ @root\n/base/gitlab-runner/ @root\n/base/gitlab-runner/config.toml @root {BOT}\n"

UBUNTU_OLD = f'image = "ubuntu:24.04@{D1}"'
UBUNTU_NEW = f'image = "ubuntu:24.04@{D2}"'


def edit(text, old, new, count=1):
    assert old in text, old
    return text.replace(old, new, count)


def evaluate(head, *, base=BASE, author=RENOVATE, allow=ALLOW, files=None, labels=(), codeowners=CODEOWNERS):
    files = files or [FileChange(path=RUNNER)]
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=list(labels), files=files,
        composes={RUNNER: (base, head)}, allowlist=allow, codeowners_text=codeowners, merger="bot")


DIGEST_BUMP = edit(BASE, UBUNTU_OLD, UBUNTU_NEW, -1)        # all three runners, as Renovate does


class EligibilityTests(unittest.TestCase):
    def test_digest_bump_of_ubuntu_2404(self):
        v = evaluate(DIGEST_BUMP)
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.runner_image)
        self.assertFalse(v.ci_image or v.dependency or v.fast_track or v.bot_config)
        self.assertEqual(v.deploy_jobs, [])
        self.assertEqual(v.runner_images, ["ubuntu"])
        self.assertEqual([(c.service, c.kind) for c in v.changes],
                         [("router-protected", "digest"), ("router-firmware", "digest"),
                          ("main.nkontur.com", "digest")])
        self.assertEqual(v.hold_keys, [(policy.RUNNER_HOLD_JOB, "ubuntu")])
        self.assertIn("class: runner image", " ".join(v.notes))

    def test_single_runner_digest_bump(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, UBUNTU_NEW))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual([c.service for c in v.changes], ["router-protected"])

    def test_patch_same_series_accepted(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "ubuntu:24.04.1@{D2}"', -1))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual({c.kind for c in v.changes}, {"patch"})

    def test_major_tag_change_refused(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "ubuntu:26.04@{D2}"', -1))
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)
        self.assertTrue(v.manual)
        self.assertIn("major", " ".join(v.reasons))

    def test_minor_tag_change_refused(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "ubuntu:24.10@{D2}"', -1))
        self.assertFalse(v.eligible)
        self.assertFalse(v.major)
        self.assertTrue(v.manual)
        self.assertIn("digest and patch updates only", " ".join(v.reasons))

    def test_codename_tag_refused(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "ubuntu:noble@{D2}"'))
        self.assertFalse(v.eligible)

    def test_major_label_refused(self):
        v = evaluate(DIGEST_BUMP, labels=["major"])
        self.assertFalse(v.eligible)

    def test_ansible_image_refused(self):
        # willhallonline/ansible stays manual (ci_images.denylist), also as a runner default
        old = f'image = "willhallonline/ansible:2.21.4-alpine-3.22@{D1}"'
        base = edit(BASE, UBUNTU_OLD, old)
        v = evaluate(edit(base, old, f'image = "willhallonline/ansible:2.21.4-alpine-3.22@{D2}"'), base=base)
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)
        self.assertTrue(v.manual)
        self.assertIn("willhallonline/ansible", " ".join(v.reasons))

    def test_crash_skip_denylisted_image_refused(self):
        old = f'image = "mikebrady/shairport-sync:5.0.2@{D1}"'
        base = edit(BASE, UBUNTU_OLD, old)
        v = evaluate(edit(base, old, f'image = "mikebrady/shairport-sync:5.0.2@{D2}"'), base=base)
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)

    def test_runner_only_denylist(self):
        allow = copy.deepcopy(ALLOW)
        allow["runner_images"]["denylist"] = {"ubuntu": "test"}
        v = evaluate(DIGEST_BUMP, allow=allow)
        self.assertFalse(v.eligible)
        self.assertTrue(v.denied)

    def test_other_lines_refused(self):
        for old, new in [
            ('memory = "4g"', 'memory = "8g"'),
            ('network_mode = "gitlab-ci-protected"', 'network_mode = "host"'),
            ("privileged = false\n    memory", "privileged = true\n    memory"),
            ("gitlab_runner_protected_token", "other_token"),
            ("concurrent = 13", "concurrent = 14"),
            ('"/cache/gitlab-runner:/cache"', '"/:/host"'),
            (f'helper_image = "gitlab/gitlab-runner-helper:x86_64-v18.5.0@{D1}"',
             f'helper_image = "gitlab/gitlab-runner-helper:x86_64-v18.5.0@{D2}"'),
            (f'image = "redis:7.4.1@{D1}"', f'image = "redis:7.4.1@{D2}"'),       # service image
            ("# Default image: pinned by digest.", "# Default image."),
        ]:
            with self.subTest(old=old):
                v = evaluate(edit(DIGEST_BUMP, old, new))
                self.assertFalse(v.eligible)
                self.assertFalse(evaluate(edit(BASE, old, new)).eligible)

    def test_line_added_refused(self):
        v = evaluate(edit(DIGEST_BUMP, '    privileged = false\n', '    privileged = false\n    pull_policy = "never"\n'))
        self.assertFalse(v.eligible)
        self.assertIn("line count changed", " ".join(v.reasons))

    def test_key_or_spacing_change_refused(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image  = "ubuntu:24.04@{D2}"'))
        self.assertFalse(v.eligible)

    def test_unpinned_or_malformed_refused(self):
        for new in ('image = "ubuntu:24.04"', f'image = "ubuntu@{D2}"', f'image = "ubuntu:24.04@{D2} "',
                    f'image = "ubuntu:{{{{ x }}}}@{D2}"', f'image = "ubuntu:24.04@{D2}" # x'):
            with self.subTest(new=new):
                self.assertFalse(evaluate(edit(BASE, UBUNTU_OLD, new)).eligible)

    def test_image_name_change_refused(self):
        v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "debian:24.04@{D2}"'))
        self.assertFalse(v.eligible)
        self.assertIn("image name changed", " ".join(v.reasons))

    def test_line_break_refused(self):
        for brk in ("\r", " "):
            with self.subTest(brk=repr(brk)):
                v = evaluate(edit(BASE, UBUNTU_OLD, f'image = "ubuntu:24.04@{D2}{brk}"'))
                self.assertFalse(v.eligible)

    def test_mixed_with_other_files_refused(self):
        for other in (policy.CI_PATH, policy.COMPOSE_PATH, "base/gitlab-runner/other.toml", "ci/x.py"):
            with self.subTest(other=other):
                v = evaluate(DIGEST_BUMP, files=[FileChange(path=RUNNER), FileChange(path=other)])
                self.assertFalse(v.eligible)

    def test_other_runner_file_refused(self):
        path = "base/gitlab-runner/other.toml"
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=path)],
                               composes={path: (BASE, DIGEST_BUMP)}, allowlist=ALLOW,
                               codeowners_text=CODEOWNERS, merger="bot")
        self.assertFalse(v.eligible)
        self.assertFalse(v.runner_image)

    def test_other_authors_refused(self):
        v = evaluate(DIGEST_BUMP, author="moltbot")
        self.assertFalse(v.eligible)
        self.assertIn("may not auto-merge runner image updates", " ".join(v.reasons))

    def test_bot_must_be_code_owner(self):
        v = evaluate(DIGEST_BUMP, codeowners="/base/ @root\n")
        self.assertFalse(v.eligible)
        self.assertIn("CODEOWNERS does not list the auto-merge bot", " ".join(v.reasons))

    def test_class_disabled(self):
        allow = copy.deepcopy(ALLOW)
        allow["runner_images"]["enabled"] = False
        v = evaluate(DIGEST_BUMP, allow=allow)
        self.assertFalse(v.eligible)
        self.assertIn(f"outside the auto-merge scope: {RUNNER}", " ".join(v.reasons))

    def test_malformed_section_fails_closed(self):
        for sec in ({"enabled": True, "denylist": {"x": ""}}, ["x"], {"enabled": True, "files": ["x"]},
                    {"enabled": True, "codeowner": "bot"}, {"enabled": True, "authors": "x"}):
            with self.subTest(sec=sec):
                allow = copy.deepcopy(ALLOW)
                allow["runner_images"] = sec
                self.assertFalse(evaluate(DIGEST_BUMP, allow=allow).eligible)

    def test_freeze_label(self):
        v = evaluate(DIGEST_BUMP, labels=["freeze"])
        self.assertFalse(v.eligible)
        self.assertFalse(v.manual)

    def test_image_lines(self):
        lines = policy.runner_image_lines(BASE)
        self.assertEqual(sorted(lines.values()), ["main.nkontur.com", "router-firmware", "router-protected"])
        text = BASE.split("\n")
        self.assertTrue(all("ubuntu" in text[i] for i in lines))


class WatchFlowTests(unittest.TestCase):
    CI = "router:deploy:\n  image: willhallonline/ansible:2@" + D1 + "\n  script: [x]\n" \
         "runner:default-image:\n  needs: [router:deploy]\n  script: [x]\n"

    def setUp(self):
        self.mr = {"iid": 1200, "title": "update ubuntu:24.04 digest", "merge_commit_sha": "merge", "web_url": "u"}
        self.mailed = []
        self._mail = automerge.mail_root
        automerge.mail_root = lambda s, b: self.mailed.append(s) or True

    def tearDown(self):
        automerge.mail_root = self._mail

    def gl(self, status, jobs, trace=""):
        return FakeGitLab({"base": BASE, "merge": DIGEST_BUMP, "main": DIGEST_BUMP}, self.CI,
                          {"id": 5, "status": status, "web_url": "p"}, jobs, [self.mr], trace)

    def ok_jobs(self, canary="success"):
        return [{"id": 1, "name": "router:deploy", "status": "success"},
                {"id": 2, "name": "runner:default-image", "status": canary}]

    def notes(self, gl):
        return " ".join(b["body"] for m, p, b in gl.writes if p.endswith("/notes"))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_pending_keeps_watch(self):
        gl = self.gl("running", [])
        self.assertEqual(automerge.ci_watch(gl, ALLOW), [1200])
        self.assertEqual(gl.writes, [])

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_passed_when_canary_ran_the_new_image(self):
        trace = f"Using docker image sha256:abc for ubuntu:24.04@{D2} with digest ubuntu@{D2} ..."
        gl = self.gl("success", self.ok_jobs(), trace)
        self.assertEqual(automerge.ci_watch(gl, ALLOW), [])
        self.assertIn("passed", self.notes(gl))
        self.assertIn(("PUT", "projects/4/merge_requests/1200", {"remove_labels": automerge.LABEL_RUNNER_WATCH}),
                      gl.writes)
        self.assertFalse(any(p.endswith("/issues") for _, p, _ in gl.writes))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_canary_in_old_image_is_unverified(self):
        trace = f"Using docker image sha256:abc for ubuntu:24.04@{D1} ..."
        gl = self.gl("success", self.ok_jobs(), trace)
        automerge.ci_watch(gl, ALLOW)
        self.assertIn("not verified", self.notes(gl))
        self.assertIn("does not show the new image", self.notes(gl))
        self.assertFalse(any(p.endswith("/issues") for _, p, _ in gl.writes))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_canary_missing_or_skipped_is_unverified(self):
        for jobs in ([{"id": 1, "name": "router:deploy", "status": "success"}], self.ok_jobs("skipped")):
            with self.subTest(jobs=jobs):
                gl = self.gl("success", jobs)
                automerge.ci_watch(gl, ALLOW)
                self.assertIn("not verified", self.notes(gl))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_failed_canary_reverts_and_holds(self):
        gl = self.gl("failed", self.ok_jobs("failed"))
        automerge.ci_watch(gl, ALLOW)
        commit = next(b for m, p, b in gl.writes if p.endswith("/repository/commits"))
        self.assertEqual(commit["branch"], "automerge-revert/1200")
        self.assertEqual(commit["actions"], [{"action": "update", "file_path": RUNNER, "content": BASE}])
        issue = next(b for m, p, b in gl.writes if p.endswith("/issues"))
        scope = automerge.rollback_scope(issue)
        self.assertEqual(scope, {("runner", "ubuntu")})
        holds = [({"iid": 77}, scope)]
        self.assertIn("#77", automerge.rollback_gate(holds, evaluate(DIGEST_BUMP)))
        self.assertTrue(self.mailed)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_failed_router_deploy_reverts(self):
        jobs = [{"id": 1, "name": "router:deploy", "status": "failed"},
                {"id": 2, "name": "runner:default-image", "status": "skipped"}]
        gl = self.gl("failed", jobs)
        automerge.ci_watch(gl, ALLOW)
        self.assertTrue(any(p.endswith("/repository/commits") for _, p, _ in gl.writes))

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_unrelated_failure_not_reverted(self):
        trace = f"for ubuntu:24.04@{D2}"
        gl = self.gl("failed", self.ok_jobs() + [{"id": 3, "name": "zwave:deploy", "status": "failed"}], trace)
        automerge.ci_watch(gl, ALLOW)
        self.assertFalse(any(p.endswith("/issues") or p.endswith("/repository/commits") for _, p, _ in gl.writes))
        self.assertIn("Not reverted", self.notes(gl))


class FakeGitLab:
    """Records writes; answers the reads ci_watch and ci_rollback make."""

    def __init__(self, files, ci, pipeline, jobs, merged, trace):
        self.dry_run = False
        self.files, self.ci, self.pipeline, self.jobs, self.merged, self.trace = \
            files, ci, pipeline, jobs, merged, trace
        self.writes = []

    def get_all(self, path, **params):
        if path.endswith("/merge_requests"):
            return self.merged if params.get("labels") == automerge.LABEL_RUNNER_WATCH else []
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

    def _req(self, method, path, params=None, body=None, raw=False):
        assert method == "GET" and path.endswith("/trace") and raw, path
        return self.trace

    def file_at(self, path, ref):
        if path == policy.CI_PATH:
            return self.ci
        assert path == RUNNER, path
        return self.files[ref]

    def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        if path.endswith("/issues"):
            return {"iid": 77}
        if path.endswith("/merge_requests"):
            return {"iid": 1201}
        return {}

    def ensure_labels(self):
        pass


class MergeTests(unittest.TestCase):
    def test_merge_message_and_labels(self):
        v = evaluate(DIGEST_BUMP)
        gl = FakeGitLab({}, "", None, [], [], "")
        mr = {"iid": 1200, "sha": "abc" * 10, "source_branch": "renovate/runner-ubuntu-24.04",
              "target_branch": "main", "detailed_merge_status": "not_approved"}
        self.assertTrue(automerge.do_merge(gl, mr, v))
        merge = next(b for m, p, b in gl.writes if p.endswith("/merge"))
        msg = merge["merge_commit_message"]
        self.assertIn(automerge.RUNNER_TRAILER, msg)
        self.assertIn(automerge.RUNNER_CLASS_TRAILER, msg)
        self.assertNotIn("Automerge-Deploy", msg)
        labels = gl.writes[0][2]["add_labels"].split(",")
        self.assertEqual(sorted(labels), sorted([automerge.LABEL_MERGED, automerge.LABEL_RUNNER_IMAGE,
                                                 automerge.LABEL_RUNNER_WATCH]))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in (cls.allow.get("services") or {}).items()}
        cls.ci = (REPO / policy.CI_PATH).read_text()
        cls.runner = (REPO / RUNNER).read_text()
        cls.codeowners = (REPO / "CODEOWNERS").read_text()
        cls.rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]

    def test_config(self):
        cfg = policy.runner_images_config(self.allow)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["authors"], [RENOVATE])
        self.assertEqual(cfg["codeowner"], BOT)
        # willhallonline/ansible stays manual as a runner default image (Noah's decision)
        self.assertIn("willhallonline/ansible", cfg["denylist"])

    def test_three_default_image_lines(self):
        lines = policy.runner_image_lines(self.runner)
        self.assertEqual(sorted(lines.values()), ["main.nkontur.com", "router-firmware", "router-protected"])

    def test_digest_bump_replayed_on_the_real_file(self):
        head = re.sub(r'(image = "ubuntu:24\.04@)sha256:[0-9a-f]{64}"', rf'\g<1>{D2}"', self.runner)
        self.assertNotEqual(head, self.runner)
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=RUNNER)],
                               composes={RUNNER: (self.runner, head)}, allowlist=self.allow,
                               codeowners_text=self.codeowners, merger="automerge-bot")
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.runner_image)
        self.assertEqual(len(v.changes), 3)
        tag = re.sub(r'(image = "ubuntu:)24\.04@sha256:[0-9a-f]{64}"', rf'\g<1>26.04@{D2}"', self.runner)
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=RUNNER)],
                               composes={RUNNER: (self.runner, tag)}, allowlist=self.allow,
                               codeowners_text=self.codeowners, merger="automerge-bot")
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)

    def test_only_the_canary_runs_in_the_default_image(self):
        # A broken default image can only fail the canary: the sweep, the
        # deploys and the revert path all set their own image.
        self.assertEqual(policy.default_image_jobs(self.ci), [automerge.RUNNER_CANARY_JOB])
        for job in ("automerge:sweep", "router:deploy", "router:deploy-service", "automerge:mr-check"):
            if job in yaml.load(self.ci, Loader=_Loader):
                self.assertIn(job, policy.ci_image_jobs(
                    self.ci, ["willhallonline/ansible", "docker.io/willhallonline/ansible"]), job)

    def test_canary_runs_after_router_deploy_on_runner_changes(self):
        doc = yaml.load(self.ci, Loader=_Loader)
        job = doc[automerge.RUNNER_CANARY_JOB]
        self.assertNotIn("image", job)
        self.assertNotIn("extends", job)
        self.assertEqual(job["needs"], [{"job": automerge.RUNNER_DEPLOY_JOB}])
        self.assertIn(RUNNER, job["rules"][-1]["changes"])

    def test_runner_merges_deploy_normally(self):
        m = re.search(r"\.automerged_commit_manual:\n  rules:\n    - if: '.*?=~ /(Automerge-Policy: [^/]*)/'", self.ci)
        self.assertTrue(m)
        self.assertFalse(re.search(m.group(1), automerge.RUNNER_TRAILER))
        self.assertFalse(re.search(m.group(1), automerge.RUNNER_CLASS_TRAILER))

    def test_codeowners_lists_the_bot(self):
        own = policy.codeowner_of(RUNNER, self.codeowners)
        self.assertIn("@root", own)
        self.assertIn(BOT, own)
        # only that file: the rest of base/gitlab-runner/ stays with root alone
        self.assertNotIn(BOT, policy.codeowner_of("base/gitlab-runner/other.toml", self.codeowners))

    def test_renovate_rules_match_policy(self):
        cls = [r for r in self.rules if r.get("description", "").startswith("Runner image class (")]
        self.assertEqual(len(cls), 1)
        r = cls[0]
        self.assertEqual(r["matchManagers"], ["custom.regex"])
        self.assertEqual(r["matchFileNames"], [RUNNER])
        self.assertEqual(r["additionalBranchPrefix"], "runner-")
        self.assertNotIn("minor", r["matchUpdateTypes"])
        self.assertNotIn("major", r["matchUpdateTypes"])
        self.assertEqual(r["matchPackageNames"][0], "/.+/")
        excluded = {n[1:] for n in r["matchPackageNames"][1:]}
        want = (set(policy.denylist(self.allow)["images"]) | set(policy.ci_images_config(self.allow)["denylist"])
                | set(policy.runner_images_config(self.allow)["denylist"]))
        self.assertEqual(excluded, want)
        self.assertNotIn("automerge", r)
        manual = [r for r in self.rules if r.get("description", "").startswith("Runner image class: a minor")]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0]["matchUpdateTypes"], ["minor"])
        self.assertIn("automerge-manual", manual[0]["addLabels"])


if yaml is not None:
    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


if __name__ == "__main__":
    unittest.main()
