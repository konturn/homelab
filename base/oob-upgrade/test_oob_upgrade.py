"""Unit tests for oob-upgrade.py (python3 -m unittest discover -s base/oob-upgrade -p 'test_*.py')."""
import datetime as dt
import importlib.util
import os
import sys
import unittest

_spec = importlib.util.spec_from_file_location(
    "oob", os.path.join(os.path.dirname(os.path.abspath(__file__)), "oob-upgrade.py"))
oob = importlib.util.module_from_spec(_spec)
sys.modules["oob"] = oob
_spec.loader.exec_module(oob)

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
GL = oob.PROFILES["gitlab"]
VA = oob.PROFILES["vault"]


def img(tag, digest=D1, name="gitlab/gitlab-ee", p=GL):
    return oob.parse_image(p, f"{name}:{tag}@{digest}")


def vimg(tag, digest=D1):
    return img(tag, digest, "hashicorp/vault", VA)


COMPOSE = """services:
  gitlab:
    container_name: gitlab
    image: 'gitlab/gitlab-ee:%s@%s'
    environment:
      X: "{{ some_secret }}"
  runner:
    image: 'gitlab/gitlab-runner:v18.0.0@%s'
  vault:
    image: hashicorp/vault:%s@%s
  other:
    image: 'nginx:1.27@%s'
"""


def compose(gl="19.6.2-ee.0", gld=D2, va="2.2.2", vad=D1):
    return COMPOSE % (gl, gld, D1, va, vad, D1)


class ParseTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(img("19.6.1-ee.0").ref, f"gitlab/gitlab-ee:19.6.1-ee.0@{D1}")
        self.assertEqual(vimg("2.2.2").ref, f"hashicorp/vault:2.2.2@{D1}")

    def test_rejects_unpinned_and_other_images(self):
        for bad in ("gitlab/gitlab-ee:19.6.1-ee.0", f"nginx:1@{D1}", f"gitlab/gitlab-runner:v1@{D1}",
                    f"hashicorp/vault:2.2.2@{D1}"):
            with self.assertRaises(ValueError):
                oob.parse_image(GL, bad)
        for bad in ("hashicorp/vault:2.2.2", f"gitlab/gitlab-ee:19.6.1-ee.0@{D1}", f"hashicorp/consul:1@{D1}"):
            with self.assertRaises(ValueError):
                oob.parse_image(VA, bad)

    def test_desired_picks_the_profiles_line(self):
        text = compose()
        self.assertEqual(oob.desired_from_compose(GL, text), img("19.6.2-ee.0", D2))
        self.assertEqual(oob.desired_from_compose(VA, text), vimg("2.2.2"))

    def test_desired_needs_exactly_one_line(self):
        for p in (GL, VA):
            with self.assertRaises(ValueError):
                oob.desired_from_compose(p, "services: {}\n")
        twice = compose() + f"    image: 'gitlab/gitlab-ce:19.6.2-ce.0@{D2}'\n"
        with self.assertRaises(ValueError):
            oob.desired_from_compose(GL, twice)
        with self.assertRaises(ValueError):
            oob.desired_from_compose(VA, compose() + f"    image: hashicorp/vault:2.2.3@{D2}\n")


class ClassifyTest(unittest.TestCase):
    def test_gitlab_kinds(self):
        o = img("19.6.1-ee.0")
        c = lambda n: oob.classify(GL, o, n)  # noqa: E731
        self.assertEqual(c(img("19.6.1-ee.0", D2)), "digest")
        self.assertEqual(c(img("19.6.1-ee.0")), "same")
        self.assertEqual(c(img("19.6.2-ee.0", D2)), "patch")
        self.assertEqual(c(img("19.6.1-ee.1", D2)), "patch")
        self.assertEqual(c(img("19.7.0-ee.0", D2)), "minor")
        self.assertEqual(c(img("20.0.0-ee.0", D2)), "major")
        self.assertEqual(c(img("19.6.1-ce.0", D2, "gitlab/gitlab-ce")), "edition")
        self.assertEqual(c(img("19.6.0-ee.0", D2)), "downgrade")
        self.assertEqual(oob.classify(GL, img("19.6.1-ee.1"), img("19.6.1-ee.0", D2)), "downgrade")
        self.assertEqual(c(img("rc1", D2)), "unknown")

    def test_vault_kinds(self):
        o = vimg("2.2.2")
        c = lambda n: oob.classify(VA, o, n)  # noqa: E731
        self.assertEqual(c(vimg("2.2.2", D2)), "digest")
        self.assertEqual(c(vimg("2.2.3", D2)), "patch")
        self.assertEqual(c(vimg("2.3.0", D2)), "minor")
        self.assertEqual(c(vimg("3.0.0", D2)), "major")
        self.assertEqual(c(vimg("2.2.1", D2)), "downgrade")
        self.assertEqual(c(vimg("2.2.3-ent", D2)), "unknown")

    def test_gitlab_api_version(self):
        self.assertEqual(oob.gitlab_api_version("19.6.1-ee.0"), "19.6.1-ee")


class DecideTest(unittest.TestCase):
    old = img("19.6.1-ee.0")

    def d(self, new, running=None, hold=None, auto=True, p=GL, old=None):
        old = old or self.old
        return oob.decide(p, desired=new, pinned=old, running=running or old.ref, hold=hold, auto=auto)

    def test_noop(self):
        self.assertEqual(self.d(self.old).action, "noop")

    def test_drift_beats_everything(self):
        self.assertEqual(self.d(self.old, running="gitlab/gitlab-ee:latest").action, "drift")
        self.assertEqual(oob.decide(GL, desired=self.old, pinned=self.old, running=None, hold=None,
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

    def test_vault(self):
        o = vimg("2.2.2")
        self.assertEqual(self.d(vimg("2.2.3", D2), p=VA, old=o).action, "upgrade")
        self.assertEqual(self.d(vimg("2.3.0", D2), p=VA, old=o).action, "refuse")
        self.assertEqual(self.d(vimg("2.3.0", D2), p=VA, old=o, auto=False).action, "upgrade")
        self.assertEqual(self.d(vimg("2.2.3-ent", D2), p=VA, old=o, auto=False).action, "refuse")


class HelpersTest(unittest.TestCase):
    def test_merge_info(self):
        msg = ("Merge branch 'renovate/x' into 'main'\n\nSee merge request root/homelab!123\n\n"
               "Automerge-Class: oob\nAutomerge-Policy: oob\n")
        self.assertEqual(oob.merge_info(msg), (True, 123))
        legacy = msg.replace("Automerge-Class: oob", "Automerge-Class: gitlab-oob")
        self.assertEqual(oob.merge_info(legacy), (True, 123))
        self.assertEqual(oob.merge_info("Merge branch\n\nSee merge request root/homelab!7\n"), (False, 7))
        self.assertEqual(oob.merge_info("direct commit"), (False, None))
        # a trailer only counts on its own line
        self.assertEqual(oob.merge_info("text Automerge-Class: oob\nSee merge request r/h!8"), (False, 8))

    def test_blocking_migrations(self):
        rows = [{"status": "finished"}, {"status": "finalized"},
                {"status": "active", "job_class_name": "A"}, {"status": "failed", "job_class_name": "B"}]
        self.assertEqual(oob.blocking_migrations(rows), ["A (active)", "B (failed)"])
        self.assertEqual(oob.blocking_migrations([{"status": "finished"}]), [])

    def test_vault_status_problem(self):
        ok = {"initialized": True, "sealed": False, "version": "2.2.3"}
        self.assertIsNone(oob.vault_status_problem(ok, "2.2.3"))
        self.assertIsNone(oob.vault_status_problem(dict(ok, version="2.2.3+ent"), "2.2.3"))
        self.assertIn("sealed", oob.vault_status_problem(dict(ok, sealed=True), "2.2.3"))
        self.assertIn("sealed", oob.vault_status_problem({"initialized": True}, "2.2.3"))
        self.assertIn("not initialized", oob.vault_status_problem(dict(ok, initialized=False), "2.2.3"))
        self.assertIn("2.2.2", oob.vault_status_problem(dict(ok, version="2.2.2"), "2.2.3"))

    def test_swap_compose_line(self):
        o, n = img("19.6.1-ee.0"), img("19.6.2-ee.0", D2)
        text = f"  gitlab:\n    image: '{o.ref}'\n"
        self.assertEqual(oob.swap_compose_line(text, o, n), f"  gitlab:\n    image: '{n.ref}'\n")
        with self.assertRaises(ValueError):
            oob.swap_compose_line(text + text, o, n)
        with self.assertRaises(ValueError):
            oob.swap_compose_line("nothing here", o, n)

    def test_snapshot_naming_and_pruning(self):
        name = oob.snapshot_name("mpool/backups/gitlab-oob", dt.datetime(2026, 11, 24, 3, 4, 5),
                                 img("19.6.1-ee.0"), img("19.6.2-ee.0", D2))
        self.assertEqual(name, "mpool/backups/gitlab-oob@oob-20261124T030405Z-19.6.1-ee.0-to-19.6.2-ee.0")
        snaps = ["ds@manual", "ds@oob-1", "ds@oob-2", "ds@oob-3"]
        self.assertEqual(oob.snapshots_to_prune(snaps, 2), ["ds@oob-1"])
        self.assertEqual(oob.snapshots_to_prune(snaps[:2], 2), [])

    def test_destroy_refuses_foreign_snapshots(self):
        h = oob.Host({"state_dir": "/nonexistent", "service": "vault"}, dry_run=True)
        with self.assertRaises(ValueError):
            h.destroy("mpool/backups@manual")

    def test_mirror_and_snapshot_dirs_are_per_service(self):
        h = oob.Host({"state_dir": "/x", "service": "vault", "mirror_mount": "/mpool/backups/vault-oob"}, True)
        self.assertEqual(h.mirror_dir(), "/mpool/backups/vault-oob/vault")
        self.assertEqual(h.snapshot_dir("mpool/backups/vault-oob@oob-1"),
                         "/mpool/backups/vault-oob/.zfs/snapshot/oob-1/vault")
        # GitLab keeps the mirror path it had before Vault joined
        g = oob.Host({"state_dir": "/x", "service": "gitlab", "mirror_mount": "/mpool/backups/gitlab-oob"}, True)
        self.assertEqual(g.mirror_dir(), "/mpool/backups/gitlab-oob/gitlab")


class HealthTest(unittest.TestCase):
    class FakeHost:
        def __init__(self, service, **kw):
            self.cfg = {"service": service}
            self.kw = kw

        def container_health(self):
            return self.kw.get("health", "healthy")

        def vault_status(self):
            return self.kw.get("status", {"initialized": True, "sealed": False, "version": "2.2.3"})

        def vault_canary(self):
            return self.kw.get("canary")

    def test_vault_gate(self):
        new = vimg("2.2.3", D2)
        self.assertIsNone(oob.health_once(self.FakeHost("vault"), new, {}))
        self.assertEqual(oob.health_once(self.FakeHost("vault", health="starting"), new, {}), "container starting")
        sealed = {"initialized": True, "sealed": True, "version": "2.2.3"}
        self.assertEqual(oob.health_once(self.FakeHost("vault", status=sealed), new, {}), "Vault is sealed")
        self.assertEqual(oob.health_once(self.FakeHost("vault", canary="KV read failed: HTTPError"), new, {}),
                         "KV read failed: HTTPError")


if __name__ == "__main__":
    unittest.main()
