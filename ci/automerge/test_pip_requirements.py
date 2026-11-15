"""Tests for pip requirements files in the dependency class
(dependencies.pip_requirements; docs/AUTOMERGE.md, "Dependency updates"):
the two covered files, the cases that stay manual, the test-job gate, the
post-merge watch and revert, and the real policy, CODEOWNERS and CI file.

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402
import policy  # noqa: E402
from policy import FileChange  # noqa: E402
from test_dependency_class import FakeGitLab  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
ALLOW = automerge.load_allowlist()
CFG = policy.dependencies_config(ALLOW)
RENOVATE = CFG["authors"][0]
CODEOWNERS = (REPO / "CODEOWNERS").read_text()
CI_TEXT = (REPO / policy.CI_PATH).read_text()

AD = "docker/anomaly-detector/requirements.txt"
ER_DEV = "docker/event-router/requirements-dev.txt"
ER_RUN = "docker/event-router/requirements.txt"
JIT_REQ = "docker/jit-approval-svc/requirements.txt"
OC_REQ = "docker/openclaw/tools/requirements.txt"

AD_TEXT = "PyYAML==6.0.3\npaho-mqtt==2.2.0\n"
ER_DEV_TEXT = "-r requirements.txt\nhypothesis==6.170.15\n"
ER_RUN_TEXT = "PyYAML==6.0.3\n"


def evaluate(files, author=RENOVATE, labels=(), allow=ALLOW, codeowners=CODEOWNERS, ci_text=CI_TEXT):
    """files: {path: (base, head)}"""
    return policy.evaluate_mr(author=author, target_branch="main", labels=list(labels),
                              files=[FileChange(path=p) for p in files], composes=dict(files),
                              allowlist=allow, codeowners_text=codeowners, merger="automerge-bot",
                              ci_text=ci_text)


def with_pip(mapping):
    allow = copy.deepcopy(ALLOW)
    allow["dependencies"]["pip_requirements"] = mapping
    return allow


class AllowedTests(unittest.TestCase):
    def ok(self, path, base, head):
        v = evaluate({path: (base, head)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.dependency)
        return v

    def test_anomaly_detector_patch(self):
        # !1200: paho-mqtt in the anomaly-detector image
        v = self.ok(AD, AD_TEXT, AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.2.1"))
        self.assertEqual([(c.name, c.kind, c.old, c.new) for c in v.dep_changes],
                         [("paho-mqtt", "patch", "2.2.0", "2.2.1")])
        self.assertEqual(v.required_jobs, ["anomaly-detector:test", "anomaly-detector:build-check"])
        self.assertEqual(v.hold_keys, [(policy.DEP_HOLD_JOB, "paho-mqtt")])

    def test_anomaly_detector_minor(self):
        v = self.ok(AD, AD_TEXT, AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.3.0"))
        self.assertEqual(v.dep_changes[0].kind, "minor")

    def test_event_router_dev_minor(self):
        # !1199: hypothesis, test-only
        v = self.ok(ER_DEV, ER_DEV_TEXT, ER_DEV_TEXT.replace("6.170.15", "6.171.0"))
        self.assertEqual(v.dep_changes[0].name, "hypothesis")
        self.assertEqual(v.required_jobs[0], "event-router:test")
        self.assertIn("event-router:build-check", v.required_jobs)

    def test_test_job_required_outside_a_build_context(self):
        allow = with_pip({"ci/foo/requirements-test.txt": "foo:test"})
        base = "hypothesis==6.170.15\n"
        r = policy.dependency_changes(["ci/foo/requirements-test.txt"],
                                      {"ci/foo/requirements-test.txt": (base, base.replace("170", "171"))},
                                      allow, CI_TEXT)
        self.assertEqual(r.reasons, [])
        self.assertEqual(r.required_jobs, ["foo:test"])

    def test_four_part_version(self):
        base = "foo==1.2.3.4\n"
        v = evaluate({AD: (base, "foo==1.2.3.5\n")})
        self.assertTrue(v.eligible, v.reasons)


class StillManualTests(unittest.TestCase):
    def no(self, files, why=None, **kw):
        v = evaluate(files, **kw)
        self.assertFalse(v.eligible)
        if why:
            self.assertTrue(any(why in r for r in v.reasons), v.reasons)
        return v

    def test_major(self):
        v = self.no({AD: (AD_TEXT, AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==3.0.0"))})
        self.assertTrue(v.major)

    def test_zero_minor_counts_as_major(self):
        base = "PyYAML==6.0.3\nfoo==0.4.1\n"
        v = self.no({AD: (base, base.replace("0.4.1", "0.5.0"))})
        self.assertTrue(v.major)
        # a 0.x patch is fine
        self.assertTrue(evaluate({AD: (base, base.replace("0.4.1", "0.4.2"))}).eligible)

    def test_major_label(self):
        self.no({AD: (AD_TEXT, AD_TEXT.replace("2.2.0", "2.2.1"))}, labels=["major"])

    def test_event_router_runtime_requirements(self):
        self.no({ER_RUN: (ER_RUN_TEXT, ER_RUN_TEXT.replace("6.0.3", "6.0.4"))})

    def test_pyyaml_across_runtime_and_covered_files(self):
        # Renovate bumps PyYAML in every file at once; the event-router runtime
        # file keeps the whole MR manual.
        self.no({AD: (AD_TEXT, AD_TEXT.replace("6.0.3", "6.0.4")),
                 ER_RUN: (ER_RUN_TEXT, ER_RUN_TEXT.replace("6.0.3", "6.0.4"))},
                why="must not change other files")

    def test_jit_and_openclaw_requirements(self):
        for p in (JIT_REQ, OC_REQ):
            self.no({p: ("x==1.0.0\n", "x==1.0.1\n")})

    def test_other_files_in_event_router(self):
        self.no({ER_DEV: (ER_DEV_TEXT, ER_DEV_TEXT.replace("6.170.15", "6.170.16")),
                 "docker/event-router/config.yml": ("a: 1\n", "a: 2\n")},
                why="must not change other files")

    def test_shape(self):
        bad = [
            AD_TEXT + "requests==2.32.0\n",                                   # added line
            AD_TEXT.replace("paho-mqtt==2.2.0\n", ""),                        # removed line
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho_mqtt==2.2.1"),          # renamed
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt>=2.2.1"),          # range
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt[x]==2.2.1"),       # extras
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.2.1 ; python_version>'3'"),
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.2.1  # pinned"),
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.2.1 --hash=sha256:" + "a" * 64),
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.3.0rc1"),       # pre-release
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.1.0"),          # downgrade
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt @ https://x/y.whl"),
            AD_TEXT.replace("paho-mqtt==2.2.0", "paho-mqtt==2.2.1\r"),
            AD_TEXT.replace("paho-mqtt==2.2.0", "--index-url https://evil/"),
        ]
        for head in bad:
            v = evaluate({AD: (AD_TEXT, head)})
            self.assertFalse(v.eligible, head)
        self.no({ER_DEV: (ER_DEV_TEXT, ER_DEV_TEXT.replace("-r requirements.txt", "-r other.txt"))})
        self.no({ER_DEV: (ER_DEV_TEXT, ER_DEV_TEXT.replace("-r requirements.txt",
                                                           "--extra-index-url https://evil/"))})
        self.no({AD: (AD_TEXT, AD_TEXT)}, why="no version change found")

    def test_not_renovate(self):
        head = AD_TEXT.replace("2.2.0", "2.2.1")
        self.no({AD: (AD_TEXT, head)}, author="moltbot", why="dependencies.authors")

    def test_bot_config_never_list_unchanged(self):
        # moltbot's own config MRs still may not touch docker/event-router/**.
        v = evaluate({ER_DEV: (ER_DEV_TEXT, ER_DEV_TEXT.replace("6.170.15", "6.170.16"))},
                     author="moltbot")
        self.assertFalse(v.eligible)
        import botconfig
        cfg = botconfig.config(ALLOW)
        self.assertTrue(botconfig._any(ER_DEV, cfg["never"]))

    def test_bot_not_codeowner(self):
        lines = [l for l in CODEOWNERS.split("\n") if not l.startswith("/docker/anomaly-detector/requirements")]
        self.no({AD: (AD_TEXT, AD_TEXT.replace("2.2.0", "2.2.1"))}, codeowners="\n".join(lines),
                why="CODEOWNERS")

    def test_no_ci_file_fails_closed(self):
        self.no({AD: (AD_TEXT, AD_TEXT.replace("2.2.0", "2.2.1"))}, ci_text=None)


class ConfigTests(unittest.TestCase):
    def test_never_listable(self):
        for path in (ER_RUN, JIT_REQ, "docker/jit-approval-svc/sub/requirements-dev.txt", OC_REQ,
                     "docker/openclaw-gateway/requirements.txt", "docker/x/moltbot-requirements.txt"):
            with self.assertRaises(policy.PolicyError, msg=path):
                policy.dependencies_config(with_pip({path: "x:test"}))
            v = evaluate({AD: (AD_TEXT, AD_TEXT.replace("2.2.0", "2.2.1"))}, allow=with_pip({path: "x:test"}))
            self.assertFalse(v.eligible, path)

    def test_bad_entries(self):
        for mapping in ({AD: ""}, {AD: "anomaly-detector:build-check"}, {AD: "test"}, {AD: None},
                        {"docker/*/requirements.txt": "x:test"}, {"docker/x/../y.txt": "x:test"},
                        {"docker/x/Dockerfile": "x:test"}, ["x"]):
            with self.assertRaises(policy.PolicyError, msg=mapping):
                policy.dependencies_config(with_pip(mapping))

    def test_real_config(self):
        self.assertEqual(CFG["pip"], {AD: "anomaly-detector:test", ER_DEV: "event-router:test"})
        for path in (ER_RUN, JIT_REQ, OC_REQ, "requirements.txt", "ci/automerge/requirements-test.txt"):
            self.assertIsNone(policy.dependency_file_kind(path, CFG), path)
        for path in CFG["pip"]:
            self.assertEqual(policy.dependency_file_kind(path, CFG), "pip")


class RealFileTests(unittest.TestCase):
    def test_files_exist_and_are_plain_pins(self):
        for path in CFG["pip"]:
            for line in (REPO / path).read_text().split("\n"):
                if line and not line.startswith("-r "):
                    self.assertTrue(policy._PIP_PIN.match(line), (path, line))

    def test_codeowners_lists_the_bot(self):
        for path in CFG["pip"]:
            own = policy.codeowner_of(path, CODEOWNERS)
            self.assertIn("@root", own, path)
            self.assertIn(CFG["codeowner"], own, path)
        for path in (ER_RUN, "docker/event-router/config.yml", "docker/anomaly-detector/config.yml"):
            self.assertNotIn(CFG["codeowner"], policy.codeowner_of(path, CODEOWNERS), path)

    def test_test_jobs_exist_and_run_on_mr_and_main(self):
        import yaml

        class Loader(yaml.SafeLoader):
            pass
        Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
        doc = yaml.load(CI_TEXT, Loader=Loader)
        for path, job in CFG["pip"].items():
            self.assertIn(job, doc, job)
            text = str(doc[job])
            self.assertIn("merge_request_event", text, job)
            self.assertIn("main", text, job)
            self.assertIn(str(Path(path).parent) + "/**/*", text, job)

    def test_replay_on_real_files(self):
        for path, name in ((AD, "paho-mqtt"), (ER_DEV, "hypothesis")):
            text = (REPO / path).read_text()
            line = next(l for l in text.split("\n") if l.startswith(name + "=="))
            ver = line.split("==")[1].split(".")
            ver[-1] = str(int(ver[-1]) + 1)
            v = evaluate({path: (text, text.replace(line, name + "==" + ".".join(ver)))})
            self.assertTrue(v.eligible, (path, v.reasons))


class SweepTests(unittest.TestCase):
    def verdict(self):
        v = evaluate({AD: (AD_TEXT, AD_TEXT.replace("2.2.0", "2.2.1"))})
        self.assertTrue(v.eligible, v.reasons)
        return v

    def test_test_job_gates_the_merge(self):
        v = self.verdict()
        mr = {"sha": "abc", "head_pipeline": {"id": 5, "sha": "abc", "status": "success"}}
        ok = [{"id": 1, "name": "anomaly-detector:test", "status": "success"},
              {"id": 2, "name": "anomaly-detector:build-check", "status": "success"}]
        self.assertEqual(automerge.required_job_blockers(FakeGitLab(ok), mr, v), [])
        for jobs in (ok[1:], [dict(ok[0], status="failed"), ok[1]], [dict(ok[0], status="manual"), ok[1]],
                     ok + [{"id": 3, "name": "anomaly-detector:test", "status": "failed"}]):
            self.assertTrue(automerge.required_job_blockers(FakeGitLab(jobs), mr, v), jobs)

    def test_watch_includes_test_job(self):
        jobs = [{"name": n} for n in ("anomaly-detector:test", "anomaly-detector:build", "router:deploy",
                                       "event-router:test", "automerge:test")]
        self.assertEqual(automerge.dep_watch_jobs(["anomaly-detector:test", "anomaly-detector:build-check"],
                                                  jobs),
                         ["anomaly-detector:build", "anomaly-detector:test", "router:deploy"])
        # existing behaviour for Dockerfiles unchanged
        self.assertEqual(automerge.dep_watch_jobs(["novnc:build-check"], jobs), ["router:deploy"])

    def test_revert_text(self):
        v = self.verdict()
        head = AD_TEXT.replace("2.2.0", "2.2.1")
        self.assertEqual(automerge.revert_file_text(head, AD_TEXT, head, v.dep_changes), AD_TEXT)
        later = head.replace("6.0.3", "6.0.4")
        self.assertEqual(automerge.revert_file_text(later, AD_TEXT, head, v.dep_changes),
                         AD_TEXT.replace("6.0.3", "6.0.4"))


if __name__ == "__main__":
    unittest.main()
