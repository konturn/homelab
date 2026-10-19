"""Unit tests for the auto-merge policy. Run: python3 -m unittest discover -s ci/automerge -v"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402
from policy import FileChange, PolicyError  # noqa: E402

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64

BASE = f"""volumes:
  grafana-storage:
services:
  radarr:
    image: linuxserver/radarr:6.0.4.10291-ls292@{D1}
    container_name: radarr
    environment:
      - PUID=1000
    restart: unless-stopped
  vault:
    image: hashicorp/vault:2.0.4@{D1}
    container_name: vault
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest@{D1}
  grafana:
    image: grafana/grafana:12.4.0-ubuntu@{D1}
  homeassistant:
    image: "homeassistant/home-assistant:2026.6.1@{D1}"
  jackett:
    image: 'linuxserver/jackett:0.24.2486@{D1}'
  lab_nginx:
    image: nginx:1.31.1-alpine3.23-perl@{D1}
  plex:
    image: plexinc/pms-docker:1.43.3.10896-cb3ebc72d@{D1}
  openclaw-gateway:
    image: gitlab-registry.lab.nkontur.com:443/root/homelab/openclaw:{{{{ openclaw_image_tag | default('latest') }}}}
networks:
  internal:
    driver: bridge
"""

ALLOW = {
    "authors": ["moltbot", "renovate-bot"],
    "target_branch": "main",
    "allowed_files": [policy.COMPOSE_PATH],
    "services": {s: {} for s in ["radarr", "prowlarr", "grafana", "homeassistant", "jackett",
                                 "lab_nginx", "plex"]},
}

CODEOWNERS = """
/ansible/                                @root
/.gitlab-ci.yml                          @root
/docker/docker-compose.yml               @root
/docker/docker-compose-*.yml             @root
/docker/openclaw/                        @root
/ci/                                     @root
"""


def edit(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def evaluate(head: str, *, author="moltbot", files=None, labels=(), merger=None):
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=list(labels),
        files=files or [FileChange(path=policy.COMPOSE_PATH)],
        base_compose=BASE, head_compose=head, allowlist=ALLOW, codeowners_text=CODEOWNERS,
        merger=merger)


class ImageRefTests(unittest.TestCase):
    def test_parse_forms(self):
        r = policy.parse_image_ref(f"'linuxserver/jackett:0.24.2486@{D1}'")
        self.assertEqual((r.name, r.tag, r.digest), ("linuxserver/jackett", "0.24.2486", D1))
        r = policy.parse_image_ref(f"gitlab-registry.lab.nkontur.com:443/root/homelab/x@{D1}  # pinned")
        self.assertEqual((r.name, r.tag), ("gitlab-registry.lab.nkontur.com:443/root/homelab/x", None))

    def test_templated_rejected(self):
        with self.assertRaises(PolicyError):
            policy.parse_image_ref("foo/bar:{{ tag }}")


class VersionTests(unittest.TestCase):
    def test_patch_minor(self):
        self.assertEqual(policy.classify_tag_change("12.4.0-ubuntu", "12.4.13-ubuntu"), "patch")
        self.assertEqual(policy.classify_tag_change("2026.6.1", "2026.7.0"), "minor")
        self.assertEqual(policy.classify_tag_change("6.0.4.10291-ls292", "6.0.4.10291-ls293"), "patch")
        self.assertEqual(policy.classify_tag_change("1.31.1-alpine3.23-perl", "1.31.2-alpine3.24-perl"), "patch")
        self.assertEqual(policy.classify_tag_change("1.43.3.10896-cb3ebc72d", "1.43.4.10920-0a1b2c3d4"), "patch")
        self.assertEqual(policy.classify_tag_change("0.24.2486", "0.24.2492"), "patch")

    def test_rejections(self):
        for old, new in [("2.0.4", "3.0.0"),            # major
                         ("0.63.2", "0.64.2"),          # 0.x minor is breaking
                         ("12.4.0-ubuntu", "12.4.1"),   # flavor change
                         ("1.2.3", "1.2.2"),            # downgrade
                         ("latest", "stable"),          # not a version
                         ("18993091-ls1", "18993092-ls1")]:  # no major.minor
            with self.assertRaises(PolicyError, msg=f"{old}->{new}"):
                policy.classify_tag_change(old, new)


class DiffParserTests(unittest.TestCase):
    """The five sample diffs required by the policy, plus edge cases."""

    def test_digest_bump_ok(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        v = evaluate(head)
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual([(c.service, c.kind) for c in v.changes], [("prowlarr", "digest")])

    def test_patch_and_minor_ok(self):
        head = edit(BASE, f"radarr:6.0.4.10291-ls292@{D1}", f"radarr:6.0.5.10301-ls293@{D2}")
        head = edit(head, f"home-assistant:2026.6.1@{D1}", f"home-assistant:2026.7.0@{D3}")
        head = edit(head, f"jackett:0.24.2486@{D1}'", f"jackett:0.24.2492@{D2}'")
        v = evaluate(head)
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(sorted(c.kind for c in v.changes), ["minor", "patch", "patch"])

    def test_major_bump_rejected(self):
        head = edit(BASE, f"grafana:12.4.0-ubuntu@{D1}", f"grafana:13.0.0-ubuntu@{D2}")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertIn("major", " ".join(v.reasons))

    def test_new_env_var_rejected(self):
        head = edit(BASE, "      - PUID=1000\n", "      - PUID=1000\n      - DEBUG=1\n")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertIn("line count", " ".join(v.reasons))

    def test_env_value_change_in_place_rejected(self):
        head = edit(BASE, "PUID=1000", "PUID=0")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertIn("not a service image line", " ".join(v.reasons))

    def test_non_allowlisted_service_rejected(self):
        head = edit(BASE, f"vault:2.0.4@{D1}", f"vault:2.0.5@{D2}")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertIn("'vault' is not on the auto-merge allowlist", " ".join(v.reasons))

    def test_mixed_change_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        head = edit(head, "restart: unless-stopped", "restart: always")
        v = evaluate(head)
        self.assertFalse(v.eligible)

    def test_mixed_files_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        v = evaluate(head, files=[FileChange(path=policy.COMPOSE_PATH),
                                  FileChange(path="docker/grafana/provisioning/alerting/infrastructure.yml")])
        self.assertFalse(v.eligible)
        self.assertIn("outside the auto-merge scope", " ".join(v.reasons))

    def test_codeowned_extra_path_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        v = evaluate(head, files=[FileChange(path=policy.COMPOSE_PATH), FileChange(path="ci/automerge-allowlist.yml")])
        self.assertFalse(v.eligible)
        self.assertIn("CODEOWNERS-protected paths: ci/automerge-allowlist.yml", " ".join(v.reasons))

    def test_image_name_change_rejected(self):
        head = edit(BASE, f"lscr.io/linuxserver/prowlarr:latest@{D1}", f"evil/prowlarr:latest@{D2}")
        self.assertFalse(evaluate(head).eligible)

    def test_digest_removed_rejected(self):
        head = edit(BASE, f"radarr:6.0.4.10291-ls292@{D1}", "radarr:6.0.4.10291-ls293")
        v = evaluate(head)
        self.assertFalse(v.eligible)
        self.assertIn("not pinned by digest", " ".join(v.reasons))

    def test_non_version_tag_change_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:nightly@{D2}")
        self.assertFalse(evaluate(head).eligible)

    def test_templated_image_rejected(self):
        head = edit(BASE, "default('latest')", "default('next')")
        self.assertFalse(evaluate(head).eligible)

    def test_wrong_author_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        v = evaluate(head, author="someone")
        self.assertFalse(v.eligible)

    def test_second_author_ok(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        self.assertTrue(evaluate(head, author="renovate-bot", merger="automerge-bot").eligible)

    def test_author_is_merger_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        v = evaluate(head, author="moltbot", merger="moltbot")
        self.assertFalse(v.eligible)
        self.assertIn("merger must not be the author", " ".join(v.reasons))

    def test_multi_service_same_image_ok(self):
        # one image used by several allowlisted services, bumped in one MR
        base = (f"services:\n  nginx:\n    image: nginx:1.31.1-alpine3.23-perl@{D1}\n"
                f"  lab_nginx:\n    image: nginx:1.31.1-alpine3.23-perl@{D1}\n")
        head = base.replace(f"1.31.1-alpine3.23-perl@{D1}", f"1.31.2-alpine3.23-perl@{D2}")
        changes = policy.diff_compose(base, head)
        self.assertEqual([c.service for c in changes], ["nginx", "lab_nginx"])

    def test_freeze_label_rejected(self):
        head = edit(BASE, f"prowlarr:latest@{D1}", f"prowlarr:latest@{D2}")
        self.assertFalse(evaluate(head, labels=["freeze"]).eligible)

    def test_new_file_rejected(self):
        v = evaluate(BASE, files=[FileChange(path=policy.COMPOSE_PATH, new_file=True)])
        self.assertFalse(v.eligible)

    def test_no_change_rejected(self):
        self.assertFalse(evaluate(BASE).eligible)

    def test_image_line_outside_services_ignored(self):
        text = "x-common:\n  image: foo/bar:1.0.0@" + D1 + "\nservices:\n  a:\n    image: x/y:1.0.0@" + D1 + "\n"
        _, images = policy.compose_line_services(text)
        self.assertEqual(images, {4: "a"})

    def test_nested_image_key_not_treated_as_service_image(self):
        base = f"services:\n  radarr:\n    image: linuxserver/radarr:6.0.4@{D1}\n    labels:\n      image: x:1.0.0@{D1}\n"
        head = base.replace(f"      image: x:1.0.0@{D1}", f"      image: x:1.0.1@{D2}")
        with self.assertRaises(PolicyError):
            policy.diff_compose(base, head)

    def test_real_compose_file_parses(self):
        path = Path(__file__).resolve().parents[2] / policy.COMPOSE_PATH
        if not path.exists():
            self.skipTest("compose file not present")
        text = path.read_text()
        _, images = policy.compose_line_services(text)
        services = set(images.values())
        for svc in ["radarr", "grafana", "vault", "nextcloud_db", "lab_nginx", "booksonic"]:
            self.assertIn(svc, services)
        self.assertEqual(policy.diff_compose(text, text), [])


class CodeownersTests(unittest.TestCase):
    def test_match(self):
        prot = policy.protected_paths(
            ["docker/docker-compose.yml", "docker/docker-compose-zwave.yml", "ansible/router.yml",
             "docker/grafana/x.yml", "ci/automerge/policy.py", ".gitlab-ci.yml"], CODEOWNERS)
        self.assertEqual(prot, ["docker/docker-compose.yml", "docker/docker-compose-zwave.yml",
                                "ansible/router.yml", "ci/automerge/policy.py", ".gitlab-ci.yml"])


if __name__ == "__main__":
    unittest.main()
