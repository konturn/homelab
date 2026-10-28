"""Tests for the fast-track class (docs/AUTOMERGE.md, "Fast track").

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402
from policy import FileChange, PolicyError  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]
RENOVATE = "project_4_bot_renovate"

# Real digests: nextcloud:34.0.5-apache before and after the upstream rebuild of
# 2026-10-28, and the nextcloud/mariadb references from !882 and !878.
NC_OLD = "sha256:1f9212adecae32010cdbf137d7e0db78d08c696dc3e77f1eddcbf902d10209b4"
NC_NEW = "sha256:515c072ea1cf82ece3503f301fdf75dd5a7148abcc8fa10aaefd5ec56643b703"
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64

BASE = f"""services:
  plex:
    image: plexinc/pms-docker:1.43.4.10903-e5521bd8c@{D1}
  nginx:
    image: nginx:1.31.2-alpine3.23-perl@{D1}
  lab_nginx:
    image: nginx:1.31.2-alpine3.23-perl@{D1}
  iot_nginx:
    image: nginx:1.31.2-alpine3.23-perl@{D1}
  bluebubbles-relay:
    image: nginx:1.31.2-alpine3.23-perl@{D1}
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest@{D1}
  homeassistant:
    image: "homeassistant/home-assistant:2026.10.2@{D1}"
  bitwarden:
    image: 'vaultwarden/server:1.37.4-alpine@{D1}'
  nextcloud_db:
    image: mariadb:13.0.2@{D1}
  nextcloud:
    image: nextcloud:34.0.5-apache@{NC_OLD}
    container_name: nextcloud
"""

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "allowed_files": [policy.COMPOSE_PATH],
    "services": {
        "plex": {}, "nginx": {}, "lab_nginx": {}, "iot_nginx": {}, "bluebubbles-relay": {},
        "prowlarr": {}, "homeassistant": {},
        "bitwarden": {"updates": ["digest"]},
        "nextcloud": {"updates": ["digest"]},
    },
    "fast_track": {
        "enabled": True,
        "authors": [RENOVATE],
        "max_per_24h": 4,
        "images": {
            "nginx": {}, "plexinc/pms-docker": {}, "homeassistant/home-assistant": {},
            "nextcloud": {"updates": ["digest"]}, "vaultwarden/server": {"updates": ["digest"]},
        },
    },
}

CODEOWNERS = "/docker/docker-compose.yml @root\n/ci/ @root\n"


def edit(text, old, new):
    assert old in text, old
    return text.replace(old, new)


def evaluate(head, *, base=BASE, author=RENOVATE, allow=ALLOW, files=None):
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=[],
        files=files or [FileChange(path=policy.COMPOSE_PATH)],
        base_compose=base, head_compose=head, allowlist=allow, codeowners_text=CODEOWNERS,
        merger="automerge-bot")


class FastTrackMatcherTests(unittest.TestCase):
    def test_nextcloud_same_tag_rebuild_qualifies(self):
        # the real pending update: same tag, new digest
        v = evaluate(edit(BASE, NC_OLD, NC_NEW))
        self.assertTrue(v.eligible, v.reasons)
        self.assertTrue(v.fast_track, v.notes)
        self.assertEqual([(c.service, c.kind) for c in v.changes], [("nextcloud", "digest")])

    def test_nextcloud_patch_release_not_allowed(self):
        # a version change runs `occ upgrade`: not auto-merged in any class
        v = evaluate(edit(BASE, f"nextcloud:34.0.5-apache@{NC_OLD}", f"nextcloud:34.0.6-apache@{D2}"))
        self.assertFalse(v.eligible)
        self.assertFalse(v.fast_track)
        self.assertIn("patch update of nextcloud is not allowed", " ".join(v.reasons))

    def test_mr_882_nextcloud_major_never_qualifies(self):
        # !882: nextcloud 33.0.10-apache -> 34.0.5-apache (major), opened by moltbot
        base = edit(BASE, f"nextcloud:34.0.5-apache@{NC_OLD}", f"nextcloud:33.0.10-apache@{D1}")
        for author in ("moltbot", RENOVATE):
            with self.subTest(author=author):
                v = evaluate(edit(base, f"nextcloud:33.0.10-apache@{D1}", f"nextcloud:34.0.5-apache@{NC_OLD}"),
                             base=base, author=author)
                self.assertFalse(v.eligible)
                self.assertFalse(v.fast_track)
                self.assertIn("major version change", " ".join(v.reasons))

    def test_mr_878_mariadb_major_never_qualifies(self):
        base = edit(BASE, f"mariadb:13.0.2@{D1}", f"mariadb:12.3.3@{D1}")
        v = evaluate(edit(base, f"mariadb:12.3.3@{D1}", f"mariadb:13.0.2@{D2}"), base=base)
        self.assertFalse(v.eligible)
        self.assertFalse(v.fast_track)

    def test_database_digest_not_fast_tracked(self):
        v = evaluate(edit(BASE, f"mariadb:13.0.2@{D1}", f"mariadb:13.0.2@{D2}"))
        self.assertFalse(v.eligible)
        self.assertFalse(v.fast_track)
        self.assertIn("'nextcloud_db' is not on the auto-merge allowlist", " ".join(v.reasons))

    def test_nginx_digest_on_every_service_qualifies(self):
        v = evaluate(BASE.replace(f"1.31.2-alpine3.23-perl@{D1}", f"1.31.2-alpine3.23-perl@{D2}"))
        self.assertTrue(v.fast_track, v.reasons + v.notes)
        self.assertEqual(sorted(c.service for c in v.changes),
                         ["bluebubbles-relay", "iot_nginx", "lab_nginx", "nginx"])

    def test_plex_patch_qualifies(self):
        v = evaluate(edit(BASE, f"pms-docker:1.43.4.10903-e5521bd8c@{D1}", f"pms-docker:1.43.5.11079-b3b13e506@{D2}"))
        self.assertTrue(v.fast_track, v.reasons + v.notes)
        self.assertEqual(v.changes[0].kind, "patch")

    def test_fast_track_image_minor_needs_human_in_every_class(self):
        for old, new in ((f"pms-docker:1.43.4.10903-e5521bd8c@{D1}", f"pms-docker:1.44.0.11100-0a1b2c3d4@{D2}"),
                         (f"home-assistant:2026.10.2@{D1}", f"home-assistant:2026.11.0@{D2}"),
                         (f"nginx:1.31.2-alpine3.23-perl@{D1}", f"nginx:1.32.0-alpine3.23-perl@{D2}")):
            with self.subTest(new=new):
                v = evaluate(BASE.replace(old, new), author="moltbot")
                self.assertFalse(v.eligible)
                self.assertIn("minor update", " ".join(v.reasons))

    def test_non_fast_track_image_keeps_general_rules(self):
        v = evaluate(edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertFalse(v.fast_track)
        self.assertIn("is not a fast-track image", " ".join(v.notes))

    def test_other_author_is_general_class_only(self):
        v = evaluate(edit(BASE, NC_OLD, NC_NEW), author="moltbot")
        self.assertTrue(v.eligible, v.reasons)
        self.assertFalse(v.fast_track)
        self.assertIn("not a fast-track author", " ".join(v.notes))

    def test_two_images_in_one_mr_not_fast_track(self):
        head = edit(edit(BASE, NC_OLD, NC_NEW), f"server:1.37.4-alpine@{D1}", f"server:1.37.4-alpine@{D2}")
        v = evaluate(head)
        self.assertTrue(v.eligible, v.reasons)
        self.assertFalse(v.fast_track)
        self.assertIn("more than one image", " ".join(v.notes))

    def test_partial_image_move_not_fast_track(self):
        # same image, services moving to different references: not "one image line"
        head = edit(BASE, f"  nginx:\n    image: nginx:1.31.2-alpine3.23-perl@{D1}",
                    f"  nginx:\n    image: nginx:1.31.2-alpine3.23-perl@{D2}")
        head = edit(head, f"  lab_nginx:\n    image: nginx:1.31.2-alpine3.23-perl@{D1}",
                    f"  lab_nginx:\n    image: nginx:1.31.3-alpine3.23-perl@{D2}")
        v = evaluate(head)
        self.assertFalse(v.fast_track)

    def test_extra_file_or_line_disqualifies(self):
        head = edit(BASE, NC_OLD, NC_NEW)
        v = evaluate(head, files=[FileChange(path=policy.COMPOSE_PATH), FileChange(path="renovate.json")])
        self.assertFalse(v.eligible)
        self.assertFalse(v.fast_track)
        v = evaluate(edit(head, "container_name: nextcloud", "container_name: nc"))
        self.assertFalse(v.eligible)
        self.assertFalse(v.fast_track)

    def test_disabled_class(self):
        allow = copy.deepcopy(ALLOW)
        allow["fast_track"]["enabled"] = False
        v = evaluate(edit(BASE, NC_OLD, NC_NEW), allow=allow)
        self.assertTrue(v.eligible, v.reasons)
        self.assertFalse(v.fast_track)
        # minor stays blocked for listed images even with the class off
        v = evaluate(edit(BASE, f"home-assistant:2026.10.2@{D1}", f"home-assistant:2026.11.0@{D2}"), allow=allow)
        self.assertFalse(v.eligible)

    def test_malformed_config_fails_closed(self):
        for images in ({"nextcloud": {"updates": ["minor"]}}, {"nextcloud": {"updates": "digest"}},
                       {"nextcloud": {"updates": []}}, {"nextcloud": ["digest"]}):
            with self.subTest(images=images):
                allow = copy.deepcopy(ALLOW)
                allow["fast_track"]["images"] = images
                v = evaluate(edit(BASE, NC_OLD, NC_NEW), allow=allow)
                self.assertFalse(v.eligible)
                self.assertFalse(v.fast_track)

    def test_service_updates_restriction(self):
        allow = copy.deepcopy(ALLOW)
        allow["services"]["prowlarr"] = {"updates": ["patch"]}
        v = evaluate(edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}"), allow=allow)
        self.assertFalse(v.eligible)
        allow["services"]["prowlarr"] = {"updates": ["major"]}
        with self.assertRaises(PolicyError):
            policy.allowed_kinds("prowlarr", "lscr.io/linuxserver/prowlarr", allow)


class SweepModeTests(unittest.TestCase):
    def setUp(self):
        import automerge
        self.am = automerge
        self._env = os.environ.get("AUTOMERGE_DRY_RUN")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("AUTOMERGE_DRY_RUN", None)
        else:
            os.environ["AUTOMERGE_DRY_RUN"] = self._env

    def test_dry_modes(self):
        cases = {"": (False, False), "1": (True, False), "true": (True, False),
                 "all": (True, True), "ALL": (True, True), "0": (False, False)}
        for env, want in cases.items():
            with self.subTest(env=env):
                os.environ["AUTOMERGE_DRY_RUN"] = env
                self.assertEqual(self.am.dry_modes(), want)
        os.environ["AUTOMERGE_DRY_RUN"] = ""
        self.assertEqual(self.am.dry_modes(cli_dry_run=True), (True, True))

    def test_rate_gate_per_class(self):
        ft = {"labels": ["automerged", self.am.LABEL_FAST_TRACK]}
        gen = {"labels": ["automerged"]}
        allow = {**ALLOW, "max_per_24h": 3}
        self.assertIsNone(self.am.rate_gate([ft] * 3 + [gen] * 2, allow, fast_track=True))
        self.assertIsNotNone(self.am.rate_gate([ft] * 4, allow, fast_track=True))
        self.assertIsNone(self.am.rate_gate([ft] * 4 + [gen] * 2, allow, fast_track=False))
        self.assertIsNotNone(self.am.rate_gate([gen] * 3, allow, fast_track=False))


class RepoConfigTests(unittest.TestCase):
    """The real allowlist, renovate.json and compose file must agree."""

    @classmethod
    def setUpClass(cls):
        if yaml is None:
            raise unittest.SkipTest("PyYAML not installed")
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-allowlist.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in cls.allow["services"].items()}
        cls.ft = policy.fast_track_config(cls.allow)
        compose = (REPO / policy.COMPOSE_PATH).read_text()
        _, images = policy.compose_line_services(compose)
        lines = compose.split("\n")
        cls.service_image = {}
        for i, svc in images.items():
            try:
                cls.service_image[svc] = policy.parse_image_ref(lines[i].split("image:", 1)[1]).name
            except PolicyError:
                pass  # templated (locally built) images

    def test_fast_track_config_valid(self):
        self.assertTrue(self.ft["enabled"])
        self.assertTrue(self.ft["images"])
        for a in self.ft["authors"]:
            self.assertIn(a, self.allow["authors"])
        self.assertNotIn("moltbot", self.ft["authors"])
        for svc in self.allow["services"]:
            image = self.service_image.get(svc)
            if image:
                policy.allowed_kinds(svc, image, self.allow)   # raises if malformed

    def test_renovate_rule_matches_allowlist(self):
        rules = json.loads((REPO / "renovate.json").read_text())["packageRules"]
        ft_rules = [r for r in rules if r.get("description", "").startswith("Fast track")]
        self.assertEqual(len(ft_rules), 2)
        for r in ft_rules:
            self.assertEqual(sorted(r["matchPackageNames"]), sorted(self.ft["images"]))
            self.assertEqual(r["matchFileNames"], [policy.COMPOSE_PATH])
        prompt = [r for r in ft_rules if "matchUpdateTypes" in r]
        self.assertEqual(len(prompt), 1)
        self.assertEqual(sorted(prompt[0]["matchUpdateTypes"]), ["digest", "patch", "pinDigest"])
        self.assertNotIn("automerge", prompt[0])   # merging is the sweep's job
        # the fast-track rule must come after the weekly digest batching it overrides
        weekly = next(i for i, r in enumerate(rules) if r.get("matchUpdateTypes") == ["digest"])
        self.assertGreater(rules.index(prompt[0]), weekly)

    def test_every_service_of_a_fast_track_image_is_allowlisted(self):
        # Renovate changes every line of an image in one MR; one unlisted
        # service would make every such MR ineligible.
        for image in self.ft["images"]:
            users = [s for s, i in self.service_image.items() if i == image]
            self.assertTrue(users, f"{image} is not used in the compose file")
            for svc in users:
                self.assertIn(svc, self.allow["services"], f"{svc} uses fast-track image {image}")

    def test_no_database_image_is_fast_tracked(self):
        for image in self.ft["images"]:
            for db in ("mariadb", "mysql", "postgres", "redis", "mongo", "influxdb"):
                self.assertNotIn(db, image)
        for svc in ("nextcloud_db", "wordpress_db", "influxdb", "paperless-redis"):
            self.assertNotIn(svc, self.allow["services"])

    def test_migrating_apps_are_digest_only(self):
        for svc in ("nextcloud", "bitwarden", "blog"):
            self.assertEqual(policy.allowed_kinds(svc, self.service_image[svc], self.allow), ("digest",))

    def test_external_probes(self):
        for svc, opts in self.allow["services"].items():
            if opts.get("probe_via") is not None:
                self.assertEqual(opts["probe_via"], "external", svc)
                self.assertTrue(opts.get("probe"), svc)


if __name__ == "__main__":
    unittest.main()
