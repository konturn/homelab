"""Tests for the policy scope: every non-major update qualifies except the
denylist, profile-gated services and restricted kinds; deployed compose files
and their hosts; consistency of the real policy, inventory, compose files,
renovate.json and .gitlab-ci.yml (docs/AUTOMERGE.md).

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import json
import re
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
RENOVATE = "project_4_bot_af78a2ea227eba9e10006515fbde375e"
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
OBS = "docker/docker-compose-observability.yml"
ZIGBEE = "docker/docker-compose-zigbee.yml"

ROUTER = policy.Deployment("router.lab.nkontur.com", "router", "docker", "10.4.0.1")
ZWAVE = policy.Deployment("zwave.lab.nkontur.com", "zwave", "zwave", "10.6.128.6")
SAT2 = policy.Deployment("satellite-2.lab.nkontur.com", "satellite-2", "satellite", "10.6.128.11")

BASE = f"""services:
  navidrome:
    image: deluan/navidrome:0.64.2@{D1}
    healthcheck:
      test: ["CMD", "true"]
  pihole:
    image: pihole/pihole:2026.10.0@{D1}
  gitlab:
    image: 'gitlab/gitlab-ee:19.5.0-ee.0@{D1}'
  tautulli:
    image: lscr.io/linuxserver/tautulli:18993091-ls1@{D1}
  blog:
    image: wordpress:7.1.4-php8.3-apache@{D1}
    profiles: [wordpress]
  wordpress_db:
    image: mysql:9.7.2-oraclelinux9@{D1}
    profiles:
      - wordpress
    healthcheck:
      test: ["CMD", "true"]
  influxdb:
    image: influxdb:2.8.0@{D1}
  paperless-redis:
    image: redis:8.10.2-alpine@{D1}
"""

OBS_BASE = f"""services:
  promtail:
    image: grafana/promtail:3.5.8@{D1}
  telegraf:
    image: telegraf:1.40.2@{D1}
"""

ALLOW = {
    "authors": ["moltbot", RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH, OBS],
    "deploy_hosts": {"router.lab.nkontur.com": "router", "zwave.lab.nkontur.com": "zwave",
                     "satellite-2.lab.nkontur.com": "satellite-2"},
    "services": {"influxdb": {"updates": ["digest", "patch"]}},
    "denylist": {"services": {"gitlab": "runs the pipeline that would roll it back"},
                 "images": {"gitlab/gitlab-ee": "GitLab"}},
    "stateful": {"services": ["gitlab"], "images": ["mysql"]},
}
DEPLOYMENTS = {policy.COMPOSE_PATH: [ROUTER], OBS: [ZWAVE, SAT2]}
CODEOWNERS = "/docker/ @root\n/.gitlab-ci.yml @root\n"


def edit(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


def evaluate(composes, *, labels=(), author=RENOVATE, allow=ALLOW, deployments=DEPLOYMENTS, files=None):
    return policy.evaluate_mr(
        author=author, target_branch="main", labels=list(labels),
        files=files or [FileChange(path=p) for p in composes], composes=composes,
        deployments=deployments, allowlist=allow, codeowners_text=CODEOWNERS, merger="automerge-bot")


def router(head):
    return {policy.COMPOSE_PATH: (BASE, head)}


class InvertedPolicyTests(unittest.TestCase):
    def test_unlisted_service_takes_every_non_major_kind(self):
        for old, new, kind in ((f"pihole:2026.10.0@{D1}", f"pihole:2026.10.1@{D2}", "patch"),
                               (f"pihole:2026.10.0@{D1}", f"pihole:2026.11.0@{D2}", "minor"),
                               (f"pihole:2026.10.0@{D1}", f"pihole:2026.10.0@{D2}", "digest"),
                               (f"redis:8.10.2-alpine@{D1}", f"redis:8.10.3-alpine@{D2}", "patch")):
            with self.subTest(new=new):
                v = evaluate(router(edit(BASE, old, new)))
                self.assertTrue(v.eligible, v.reasons)
                self.assertEqual([c.kind for c in v.changes], [kind])
                self.assertEqual(v.deploy_jobs, ["router"])
                self.assertFalse(v.manual)

    def test_pin_digest_qualifies(self):
        base = edit(BASE, f"pihole:2026.10.0@{D1}", "pihole:2026.10.0")
        v = evaluate({policy.COMPOSE_PATH: (base, edit(base, "pihole:2026.10.0", f"pihole:2026.10.0@{D2}"))})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.changes[0].kind, "digest")

    def test_gitlab_is_denied_for_every_kind(self):
        for new in ("19.5.3-ee.0", "19.6.0-ee.0", "19.5.0-ee.0"):
            with self.subTest(new=new):
                v = evaluate(router(edit(BASE, f"gitlab-ee:19.5.0-ee.0@{D1}", f"gitlab-ee:{new}@{D2}")))
                self.assertFalse(v.eligible)
                self.assertTrue(v.denied)
                self.assertTrue(v.manual)
                self.assertIn("gitlab: on the auto-merge denylist (runs the pipeline that would roll it back)",
                              v.reasons)

    def test_denied_image_on_any_service(self):
        allow = copy.deepcopy(ALLOW)
        del allow["denylist"]["services"]["gitlab"]
        v = evaluate(router(edit(BASE, f"gitlab-ee:19.5.0-ee.0@{D1}", f"gitlab-ee:19.5.3-ee.0@{D2}")), allow=allow)
        self.assertTrue(v.denied)
        self.assertIn("(GitLab)", " ".join(v.reasons))

    def test_denylist_entry_needs_a_reason(self):
        allow = copy.deepcopy(ALLOW)
        allow["denylist"]["services"]["pihole"] = ""
        v = evaluate(router(edit(BASE, f"pihole:2026.10.0@{D1}", f"pihole:2026.10.1@{D2}")), allow=allow)
        self.assertFalse(v.eligible)
        self.assertIn("a reason is required", " ".join(v.reasons))

    def test_profile_gated_services_never_qualify(self):
        # !981: merging an update of a retired-profile service must not start it
        for old, new in ((f"mysql:9.7.2-oraclelinux9@{D1}", f"mysql:9.7.4-oraclelinux9@{D2}"),
                         (f"wordpress:7.1.4-php8.3-apache@{D1}", f"wordpress:7.1.4-php8.3-apache@{D2}")):
            with self.subTest(new=new):
                v = evaluate(router(edit(BASE, old, new)))
                self.assertFalse(v.eligible)
                self.assertTrue(v.manual)
                self.assertIn("runs only under compose profile(s) wordpress", " ".join(v.reasons))

    def test_restricted_kind(self):
        v = evaluate(router(edit(BASE, f"influxdb:2.8.0@{D1}", f"influxdb:2.9.1@{D2}")))
        self.assertFalse(v.eligible)
        self.assertIn("minor update of influxdb is not allowed (allowed: digest, patch)", " ".join(v.reasons))
        v = evaluate(router(edit(BASE, f"influxdb:2.8.0@{D1}", f"influxdb:2.8.1@{D2}")))
        self.assertTrue(v.eligible, v.reasons)

    def test_freeze_alone_is_not_manual(self):
        v = evaluate(router(edit(BASE, f"pihole:2026.10.0@{D1}", f"pihole:2026.10.1@{D2}")), labels=["freeze"])
        self.assertFalse(v.eligible)
        self.assertTrue(v.frozen)
        self.assertFalse(v.manual)


class MajorDetectionTests(unittest.TestCase):
    def test_semver_major_and_0x_minor(self):
        for old, new in ((f"pihole:2026.10.0@{D1}", f"pihole:2027.1.0@{D2}"),
                         (f"navidrome:0.64.2@{D1}", f"navidrome:0.65.0@{D2}"),
                         (f"redis:8.10.2-alpine@{D1}", f"redis:9.0.0-alpine@{D2}")):
            with self.subTest(new=new):
                v = evaluate(router(edit(BASE, old, new)))
                self.assertFalse(v.eligible)
                self.assertTrue(v.major)
                self.assertTrue(v.manual)

    def test_ambiguous_schemes_count_as_major(self):
        for old, new in ((f"tautulli:18993091-ls1@{D1}", f"tautulli:18993092-ls1@{D2}"),   # build number
                         (f"tautulli:18993091-ls1@{D1}", f"tautulli:latest@{D2}"),          # floating tag
                         (f"redis:8.10.2-alpine@{D1}", f"redis:8.10.3-bookworm@{D2}"),      # flavor
                         (f"pihole:2026.10.0@{D1}", f"pihole:20261101@{D2}")):              # date
            with self.subTest(new=new):
                v = evaluate(router(edit(BASE, old, new)))
                self.assertFalse(v.eligible)
                self.assertTrue(v.major, v.reasons)
                self.assertIn("treated as major", " ".join(v.reasons))

    def test_renovate_major_label_wins(self):
        # Renovate's versioning may call it major even where ours says minor
        v = evaluate(router(edit(BASE, f"pihole:2026.10.0@{D1}", f"pihole:2026.11.0@{D2}")), labels=["major"])
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)

    def test_major_of_stateful_service_is_heavy(self):
        v = evaluate(router(edit(BASE, f"gitlab-ee:19.5.0-ee.0@{D1}", f"gitlab-ee:20.0.0-ee.0@{D2}")))
        self.assertTrue(v.heavy)

    def test_classify_raises_major_subclass(self):
        for old, new in (("1.2.3", "2.0.0"), ("latest", "stable"), ("1.2.3-alpine", "1.2.4-debian")):
            with self.assertRaises(policy.MajorVersionChange):
                policy.classify_tag_change(old, new)
        with self.assertRaises(PolicyError) as cm:
            policy.classify_tag_change("1.2.3", "1.2.2")      # downgrade: refused, not major
        self.assertNotIsInstance(cm.exception, policy.MajorVersionChange)


class ComposeFileTests(unittest.TestCase):
    def test_service_info(self):
        info = policy.compose_service_info(BASE)
        self.assertEqual(info["blog"].profiles, ["wordpress"])
        self.assertEqual(info["wordpress_db"].profiles, ["wordpress"])
        self.assertFalse(info["pihole"].profiles)
        self.assertTrue(info["navidrome"].healthcheck)
        self.assertTrue(info["wordpress_db"].healthcheck)
        self.assertFalse(info["pihole"].healthcheck)

    def test_satellite_file_deploys_to_each_host(self):
        head = edit(OBS_BASE, f"telegraf:1.40.2@{D1}", f"telegraf:1.40.3@{D2}")
        v = evaluate({OBS: (OBS_BASE, head)})
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(v.deploy_jobs, ["zwave", "satellite-2"])
        self.assertEqual(v.changes[0].file, OBS)
        self.assertFalse(v.fast_track)

    def test_undeployed_compose_file_rejected(self):
        # !969: docker-compose-zigbee.yml is rendered by no host
        v = evaluate({ZIGBEE: (OBS_BASE, edit(OBS_BASE, f"telegraf:1.40.2@{D1}", f"telegraf:1.40.3@{D2}"))})
        self.assertFalse(v.eligible)
        self.assertIn(f"{ZIGBEE} is not deployed by any host", " ".join(v.reasons))

    def test_host_without_deploy_job_rejected(self):
        deps = {**DEPLOYMENTS, OBS: [ZWAVE, policy.Deployment("pi3.lab", None, "x", "10.0.0.3")]}
        head = edit(OBS_BASE, f"telegraf:1.40.2@{D1}", f"telegraf:1.40.3@{D2}")
        v = evaluate({OBS: (OBS_BASE, head)}, deployments=deps)
        self.assertFalse(v.eligible)
        self.assertIn("no per-service deploy", " ".join(v.reasons))

    def test_ci_file_is_outside_scope(self):
        # !989: an image in .gitlab-ci.yml is the CI path itself
        v = evaluate({}, files=[FileChange(path=".gitlab-ci.yml")])
        self.assertFalse(v.eligible)
        self.assertIn("outside the auto-merge scope: .gitlab-ci.yml", " ".join(v.reasons))

    def test_compose_deployments_inherits_like_ansible(self):
        inv = {"all": {
            "vars": {"x": 1},
            "hosts": {"router.lab.nkontur.com": {"ansible_host": "10.4.0.1",
                                                 "docker_compose_file_names": [" docker-compose.yml"]}},
            "children": {"satellites": {
                "vars": {"docker_compose_file_names": ["docker-compose-satellite.yml",
                                                       "docker-compose-observability.yml"],
                         "docker_compose_project_name": "satellite"},
                "hosts": {"zwave.lab.nkontur.com": {"ansible_host": "10.6.128.6",
                                                    "docker_compose_file_names": ["docker-compose-zwave.yml",
                                                                                  "docker-compose-observability.yml"],
                                                    "docker_compose_project_name": "zwave"},
                          "satellite-2.lab.nkontur.com": {"ansible_host": "10.6.128.11",
                                                          "docker_compose_file_names": ["docker-compose-observability.yml"]}},
            }},
        }}
        deps = policy.compose_deployments(inv, ALLOW)
        self.assertEqual(deps[policy.COMPOSE_PATH], [ROUTER])
        self.assertEqual(deps[OBS], [ZWAVE, SAT2])
        self.assertEqual(deps["docker/docker-compose-zwave.yml"], [ZWAVE])
        self.assertNotIn("docker/docker-compose-satellite.yml", deps)   # every satellite overrides it


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealRepoTests(unittest.TestCase):
    """The real policy, inventory, compose files, renovate.json and CI agree."""

    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in (cls.allow.get("services") or {}).items()}
        cls.deps = policy.compose_deployments(yaml.safe_load((REPO / "ansible" / "inventory.yml").read_text()),
                                              cls.allow)
        cls.renovate = json.loads((REPO / "renovate.json").read_text())
        cls.rules = cls.renovate["packageRules"]
        cls.compose = {p: (REPO / p).read_text() for p in policy.compose_files(cls.allow)}
        cls.all_compose = sorted(str(p.relative_to(REPO)) for p in (REPO / "docker").glob("docker-compose*.yml"))

    def images(self, path):
        text = (REPO / path).read_text()
        _, img = policy.compose_line_services(text)
        lines = text.split("\n")
        out = {}
        for i, svc in img.items():
            try:
                out[svc] = (i, policy.parse_image_ref(lines[i].split("image:", 1)[1]))
            except PolicyError:
                pass    # templated (locally built) images
        return out

    def bump(self, path, service, kind):
        """The real file with `service`'s image moved by one `kind` step."""
        text = (REPO / path).read_text()
        i, ref = self.images(path)[service]
        tag = ref.tag
        if kind == "patch":
            tag = re.sub(r"^(v?\d+\.\d+\.)(\d+)", lambda m: m.group(1) + str(int(m.group(2)) + 1), tag)
        elif kind == "minor":
            tag = re.sub(r"^(v?\d+\.)(\d+)\.(\d+)", lambda m: f"{m.group(1)}{int(m.group(2)) + 1}.0", tag)
        elif kind == "major":
            tag = re.sub(r"^(v?)(\d+)\.(\d+)\.(\d+)", lambda m: f"{m.group(1)}{int(m.group(2)) + 1}.0.0", tag)
        lines = text.split("\n")
        old = str(ref)
        new = str(policy.ImageRef(ref.name, tag, D2 if ref.digest != D2 else D1))
        assert old in lines[i], (old, lines[i])
        lines[i] = lines[i].replace(old, new)
        return text, "\n".join(lines)

    def eval_real(self, path, service, kind):
        base, head = self.bump(path, service, kind)
        return policy.evaluate_mr(
            author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=path)],
            composes={path: (base, head)}, deployments=self.deps, allowlist=self.allow,
            codeowners_text=(REPO / "CODEOWNERS").read_text(), merger="automerge-bot")

    # --- the open MRs of 2026-11-06, replayed on the current files -------------

    def test_pihole_zigbee_redis_patch_merge_on_router(self):          # !990, !970, !991
        for svc in ("pihole", "zigbee2mqtt", "paperless-redis"):
            with self.subTest(svc=svc):
                v = self.eval_real(policy.COMPOSE_PATH, svc, "patch")
                self.assertTrue(v.eligible, v.reasons)
                self.assertEqual(v.deploy_jobs, ["router"])

    def test_telegraf_deploys_to_both_satellites(self):                 # !992
        v = self.eval_real(OBS, "telegraf", "patch")
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(sorted(v.deploy_jobs), ["satellite-2", "zwave"])

    def test_zigbee_file_is_not_deployed(self):                          # !969
        if not (REPO / ZIGBEE).exists():
            self.skipTest("file removed")
        base, head = self.bump(ZIGBEE, "zigbee2mqtt", "patch")
        v = policy.evaluate_mr(author=RENOVATE, target_branch="main", labels=[],
                               files=[FileChange(path=ZIGBEE)], composes={ZIGBEE: (base, head)},
                               deployments=self.deps, allowlist=self.allow, codeowners_text="",
                               merger="automerge-bot")
        self.assertFalse(v.eligible)
        self.assertIn("not deployed by any host", " ".join(v.reasons))

    def test_gitlab_stays_manual(self):                                  # !967
        for kind in ("digest", "patch", "minor"):
            v = self.eval_real(policy.COMPOSE_PATH, "gitlab", kind)
            self.assertFalse(v.eligible)
            self.assertTrue(v.denied, v.reasons)

    def test_retired_wordpress_profile_stays_excluded(self):           # !974 / !981
        for svc in ("wordpress_db", "blog"):
            v = self.eval_real(policy.COMPOSE_PATH, svc, "digest")
            self.assertFalse(v.eligible)
            self.assertIn("compose profile(s) wordpress", " ".join(v.reasons))

    def test_majors_never_merge(self):
        for path, svc in ((policy.COMPOSE_PATH, "pihole"), (policy.COMPOSE_PATH, "grafana"), (OBS, "telegraf")):
            v = self.eval_real(path, svc, "major")
            self.assertFalse(v.eligible)
            self.assertTrue(v.major)

    # --- consistency ----------------------------------------------------------

    def test_gitlab_is_on_the_denylist(self):
        d = policy.denylist(self.allow)
        self.assertIn("gitlab", d["services"])
        self.assertIn("gitlab/gitlab-ee", d["images"])

    def test_every_compose_file_deploys_through_a_job(self):
        ci = (REPO / ".gitlab-ci.yml").read_text()
        jobs = list(self.allow["deploy_hosts"].values())
        self.assertEqual(len(jobs), len(set(jobs)))
        for path in policy.compose_files(self.allow):
            self.assertTrue(self.deps.get(path), f"{path} is rendered by no host")
            for d in self.deps[path]:
                self.assertTrue(d.job, f"{d.host} (renders {path}) is missing from deploy_hosts")
                self.assertIn(f"\n{d.job}:deploy-service:\n", ci)
                self.assertIn(f"\\[{d.job}\\]/", ci, f"{d.job}:deploy-service must match its trailer")

    def test_every_scanned_compose_file_is_deployed_or_ignored(self):
        disabled = {f for r in self.rules if r.get("enabled") is False and "matchPackageNames" not in r
                    for f in r.get("matchFileNames", [])}
        listed = set(policy.compose_files(self.allow))
        for path in self.all_compose:
            with self.subTest(path=path):
                if path in listed:
                    self.assertNotIn(path, disabled)
                else:
                    self.assertFalse(self.deps.get(path), f"{path} is deployed: list it under compose_files")
                    self.assertIn(path, disabled, f"{path} is deployed nowhere: disable it in renovate.json")

    def test_services_without_healthcheck_are_denied(self):
        for path, text in self.compose.items():
            for svc, si in policy.compose_service_info(text).items():
                if svc in policy.compose_line_services(text)[1].values() and not si.healthcheck \
                        and not si.profiles:
                    self.assertIsNotNone(policy.deny_reason(svc, None, self.allow),
                                         f"{svc} in {path} has no healthcheck: add one or deny it")

    def test_profile_images_are_ignored_by_renovate(self):
        router_rules = [r for r in self.rules if r.get("enabled") is False and r.get("matchPackageNames")
                        and policy.COMPOSE_PATH in r.get("matchFileNames", [])]
        disabled = {n for r in router_rules for n in r["matchPackageNames"]}
        info = policy.compose_service_info(self.compose[policy.COMPOSE_PATH])
        images = self.images(policy.COMPOSE_PATH)
        gated = {s for s, si in info.items() if si.profiles}
        self.assertEqual(gated, {"blog", "wordpress_db"})
        for svc in gated:
            name = images[svc][1].name
            others = [s for s, (_, r) in images.items() if r.name == name and s not in gated]
            if not others:
                self.assertIn(name, disabled, f"{svc}: Renovate would keep proposing {name}")

    def test_renovate_major_label(self):
        self.assertIn("major", self.renovate["major"].get("addLabels", []))
        self.assertTrue(self.renovate["major"].get("dependencyDashboardApproval"))

    def renovate_disabled(self, name):
        for r in self.rules:
            if r.get("enabled") is False and "matchFileNames" not in r:
                for pat in r.get("matchPackageNames", []):
                    if name == pat or (pat.endswith("/**") and name.startswith(pat[:-2])):
                        return True
        return False

    def automerge_rule(self):
        rs = [r for r in self.rules if r.get("description", "").startswith("Auto-merge class (")]
        self.assertEqual(len(rs), 1)
        return rs[0]

    def test_renovate_automerge_class_rule(self):
        r = self.automerge_rule()
        self.assertEqual(r["matchFileNames"], policy.compose_files(self.allow))
        self.assertEqual(sorted(r["matchUpdateTypes"]), ["digest", "minor", "patch", "pinDigest"])
        for k in ("prConcurrentLimit", "branchConcurrentLimit"):
            self.assertEqual(r[k], 0)
        self.assertNotIn("automerge", r)
        names = r["matchPackageNames"]
        self.assertEqual(names[0], "/.+/")
        excluded = {n[1:] for n in names[1:]}
        self.assertTrue(all(n.startswith("!") for n in names[1:]))
        d = policy.denylist(self.allow)
        restricted = set()
        for path in policy.compose_files(self.allow):
            for svc, (_, ref) in self.images(path).items():
                if self.renovate_disabled(ref.name):
                    continue
                if svc in d["services"] or (self.allow["services"].get(svc) or {}).get("updates"):
                    restricted.add(ref.name)
        self.assertEqual(excluded, set(d["images"]) | restricted)
        # after the weekly digest batching it overrides, before the fast track
        weekly = next(i for i, x in enumerate(self.rules) if x.get("matchUpdateTypes") == ["digest"])
        first_ft = next(i for i, x in enumerate(self.rules) if x.get("description", "").startswith("Fast track"))
        self.assertTrue(weekly < self.rules.index(r) < first_ft)

    def test_restricted_images_have_their_own_rule(self):
        ft = policy.fast_track_config(self.allow)["images"]
        images = self.images(policy.COMPOSE_PATH)
        for svc, opts in self.allow["services"].items():
            kinds = opts.get("updates")
            if not kinds or svc not in images or images[svc][1].name in ft:
                continue
            name = images[svc][1].name
            rules = [r for r in self.rules if r.get("matchPackageNames") == [name] and "matchUpdateTypes" in r]
            self.assertEqual(len(rules), 1, name)
            want = set(kinds) | ({"pinDigest"} if "digest" in kinds else set())
            self.assertEqual(set(rules[0]["matchUpdateTypes"]), want)
            self.assertEqual(rules[0]["prConcurrentLimit"], 0)

    def test_renovate_labels_denylisted_images(self):
        rs = [r for r in self.rules if r.get("description", "").startswith("Auto-merge denylist")]
        self.assertEqual(len(rs), 1)
        self.assertEqual(sorted(rs[0]["matchPackageNames"]), sorted(policy.denylist(self.allow)["images"]))
        self.assertIn("automerge-manual", rs[0]["addLabels"])

    def test_denied_services_use_denied_images(self):
        # a denylisted service whose image is not denied would still get an
        # unlimited Renovate MR the sweep then refuses; keep them together
        d = policy.denylist(self.allow)
        for path in policy.compose_files(self.allow):
            for svc, (_, ref) in self.images(path).items():
                if svc in d["services"] and ref.name in d["images"]:
                    continue
                if svc in d["services"]:
                    self.assertTrue(svc.endswith("-init") or ref.name.startswith(("ghcr.io/headwaymaps/", "pelias/")),
                                    f"{svc}: deny its image {ref.name} too")


if __name__ == "__main__":
    unittest.main()
