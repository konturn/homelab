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



ZLIST = "mpool\tDEGRADED\n"
ZSTATUS = """  pool: mpool
 state: DEGRADED
status: One or more devices is currently being resilvered.  The pool will
\tcontinue to function, possibly in a degraded state.
action: Wait for the resilver to complete.
  scan: resilver in progress since Sun Oct 18 21:10:32 2026
\t177T / 366T scanned at 1.30G/s, 74.9T / 267T issued at 563M/s
\t8.60T resilvered, 28.05% done, 4 days 03:25:04 to go
config:

\tNAME             STATE     READ WRITE CKSUM
\tmpool            DEGRADED     0     0     0
\t  raidz2-0       DEGRADED     0     0     0
\t    dev1         ONLINE       0     0     0
\t    replacing-7  DEGRADED     0     0     0
\t      dev22      FAULTED     25     0     0  too many errors
\t      dev17      ONLINE       0     0     0  (resilvering)
\t  raidz2-1       DEGRADED     0     0     0
\t    dev12        FAULTED     55     0     0  too many errors
\t    dev11        ONLINE       0     0     0
\t  raidz2-2       ONLINE       0     0     0
\t    dev14        ONLINE       0     0     0
\tspares
\t  dev30          AVAIL

errors: No known data errors
"""
KNOWN = {"mpool": {"bad_vdevs": ["dev22", "dev12"], "errors": {"dev22": [25, 0, 0], "dev12": [55, 0, 0]}}}
HEALTHY = """  pool: mpool
 state: ONLINE
config:

\tNAME        STATE     READ WRITE CKSUM
\tmpool       ONLINE       0     0     0
\t  raidz2-0  ONLINE       0     0     0
\t    dev1    ONLINE       0     0     0

errors: No known data errors
"""


class StorageGateTests(unittest.TestCase):
    def verdict(self, status=ZSTATUS, lst=ZLIST, known=KNOWN):
        return policy.storage_verdict(lst, status, known)

    def test_parse(self):
        st = policy.parse_zpool_status(ZSTATUS)["mpool"]
        self.assertEqual(st.state, "DEGRADED")
        self.assertTrue(st.resilvering)
        self.assertFalse(st.suspended)
        self.assertEqual(st.rows["dev22"], ("FAULTED", (25, 0, 0)))
        self.assertEqual(st.rows["dev30"], ("AVAIL", None))
        self.assertNotIn("spares", st.rows)

    def test_healthy_pool_passes(self):
        ok, msg = policy.storage_verdict("mpool\tONLINE\n", HEALTHY, None)
        self.assertTrue(ok, msg)

    def test_degraded_resilvering_within_known_state_passes(self):
        ok, msg = self.verdict()
        self.assertTrue(ok, msg)
        self.assertIn("resilvering", msg)

    def test_degraded_without_known_state_blocks(self):
        ok, msg = self.verdict(known=None)
        self.assertFalse(ok)
        self.assertIn("not ONLINE", msg)

    def test_resilver_on_unlisted_online_pool_blocks(self):
        ok, msg = policy.storage_verdict("mpool\tONLINE\n",
                                         HEALTHY.replace(" state: ONLINE", " state: ONLINE\n  scan: resilver in progress since x"), None)
        self.assertFalse(ok)
        self.assertIn("resilvering", msg)

    def test_degraded_without_resilver_blocks(self):
        ok, msg = self.verdict(ZSTATUS.replace("scan: resilver in progress", "scan: resilvered 9T in 1 days"))
        self.assertFalse(ok)
        self.assertIn("no resilver running", msg)

    def test_error_counter_increase_blocks(self):
        for old, new in (("FAULTED     25     0     0", "FAULTED     26     0     0"),
                         ("dev17      ONLINE       0     0     0", "dev17      ONLINE       0     1     0"),
                         ("dev11        ONLINE       0     0     0", "dev11        ONLINE       0     0     3")):
            with self.subTest(new=new):
                ok, msg = self.verdict(ZSTATUS.replace(old, new))
                self.assertFalse(ok)
                self.assertIn("errors", msg)

    def test_new_faulted_or_unavail_vdev_blocks(self):
        for state in ("FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"):
            with self.subTest(state=state):
                ok, msg = self.verdict(ZSTATUS.replace("dev11        ONLINE  ", "dev11        " + state.ljust(8)))
                self.assertFalse(ok)
                self.assertIn("dev11", msg)

    def test_suspended_blocks(self):
        ok, _ = self.verdict(lst="mpool\tSUSPENDED\n")
        self.assertFalse(ok)
        ok, msg = self.verdict(ZSTATUS.replace("status: One or more devices is currently being resilvered.",
                                               "status: One or more devices are faulted in response to IO failures.\n\tThe pool I/O is suspended."))
        self.assertFalse(ok)
        self.assertIn("suspended", msg)

    def test_data_errors_block(self):
        ok, msg = self.verdict(ZSTATUS.replace("No known data errors", "3 data errors, use '-v' for a list"))
        self.assertFalse(ok)
        self.assertIn("data errors", msg)

    def test_unparsable_or_missing_output_blocks(self):
        self.assertFalse(self.verdict(ZSTATUS.replace("FAULTED     25", "FAULTED     2.5K"))[0])
        self.assertFalse(self.verdict(lst="")[0])
        self.assertFalse(self.verdict(status="")[0])
        self.assertFalse(self.verdict(lst="mpool\tDEGRADED\ntank\tONLINE\n")[0])

    def test_vdev_leaving_after_resilver_is_fine(self):
        # dev22 detaches when the replace finishes; the pool is still DEGRADED (dev12)
        # but without a resilver that blocks, as the policy says.
        st = ZSTATUS.replace("\t      dev22      FAULTED     25     0     0  too many errors\n", "")
        self.assertTrue(self.verdict(st)[0])


if __name__ == "__main__":
    unittest.main()
