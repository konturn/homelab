"""Offline tests for the canary (docs/CANARY.md). No Docker needed."""

import os
import sys
import tempfile
import unittest

import jinja2.exceptions

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canary  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def worktree(files):
    d = tempfile.mkdtemp()
    for path, text in files.items():
        p = os.path.join(d, path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
    return canary.Renderer(canary.Source(worktree=d))


COMPOSE = """services:
  gitlab:
    image: gitlab/gitlab-ee:{tag}
    privileged: true
    ports: ["443:80"]
    volumes: ["/persistent_data/application/gitlab/data:/var/opt/gitlab"]
    devices: ["/dev/sda:/dev/sda"]
    network_mode: host
    cap_add: [CHOWN, SYS_ADMIN]
    security_opt: [no-new-privileges=true, seccomp=unconfined]
    environment:
      SECRET: "{{{{ vault_gitlab_secret }}}}"
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80/-/health"]
  other:
    image: busybox:1
"""


class SpecTest(unittest.TestCase):
    def setUp(self):
        self.services = canary.load_spec()
        self.repo = canary.Renderer(canary.Source(worktree=REPO))

    def test_every_service_pins_its_image_repos(self):
        for name, svc in self.services.items():
            self.assertTrue(svc.get("image_repos"), name)

    def test_files_land_under_a_mount(self):
        for name, svc in self.services.items():
            mounts = [{**m, "host": "/x" + m["path"]} for m in svc.get("mounts") or []]
            for f in svc.get("files") or []:
                canary._host_path(mounts, f["dest"])  # raises when not

    def test_files_and_compose_services_exist_in_the_repo(self):
        for name, svc in self.services.items():
            svc_def = self.repo.compose_service(svc["compose_service"])
            self.assertIn(canary.image_repo(svc_def["image"]), svc["image_repos"], name)
            for f in svc.get("files") or []:
                if f["src"].endswith("/"):
                    self.assertTrue(self.repo.src.list(f["src"]), f["src"])
                else:
                    self.repo.read(f["src"], render=f.get("render", True))
            for sc in (svc.get("sidecars") or {}).values():
                if sc.get("image_from_compose"):
                    self.repo.compose_service(sc["image_from_compose"])


class RenderTest(unittest.TestCase):
    def test_undefined_secrets_render_as_dummy(self):
        r = worktree({"x.conf": "pw={{ vault_x | trim | replace('$', '$$') }} ip={{ a.b.c }}"})
        self.assertEqual(r.read("x.conf").decode(), "pw=canary-dummy ip=canary-dummy")

    def test_unknown_ansible_filter_passes_through(self):
        r = worktree({"x.conf": "{{ 'a' | ipaddr('host') }}"})
        self.assertEqual(r.read("x.conf").decode(), "a")

    def test_inventory_vars_are_used(self):
        r = worktree({"ansible/inventory.yml":
                      "all:\n  vars:\n    base: /p\n    dir: '{{ base }}/x'\n",
                      "x.conf": "{{ dir }}"})
        self.assertEqual(r.read("x.conf").decode(), "/p/x")

    def test_templates_are_sandboxed(self):
        r = worktree({"x.conf": "{{ ''.__class__.__mro__[1].__subclasses__() }}"})
        with self.assertRaises(jinja2.exceptions.SecurityError):
            r.read("x.conf")


class RunFlagsTest(unittest.TestCase):
    def flags(self):
        r = worktree({"docker/docker-compose.yml": COMPOSE.format(tag="1")})
        notes = []
        image, flags, args = canary.run_flags(r.compose_service("gitlab"), {}, notes)
        return image, flags, notes

    def test_host_bindings_are_not_copied(self):
        _, flags, notes = self.flags()
        joined = " ".join(flags)
        for bad in ("--privileged", "443:80", "/persistent_data", "/dev/sda",
                    "--network", "--device", "-v", "-p"):
            self.assertNotIn(bad, flags)
        self.assertNotIn("/persistent_data", joined)
        self.assertTrue(any("privileged" in n for n in notes))

    def test_dangerous_caps_and_security_opts_are_refused(self):
        _, flags, notes = self.flags()
        self.assertIn("CHOWN", flags)
        self.assertNotIn("SYS_ADMIN", flags)
        self.assertNotIn("seccomp=unconfined", flags)
        self.assertIn("no-new-privileges=true", flags)

    def test_secrets_are_dummies_and_healthcheck_kept(self):
        _, flags, _ = self.flags()
        self.assertIn("SECRET=canary-dummy", flags)
        self.assertIn("curl -f http://localhost:80/-/health", flags)

    def test_image_repo(self):
        self.assertEqual(canary.image_repo("gitlab/gitlab-ee:19.5.3-ee.0@sha256:ab"),
                         "gitlab/gitlab-ee")
        self.assertEqual(canary.image_repo("gitlab-registry.x:443/root/homelab/nginx:latest"),
                         "gitlab-registry.x:443/root/homelab/nginx")


class PlanTest(unittest.TestCase):
    services = {"gitlab": {"name": "gitlab", "compose_service": "gitlab",
                           "triggers": ["docker/gitlab/**"],
                           "files": [{"src": "docker/gitlab/gitlab.rb"}]}}

    def plan(self, changed, tag_after="1"):
        base = worktree({"docker/docker-compose.yml": COMPOSE.format(tag="1")})
        cand = worktree({"docker/docker-compose.yml": COMPOSE.format(tag=tag_after)})
        return canary.plan(self.services, base, cand, changed)

    def test_config_change_selects(self):
        self.assertEqual(self.plan(["docker/gitlab/gitlab.rb"]), ["gitlab"])

    def test_image_bump_selects(self):
        self.assertEqual(self.plan(["docker/docker-compose.yml"], tag_after="2"), ["gitlab"])

    def test_other_compose_change_does_not_select(self):
        self.assertEqual(self.plan(["docker/docker-compose.yml"]), [])

    def test_unrelated_change_does_not_select(self):
        self.assertEqual(self.plan(["docs/CANARY.md"]), [])


class StubHostsTest(unittest.TestCase):
    def test_literal_upstreams_only(self):
        text = """
        upstream x {
            server docker-socket-proxy:2375;
        }
        location / { proxy_pass http://gitlab:80; }
        location /a { proxy_pass http://$backend; }
        location /b { proxy_pass https://10.3.32.6:8200; }
        location /c { proxy_pass http://real.example.com; }
        """
        self.assertEqual(canary.stub_hosts([text]),
                         ["docker-socket-proxy", "gitlab", "real.example.com"])


if __name__ == "__main__":
    unittest.main()
