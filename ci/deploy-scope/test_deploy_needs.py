"""Deploy ordering: a deploy job must start only after the image builds it
consumes have pushed.

The .docker_build jobs build on the host daemon and tag :latest there, and
compose up recreates a container when the local image behind its tag
changed. A deploy that runs while a build is still going keeps the old image
(main pipeline 5186: snapcast:build finished after router:deploy). Each deploy
job that brings up a compose file with registry images therefore lists the
builds of those images in `needs:` (optional: a build job exists only when
its image changed). Build jobs must have no manual rule: a job that needs an
unplayed manual job is skipped (main pipelines 5228 and 5230).

Run: python3 -m unittest discover -s ci/deploy-scope -p 'test_*.py' -v
"""
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
REGISTRY_IMAGE = re.compile(r"root/homelab/([a-z0-9][a-z0-9._-]*)[:@]")

# Compose file -> the deploy jobs that bring it up.
COMPOSE_DEPLOYS = {
    "docker/docker-compose.yml": ["router:deploy", "router:deploy-service"],
}


class _Loader(yaml.SafeLoader):
    pass


# GitLab's !reference tag; its value does not matter here.
_Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


def ci_config():
    return yaml.load((REPO / ".gitlab-ci.yml").read_text(), Loader=_Loader)


def build_jobs(cfg):
    """IMAGE_NAME -> job name, for every job that extends .docker_build."""
    out = {}
    for name, job in cfg.items():
        if not isinstance(job, dict) or name.startswith("."):
            continue
        ext = job.get("extends", [])
        ext = [ext] if isinstance(ext, str) else ext
        if ".docker_build" in ext:
            out[job["variables"]["IMAGE_NAME"]] = name
    return out


def compose_images(path):
    return set(REGISTRY_IMAGE.findall((REPO / path).read_text()))


def needs_of(job):
    out = {}
    for n in job.get("needs", []):
        if isinstance(n, str):
            out[n] = False
        else:
            out[n["job"]] = bool(n.get("optional", False))
    return out


class DeployNeedsBuilds(unittest.TestCase):
    def setUp(self):
        self.cfg = ci_config()
        self.builds = build_jobs(self.cfg)

    def consumed(self, path):
        return {self.builds[i] for i in compose_images(path) if i in self.builds}

    def test_build_jobs_found(self):
        self.assertIn("snapcast", self.builds)

    def test_every_compose_registry_image_has_a_build_job(self):
        for path in COMPOSE_DEPLOYS:
            for image in sorted(compose_images(path)):
                self.assertIn(image, self.builds,
                              f"{path} uses {image}, but no .docker_build job builds it")

    def test_only_mapped_compose_files_use_registry_images(self):
        for f in sorted((REPO / "docker").glob("docker-compose*.yml")):
            path = f.relative_to(REPO).as_posix()
            if path not in COMPOSE_DEPLOYS:
                self.assertEqual(compose_images(path), set(),
                                 f"{path} uses registry images: add it to COMPOSE_DEPLOYS "
                                 "and make its deploy jobs need the builds")

    def test_deploys_need_the_builds_they_consume(self):
        for path, deploys in COMPOSE_DEPLOYS.items():
            for deploy in deploys:
                needs = needs_of(self.cfg[deploy])
                for build in sorted(self.consumed(path)):
                    self.assertIn(build, needs, f"{deploy} must need {build} ({path})")
                    self.assertTrue(needs[build], f"{deploy}: the need on {build} must be optional")

    def test_deploys_do_not_wait_for_unrelated_builds(self):
        all_builds = set(self.builds.values())
        for path, deploys in COMPOSE_DEPLOYS.items():
            for deploy in deploys:
                extra = (set(needs_of(self.cfg[deploy])) & all_builds) - self.consumed(path)
                self.assertEqual(extra, set(), f"{deploy} needs builds it does not deploy")

    def test_build_jobs_have_no_manual_rule(self):
        for build in sorted(self.builds.values()):
            job = self.cfg[build]
            whens = [r.get("when") for r in job.get("rules", []) if isinstance(r, dict)]
            self.assertNotIn("manual", whens + [job.get("when")],
                             f"{build}: the router deploys need it, a manual rule skips them")

    def test_host_deploys_without_registry_images_wait_for_no_build(self):
        all_builds = set(self.builds.values())
        for deploy in ("zwave:deploy", "satellite-2:deploy", "macmini:deploy"):
            self.assertEqual(set(needs_of(self.cfg[deploy])) & all_builds, set(), deploy)


if __name__ == "__main__":
    unittest.main()
