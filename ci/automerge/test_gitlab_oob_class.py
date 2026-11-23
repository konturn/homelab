"""Tests for the GitLab out-of-band class (docs/AUTOMERGE.md, "GitLab
updates"): digest and patch updates of the gitlab image line merge without a
CI deploy, everything else about GitLab stays manual, the sweep's pending and
rollback gates, the merge trailers, and the real policy and renovate.json.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
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
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
ROUTER = policy.Deployment("router.lab.nkontur.com", "router", "docker", "10.4.0.1")

BASE = f"""services:
  pihole:
    image: pihole/pihole:2026.10.0@{D1}
  gitlab:
    image: 'gitlab/gitlab-ee:19.6.1-ee.0@{D1}'
"""

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH],
    "deploy_hosts": {"router.lab.nkontur.com": "router"},
    "services": {},
    "denylist": {"services": {"gitlab": "runs the pipeline that would roll it back"},
                 "images": {"gitlab/gitlab-ee": "GitLab", "gitlab/gitlab-ce": "GitLab"}},
    "stateful": {"services": ["gitlab"], "images": []},
    "gitlab_oob": {"enabled": True, "service": "gitlab", "file": policy.COMPOSE_PATH,
                   "images": ["gitlab/gitlab-ee", "gitlab/gitlab-ce"], "updates": ["digest", "patch"],
                   "authors": [RENOVATE]},
}


def edit(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


def evaluate(head, *, author=RENOVATE, allow=ALLOW, labels=()):
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=list(labels),
        files=[FileChange(path=policy.COMPOSE_PATH)], composes={policy.COMPOSE_PATH: (BASE, head)},
        deployments={policy.COMPOSE_PATH: [ROUTER]}, allowlist=allow, codeowners_text="/docker/ @root\n",
        merger="automerge-bot")


def gitlab(new):
    return edit(BASE, f"gitlab-ee:19.6.1-ee.0@{D1}", new)


class ClassTests(unittest.TestCase):
    def test_digest_and_patch_qualify_without_a_deploy(self):
        for new, kind in ((f"gitlab-ee:19.6.1-ee.0@{D2}", "digest"), (f"gitlab-ee:19.6.2-ee.0@{D2}", "patch"),
                          (f"gitlab-ee:19.6.1-ee.1@{D2}", "patch")):
            with self.subTest(new=new):
                v = evaluate(gitlab(new))
                self.assertTrue(v.eligible, v.reasons)
                self.assertTrue(v.gitlab_oob)
                self.assertFalse(v.denied)
                self.assertEqual([c.kind for c in v.changes], [kind])
                self.assertEqual(v.deploy_jobs, [])
                self.assertIn((policy.GITLAB_OOB_HOLD_JOB, "gitlab"), v.hold_keys)

    def test_minor_stays_manual(self):
        v = evaluate(gitlab(f"gitlab-ee:19.7.0-ee.0@{D2}"))
        self.assertFalse(v.eligible)
        self.assertTrue(v.manual)
        self.assertIn("minor updates of GitLab stay manual", " ".join(v.reasons))

    def test_major_and_edition_change_go_the_general_way(self):
        for new in (f"gitlab-ee:20.0.0-ee.0@{D2}", ):
            v = evaluate(gitlab(new))
            self.assertFalse(v.eligible)
            self.assertFalse(v.gitlab_oob)
            self.assertTrue(v.major)
        v = evaluate(edit(BASE, f"gitlab/gitlab-ee:19.6.1-ee.0@{D1}", f"gitlab/gitlab-ce:19.6.1-ce.0@{D2}"))
        self.assertFalse(v.eligible)
        self.assertFalse(v.gitlab_oob)

    def test_only_renovate(self):
        v = evaluate(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"), author="moltbot")
        self.assertFalse(v.eligible)
        self.assertIn("may not auto-merge GitLab updates", " ".join(v.reasons))

    def test_mixed_with_another_service_is_denied(self):
        head = edit(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"), f"pihole:2026.10.0@{D1}", f"pihole:2026.10.1@{D2}")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertFalse(v.gitlab_oob)
        self.assertTrue(v.denied)

    def test_disabled_or_missing_section_keeps_gitlab_denied(self):
        for allow in (dict(ALLOW, gitlab_oob=dict(ALLOW["gitlab_oob"], enabled=False)),
                      {k: v for k, v in ALLOW.items() if k != "gitlab_oob"}):
            v = evaluate(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"), allow=allow)
            self.assertFalse(v.eligible)
            self.assertTrue(v.denied)

    def test_malformed_section_fails_closed(self):
        for bad in ({"enabled": True}, ["x"], dict(ALLOW["gitlab_oob"], updates=["minor"]),
                    dict(ALLOW["gitlab_oob"], images=[])):
            allow = dict(ALLOW, gitlab_oob=bad)
            v = evaluate(edit(BASE, f"pihole:2026.10.0@{D1}", f"pihole:2026.10.1@{D2}"), allow=allow)
            self.assertFalse(v.eligible, bad)
            self.assertIn("policy: ", " ".join(v.reasons))

    def test_freeze_and_major_label_still_apply(self):
        v = evaluate(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"), labels=["freeze"])
        self.assertFalse(v.eligible)


class FakeGitLab:
    def __init__(self, pending=()):
        self.dry_run = False
        self.pending = list(pending)
        self.writes = []

    def get_all(self, path, **params):
        if path.endswith("/merge_requests") and params.get("labels") == automerge.LABEL_GITLAB_PENDING:
            return self.pending
        raise AssertionError((path, params))

    def write(self, method, path, body=None):
        self.writes.append((method, path, body))
        return {}


class SweepTests(unittest.TestCase):
    def test_merge_message_labels_and_no_deploy_trailer(self):
        v = evaluate(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"))
        gl = FakeGitLab()
        mr = {"iid": 1500, "sha": "abc" * 10, "source_branch": "renovate/gitlab", "target_branch": "main",
              "detailed_merge_status": "not_approved"}
        self.assertTrue(automerge.do_merge(gl, mr, v))
        msg = next(b for m, p, b in gl.writes if p.endswith("/merge"))["merge_commit_message"]
        self.assertIn(automerge.GITLAB_CLASS_TRAILER, msg)
        self.assertIn(automerge.GITLAB_TRAILER, msg)
        self.assertNotIn("Automerge-Deploy", msg)
        self.assertNotIn("image-update", msg)
        labels = gl.writes[0][2]["add_labels"].split(",")
        self.assertEqual(sorted(labels), sorted([automerge.LABEL_MERGED, automerge.LABEL_GITLAB_OOB,
                                                 automerge.LABEL_GITLAB_PENDING]))

    def test_pending_gate(self):
        self.assertIsNone(automerge.gitlab_pending_gate(FakeGitLab()))
        self.assertIn("!1500", automerge.gitlab_pending_gate(FakeGitLab([{"iid": 1500}])))

    def test_rollback_issue_holds_the_class(self):
        v = evaluate(gitlab(f"gitlab-ee:19.6.2-ee.0@{D2}"))
        issue = {"iid": 9, "description": "<!-- automerge-rollback job=gitlab-oob services=gitlab -->"}
        holds = [(issue, automerge.rollback_scope(issue))]
        self.assertIn("#9", automerge.rollback_gate(holds, v))
        other = {"iid": 10, "description": "<!-- automerge-rollback job=router services=pihole -->"}
        self.assertIsNone(automerge.rollback_gate([(other, automerge.rollback_scope(other))], v))

    def test_full_deploys_are_manual_for_the_merge_commit(self):
        ci = (REPO / ".gitlab-ci.yml").read_text()
        self.assertRegex(ci, r"Automerge-Policy: \([^)]*gitlab-oob[^)]*\)")

    def test_agent_trailer_matches(self):
        src = (REPO / "base" / "gitlab-oob" / "gitlab-oob-upgrade.py").read_text()
        self.assertIn(f'CLASS_TRAILER = "{automerge.GITLAB_CLASS_TRAILER}"', src)
        self.assertIn(f'PENDING_LABEL = "{automerge.LABEL_GITLAB_PENDING}"', src)
        self.assertIn(f'HOLD_JOB = "{policy.GITLAB_OOB_HOLD_JOB}"', src)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in (cls.allow.get("services") or {}).items()}
        cls.rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]

    def test_config(self):
        cfg = policy.gitlab_oob_config(self.allow)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["service"], "gitlab")
        self.assertEqual(sorted(cfg["updates"]), ["digest", "patch"])
        self.assertEqual(cfg["authors"], [RENOVATE])
        # gitlab stays denied for every other class
        d = policy.denylist(self.allow)
        self.assertIn("gitlab", d["services"])
        for img in cfg["images"]:
            self.assertIn(img, d["images"])

    def test_real_compose_patch_and_minor(self):
        text = (REPO / policy.COMPOSE_PATH).read_text()
        _, img = policy.compose_line_services(text)
        lines = text.split("\n")
        i = next(i for i, l in enumerate(lines) if "gitlab/gitlab-e" in l and "image:" in l)
        ref = policy.parse_image_ref(lines[i].split("image:", 1)[1])
        major, minor, patch = (int(x) for x in ref.tag.split("-")[0].split("."))
        deps = {policy.COMPOSE_PATH: [ROUTER]}
        for tag, ok in ((f"{major}.{minor}.{patch + 1}-ee.0", True), (f"{major}.{minor + 1}.0-ee.0", False)):
            head = text.replace(str(ref), str(policy.ImageRef(ref.name, tag, D2)), 1)
            v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[],
                                   files=[FileChange(path=policy.COMPOSE_PATH)],
                                   composes={policy.COMPOSE_PATH: (text, head)}, deployments=deps,
                                   allowlist=self.allow, codeowners_text=(REPO / "CODEOWNERS").read_text(),
                                   merger="automerge-bot")
            self.assertEqual(v.eligible, ok, (tag, v.reasons))
            self.assertTrue(v.gitlab_oob)

    def test_renovate_rules_match_the_class(self):
        cfg = policy.gitlab_oob_config(self.allow)
        cls = [r for r in self.rules if r.get("description", "").startswith("GitLab out-of-band class")]
        manual = [r for r in self.rules if r.get("description", "").startswith("GitLab minor and major")]
        self.assertEqual(len(cls), 1)
        self.assertEqual(len(manual), 1)
        self.assertEqual(sorted(cls[0]["matchPackageNames"]), sorted(cfg["images"]))
        self.assertEqual(cls[0]["matchFileNames"], [cfg["file"]])
        self.assertEqual(set(cls[0]["matchUpdateTypes"]), set(cfg["updates"]) | {"pinDigest"})
        self.assertNotIn("addLabels", cls[0])
        self.assertEqual(sorted(manual[0]["matchPackageNames"]), sorted(cfg["images"]))
        self.assertEqual(set(manual[0]["matchUpdateTypes"]), {"minor", "major"})
        self.assertIn("automerge-manual", manual[0]["addLabels"])


if __name__ == "__main__":
    unittest.main()
