"""Unit tests for gitlab-oob-upgrade.py (python3 -m unittest base/gitlab-oob/test_gitlab_oob.py)."""
import datetime as dt
import importlib.util
import os
import sys
import unittest

_spec = importlib.util.spec_from_file_location(
    "goob", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gitlab-oob-upgrade.py"))
goob = importlib.util.module_from_spec(_spec)
sys.modules["goob"] = goob
_spec.loader.exec_module(goob)

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64


def img(tag, digest=D1, name="gitlab/gitlab-ee"):
    return goob.parse_image(f"{name}:{tag}@{digest}")


COMPOSE = """services:
  gitlab:
    container_name: gitlab
    image: 'gitlab/gitlab-ee:%s@%s'
    environment:
      X: "{{ some_secret }}"
  runner:
    image: 'gitlab/gitlab-runner:v18.0.0@%s'
  other:
    image: 'nginx:1.27@%s'
"""


class ParseTest(unittest.TestCase):
    def test_parse(self):
        i = img("19.6.1-ee.0")
        self.assertEqual(i.ref, f"gitlab/gitlab-ee:19.6.1-ee.0@{D1}")

    def test_rejects_unpinned_and_other_images(self):
        for bad in ("gitlab/gitlab-ee:19.6.1-ee.0", f"nginx:1@{D1}", f"gitlab/gitlab-runner:v1@{D1}"):
            with self.assertRaises(ValueError):
                goob.parse_image(bad)

    def test_desired_ignores_other_images(self):
        text = COMPOSE % ("19.6.2-ee.0", D2, D1, D1)
        self.assertEqual(goob.desired_from_compose(text), img("19.6.2-ee.0", D2))

    def test_desired_needs_exactly_one_line(self):
        with self.assertRaises(ValueError):
            goob.desired_from_compose("services: {}\n")
        twice = COMPOSE % ("19.6.2-ee.0", D2, D1, D1) + f"    image: 'gitlab/gitlab-ce:19.6.2-ce.0@{D2}'\n"
        with self.assertRaises(ValueError):
            goob.desired_from_compose(twice)


class ClassifyTest(unittest.TestCase):
    def test_kinds(self):
        o = img("19.6.1-ee.0")
        self.assertEqual(goob.classify(o, img("19.6.1-ee.0", D2)), "digest")
        self.assertEqual(goob.classify(o, img("19.6.2-ee.0", D2)), "patch")
        self.assertEqual(goob.classify(o, img("19.7.0-ee.0", D2)), "minor")
        self.assertEqual(goob.classify(o, img("20.0.0-ee.0", D2)), "major")
        self.assertEqual(goob.classify(o, img("19.6.1-ce.0", D2, "gitlab/gitlab-ce")), "edition")
        self.assertEqual(goob.classify(o, img("19.6.0-ee.0", D2)), "downgrade")
        self.assertEqual(goob.classify(o, img("rc1", D2)), "unknown")

    def test_api_version(self):
        self.assertEqual(goob.api_version("19.6.1-ee.0"), "19.6.1-ee")


class DecideTest(unittest.TestCase):
    old = img("19.6.1-ee.0")

    def d(self, new, running=None, hold=None, auto=True):
        return goob.decide(desired=new, pinned=self.old, running=running or self.old.ref, hold=hold, auto=auto)

    def test_noop(self):
        self.assertEqual(self.d(self.old).action, "noop")

    def test_drift_beats_everything(self):
        self.assertEqual(self.d(self.old, running="gitlab/gitlab-ee:latest").action, "drift")
        self.assertEqual(goob.decide(desired=self.old, pinned=self.old, running=None, hold=None,
                                     auto=False).action, "drift")

    def test_patch_and_digest_upgrade(self):
        self.assertEqual(self.d(img("19.6.2-ee.0", D2)).action, "upgrade")
        self.assertEqual(self.d(img("19.6.1-ee.0", D2)).action, "upgrade")

    def test_auto_minor_refused_manual_minor_allowed(self):
        new = img("19.7.0-ee.0", D2)
        self.assertEqual(self.d(new, auto=True).action, "refuse")
        self.assertEqual(self.d(new, auto=False).action, "upgrade")

    def test_never_edition_or_downgrade(self):
        self.assertEqual(self.d(img("19.6.1-ce.0", D2, "gitlab/gitlab-ce"), auto=False).action, "refuse")
        self.assertEqual(self.d(img("19.5.9-ee.0", D2), auto=False).action, "refuse")

    def test_held_image_not_retried(self):
        new = img("19.6.2-ee.0", D2)
        self.assertEqual(self.d(new, hold={"image": new.ref, "at": "x", "reason": "y"}).action, "hold")
        other = img("19.6.3-ee.0", D2)
        self.assertEqual(self.d(other, hold={"image": new.ref}).action, "upgrade")


class HelpersTest(unittest.TestCase):
    def test_merge_info(self):
        msg = ("Merge branch 'renovate/x' into 'main'\n\nSee merge request root/homelab!123\n\n"
               "Automerge-Class: gitlab-oob\nAutomerge-Policy: gitlab-oob\n")
        self.assertEqual(goob.merge_info(msg), (True, 123))
        self.assertEqual(goob.merge_info("Merge branch\n\nSee merge request root/homelab!7\n"), (False, 7))
        self.assertEqual(goob.merge_info("direct commit"), (False, None))

    def test_blocking_migrations(self):
        rows = [{"status": "finished"}, {"status": "finalized"},
                {"status": "active", "job_class_name": "A"}, {"status": "failed", "job_class_name": "B"}]
        self.assertEqual(goob.blocking_migrations(rows), ["A (active)", "B (failed)"])
        self.assertEqual(goob.blocking_migrations([{"status": "finished"}]), [])

    def test_swap_compose_line(self):
        o, n = img("19.6.1-ee.0"), img("19.6.2-ee.0", D2)
        text = f"  gitlab:\n    image: '{o.ref}'\n"
        self.assertEqual(goob.swap_compose_line(text, o, n), f"  gitlab:\n    image: '{n.ref}'\n")
        with self.assertRaises(ValueError):
            goob.swap_compose_line(text + text, o, n)
        with self.assertRaises(ValueError):
            goob.swap_compose_line("nothing here", o, n)

    def test_snapshot_naming_and_pruning(self):
        name = goob.snapshot_name("mpool/backups/gitlab-oob", dt.datetime(2026, 11, 24, 3, 4, 5),
                                  img("19.6.1-ee.0"), img("19.6.2-ee.0", D2))
        self.assertEqual(name, "mpool/backups/gitlab-oob@oob-20261124T030405Z-19.6.1-ee.0-to-19.6.2-ee.0")
        snaps = ["ds@manual", "ds@oob-1", "ds@oob-2", "ds@oob-3"]
        self.assertEqual(goob.snapshots_to_prune(snaps, 2), ["ds@oob-1"])
        self.assertEqual(goob.snapshots_to_prune(snaps[:2], 2), [])

    def test_destroy_refuses_foreign_snapshots(self):
        h = goob.Host({"state_dir": "/nonexistent"}, dry_run=True)
        with self.assertRaises(ValueError):
            h.destroy("mpool/backups@manual")


if __name__ == "__main__":
    unittest.main()
