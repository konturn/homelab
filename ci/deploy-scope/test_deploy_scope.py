"""Tests for deploy_scope.py: tag selection from sample diffs, plus checks
that scope.yml still covers every file the roles and the inventory read.

Run: python3 -m unittest discover -s ci/deploy-scope -p 'test_*.py' -v
(needs PyYAML; the inventory checks also need ansible-inventory).
"""
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_scope as ds  # noqa: E402

import yaml  # noqa: E402

PLAYBOOKS = ["router", "zwave", "satellite-2", "macmini"]
FULL = ds.FULL


def sel(pb, *files):
    return ds.select(pb, list(files))[0]


class SampleDiffs(unittest.TestCase):
    def test_docs_only_deploys_nothing(self):
        for pb in PLAYBOOKS:
            self.assertEqual(sel(pb, "docs/DEPLOY.md", "README.md", "CLAUDE.md"), [], pb)

    def test_role_readme_and_tests_deploy_nothing(self):
        for pb in PLAYBOOKS:
            self.assertEqual(sel(pb, "ansible/roles/configure-excel-vm/README.md",
                                 "ansible/tests/fetch-vault-secrets/run.sh"), [], pb)

    def test_router_compose_change(self):
        self.assertEqual(sel("router", "docker/docker-compose.yml"),
                         ["configure-homelab-services", "configure-wordpress"])
        for pb in ("zwave", "satellite-2", "macmini"):
            self.assertEqual(sel(pb, "docker/docker-compose.yml"), [], pb)

    def test_single_service_config_change(self):
        self.assertEqual(sel("router", "docker/grafana/provisioning/alerting/policies.yml"),
                         ["configure-homelab-services"])
        self.assertEqual(sel("router", "docker/nginx/router.lab.nkontur.com/internal_http.conf"),
                         ["configure-homelab-services"])

    def test_dhcpd_change_selects_router_network(self):
        self.assertEqual(sel("router", "networking/dhcp/dhcpd.conf"),
                         ["configure-router-network"])
        self.assertEqual(sel("zwave", "networking/dhcp/dhcpd.conf"), [])

    def test_luks_and_disk_files_select_base(self):
        self.assertEqual(sel("router", "base/disk/crypttab", "base/disk/luks-keyfile-check"),
                         ["configure-base"])

    def test_wordpress_and_daemon_json(self):
        self.assertEqual(sel("router", "docker/wordpress/site.yml"), ["configure-wordpress"])
        self.assertEqual(sel("router", "docker/daemon.json"),
                         ["configure-docker", "configure-homelab-services"])
        self.assertEqual(sel("zwave", "docker/daemon.json"), ["configure-docker", "deploy-compose"])

    def test_satellite_files(self):
        f = "docker/promtail/satellite-config.yml"
        self.assertEqual(sel("router", f), [])
        self.assertEqual(sel("zwave", f), ["deploy-compose"])
        self.assertEqual(sel("satellite-2", f), ["deploy-compose"])
        self.assertEqual(sel("macmini", f), [])
        self.assertEqual(sel("zwave", "docker/docker-compose-zwave.yml"), ["deploy-compose"])
        self.assertEqual(sel("satellite-2", "docker/docker-compose-zwave.yml"), [])
        self.assertEqual(sel("zwave", "snapclient/config.txt"), ["configure-snapclient"])
        self.assertEqual(sel("satellite-2", "base/sane/scan.sh"), ["configure-scanner"])

    def test_role_change_selects_that_role_where_used(self):
        f = "ansible/roles/configure-docker/tasks/main.yml"
        self.assertEqual(sel("router", f), ["configure-docker"])
        self.assertEqual(sel("zwave", f), ["configure-docker"])
        self.assertEqual(sel("macmini", f), [])
        self.assertEqual(sel("router", "ansible/roles/configure-zwave/tasks/main.yml"), [])
        self.assertEqual(sel("router", "ansible/roles/configure-excel-vm/tasks/build.yml"),
                         ["configure-excel-vm"])

    def test_selection_keeps_playbook_order(self):
        self.assertEqual(sel("router", "base/msmtp/msmtprc", "networking/dns/dnsmasq.conf",
                             "docker/mqtt/mosquitto.conf"),
                         ["configure-router-network", "configure-homelab-services",
                          "configure-notifications"])

    def test_shared_files_force_full(self):
        for f in (".gitlab-ci.yml", "ansible/inventory.yml", "requirements.txt",
                  "ansible/requirements.yml", "ansible/ansible.cfg",
                  "ansible/roles/fetch-vault-secrets/tasks/main.yml",
                  "ansible/roles/fetch-vault-secrets/files/vault_read.py",
                  "base/validate/homelab-config-check", "ci/deploy-scope/scope.yml",
                  "ci/deploy-scope/deploy_scope.py", "ansible/group_vars/all.yml"):
            for pb in PLAYBOOKS:
                self.assertEqual(sel(pb, "docs/x.md", f), FULL, (pb, f))

    def test_own_playbook_full_other_playbooks_none(self):
        self.assertEqual(sel("router", "ansible/router.yml"), FULL)
        self.assertEqual(sel("zwave", "ansible/router.yml"), [])
        self.assertEqual(sel("zwave", "ansible/zwave.yml"), FULL)
        self.assertEqual(sel("macmini", "ansible/macmini.yml"), FULL)

    def test_unmapped_path_forces_full(self):
        for pb in PLAYBOOKS:
            self.assertEqual(sel(pb, "newdir/thing.conf"), FULL, pb)
        self.assertEqual(sel("router", "base/brand-new-file"), FULL)

    def test_new_role_dir_for_unknown_role_is_not_full(self):
        # A role no playbook uses yet (it only takes effect once a playbook
        # lists it, and that playbook change is a full deploy).
        self.assertEqual(sel("router", "ansible/roles/brand-new/tasks/main.yml"), [])

    def test_rule_naming_unknown_role_forces_full(self):
        rules = [("x/**", ["no-such-role"], "test")]
        self.assertEqual(ds.select("router", ["x/y"], rules=rules)[0], FULL)

    def test_empty_change_deploys_nothing(self):
        for pb in PLAYBOOKS:
            self.assertEqual(sel(pb), [], pb)

    def test_tags_exclude_always_roles(self):
        self.assertEqual(ds.tags_for(["fetch-vault-secrets", "configure-base"]), ["configure-base"])


class ForcedFull(unittest.TestCase):
    def test_reasons(self):
        f = ds.forced_full_reason
        self.assertIsNone(f({"CI_PIPELINE_SOURCE": "push"}))
        self.assertIsNone(f({}))
        self.assertIn("DEPLOY_FULL", f({"CI_PIPELINE_SOURCE": "push", "DEPLOY_FULL": "1"}))
        self.assertIn("schedule", f({"CI_PIPELINE_SOURCE": "schedule"}))
        self.assertIn("web", f({"CI_PIPELINE_SOURCE": "web"}))
        self.assertIsNone(f({"CI_PIPELINE_SOURCE": "web", "DEPLOY_SCOPE": "changed"}))
        self.assertIsNone(f({"CI_PIPELINE_SOURCE": "push", "DEPLOY_FULL": "0"}))


class Patterns(unittest.TestCase):
    def test_match(self):
        self.assertTrue(ds.match("docker/**", "docker/a/b.yml"))
        self.assertFalse(ds.match("docker/**", "dockerfile"))
        self.assertTrue(ds.match("base/systemd/zpool-scrub.*", "base/systemd/zpool-scrub.timer"))
        self.assertTrue(ds.match("ansible/roles/*/README.md", "ansible/roles/x/README.md"))
        self.assertFalse(ds.match("README.md", "docker/README.md"))


# --------------------------------------------------------------------------
# Coverage checks: scope.yml must map what the roles and inventory read.
# --------------------------------------------------------------------------

LITERAL = re.compile(r"CI_PROJECT_DIR'\)\s*(?:\}\}|\+\s*')\s*/?([A-Za-z0-9_./-]+)('\s*\+|\{\{)?")


def role_literal_paths(role):
    """Repo files a role reads through lookup('env', 'CI_PROJECT_DIR').

    A literal ending in "/" is a directory the role copies as a whole (all
    its files count). A literal followed by more Jinja ('/docker/' + item,
    base/nut/{{ item.src }}) or naming a directory without the slash is the
    prefix of a dynamic path; those files come from the inventory and are
    checked there."""
    out = set()
    for f in (ds.ANSIBLE_DIR / "roles" / role).rglob("*.yml"):
        for m in LITERAL.finditer(f.read_text()):
            raw, dynamic = m.group(1), m.group(2)
            p = raw.rstrip("/")
            full = ds.REPO / p
            if not p or dynamic or (full.is_dir() and not raw.endswith("/")):
                continue
            if full.is_dir():
                out.update(str(x.relative_to(ds.REPO)) for x in full.rglob("*") if x.is_file())
            else:
                out.add(p)
    return out


def covers(decision, role):
    return decision == FULL or role in decision


class Coverage(unittest.TestCase):
    def test_every_role_is_tagged_with_its_name(self):
        for pb in PLAYBOOKS:
            plays = yaml.safe_load((ds.ANSIBLE_DIR / f"{pb}.yml").read_text())
            for play in plays:
                for r in play.get("roles") or []:
                    self.assertIsInstance(r, dict, f"{pb}: role entry {r!r} has no tags")
                    tags = r.get("tags") or []
                    want = "always" if r["role"] in ds.ALWAYS_ROLES else r["role"]
                    self.assertIn(want, tags, f"{pb}: role {r['role']} not tagged {want}")

    def test_no_never_tags_in_roles(self):
        # A `never` task inside a role would run under the role's own tag.
        for f in (ds.ANSIBLE_DIR / "roles").rglob("*.yml"):
            self.assertNotRegex(f.read_text(), r"tags:.*\bnever\b", str(f))

    def test_scope_roles_exist_in_playbook(self):
        scope = yaml.safe_load(ds.SCOPE_FILE.read_text())
        for pb, rules in scope["playbooks"].items():
            roles = ds.playbook_roles(pb)
            for rule in rules:
                for r in rule["roles"]:
                    self.assertIn(r, roles, f"scope.yml {pb}: {r} is not a role of {pb}.yml")

    def test_role_file_references_are_mapped(self):
        for pb in PLAYBOOKS:
            for role in ds.playbook_roles(pb):
                paths = role_literal_paths(role)
                for f in paths:
                    self.assertTrue((ds.REPO / f).exists(), f"{role} reads missing {f}")
                    self.assertTrue(covers(sel(pb, f), role),
                                    f"{pb}: {f} (read by {role}) maps to {sel(pb, f)}")

    @unittest.skipUnless(shutil.which("ansible-inventory"), "needs ansible-inventory")
    def test_inventory_config_files_are_mapped(self):
        inv = json.loads(subprocess.run(
            ["ansible-inventory", "-i", str(ds.ANSIBLE_DIR / "inventory.yml"), "--list"],
            capture_output=True, text=True, check=True).stdout)
        hostvars = inv["_meta"]["hostvars"]
        for pb in ("router", "zwave", "satellite-2"):
            roles = ds.playbook_roles(pb)
            role = "configure-homelab-services" if pb == "router" else "deploy-compose"
            self.assertIn(role, roles)
            for h in ds.playbook_hosts(pb, ds.ANSIBLE_DIR / "inventory.yml"):
                hv = hostvars[h["name"]]
                files = [c["src"] for c in hv.get("docker_config", [])]
                files += ["docker/" + c for c in hv.get("docker_compose_file_names", [])]
                for n in hv.get("nginx_config", []):
                    files += [f"docker/nginx/{h['name']}/{n['network']}_{kind}.conf"
                              for kind in ("http", "stream")]
                    files += ["docker/nginx/nginx.conf", "docker/nginx/ssl_config"]
                self.assertTrue(files, h["name"])
                for f in files:
                    self.assertTrue((ds.REPO / f).exists(), f"{h['name']}: {f} missing")
                    self.assertTrue(covers(sel(pb, f), role),
                                    f"{pb}/{h['name']}: {f} maps to {sel(pb, f)}, not {role}")

    def test_every_tracked_file_has_a_decision(self):
        # Not an assertion that nothing is unmapped (unmapped = full deploy is
        # the safe default), but no tracked file may make select() crash.
        files = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ds.REPO),
                                "ls-files"], capture_output=True, text=True).stdout.split()
        for pb in PLAYBOOKS:
            ds.select(pb, files)


class Unreachable(unittest.TestCase):
    def test_closed_port_is_unreachable(self):
        # Bind and close a socket to get a local port nothing listens on.
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        ok, why = ds.reachable({"host": "127.0.0.1"}, port=port, timeout=2)
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_listening_port_is_reachable(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            ok, why = ds.reachable({"host": "127.0.0.1"}, port=s.getsockname()[1], timeout=2)
        self.assertTrue(ok, why)

    def test_run_skips_unreachable_host(self):
        orig_hosts, orig_reach = ds.playbook_hosts, ds.reachable
        ds.playbook_hosts = lambda pb, inv: [{"name": "h", "host": "192.0.2.1", "user": "root"}]
        ds.reachable = lambda h, **kw: (False, "No route to host")
        saved_sha = ds.os.environ.get("CI_COMMIT_SHA")
        ds.os.environ["CI_COMMIT_SHA"] = "0" * 40
        try:
            args = ds.argparse.Namespace(playbook="zwave", private_key="tmp", ansible_args=[])
            self.assertEqual(ds.cmd_run(args), ds.EXIT_UNREACHABLE)
        finally:
            ds.playbook_hosts, ds.reachable = orig_hosts, orig_reach
            if saved_sha is None:
                del ds.os.environ["CI_COMMIT_SHA"]
            else:
                ds.os.environ["CI_COMMIT_SHA"] = saved_sha

    def test_ci_tolerates_unreachable_exit_code(self):
        # .gitlab-ci.yml uses !reference, which safe_load rejects: read the job
        # block as text (up to the next top-level key).
        text = (ds.REPO / ".gitlab-ci.yml").read_text()
        for job in ("zwave:deploy",):
            m = re.search(rf"^{re.escape(job)}:\n((?:[ #].*\n|\n)*)", text, re.M)
            self.assertIsNotNone(m, job)
            self.assertRegex(m.group(1), rf"exit_codes: \[{ds.EXIT_UNREACHABLE}\]", job)


if __name__ == "__main__":
    unittest.main()
