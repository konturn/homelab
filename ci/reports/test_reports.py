"""Offline tests for the scheduled report parsers and renderers."""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app_updates  # noqa: E402
import common  # noqa: E402
import firmware  # noqa: E402
import os_facts  # noqa: E402
import os_patches  # noqa: E402
import trivy_label  # noqa: E402
import trivy_scan  # noqa: E402


class CommonTest(unittest.TestCase):
    def test_render_header_and_cap(self):
        md = common.render("T", "x" * 200, [f"l{i}" for i in range(30)], generated="2026-10-19T00:00:00Z")
        lines = md.splitlines()
        self.assertEqual(lines[0], "# T")
        self.assertEqual(lines[1], "generated: 2026-10-19T00:00:00Z")
        self.assertTrue(lines[2].startswith("summary: "))
        self.assertLessEqual(len(lines[2]) - len("summary: "), 120)
        self.assertEqual(lines[3], "")
        self.assertEqual(len(lines) - 4, common.MAX_LINES)
        self.assertIn("16 more", lines[-1])

    def test_write_report_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            common.REPORT_DIR = d
            common.write_report("x", "T", "s", ["a"], {"k": 1})
            with open(os.path.join(d, "x.md")) as f:
                self.assertTrue(f.read().startswith("# T\n"))
            with open(os.path.join(d, "x.json")) as f:
                self.assertEqual(json.load(f)["data"], {"k": 1})
            self.assertEqual(sorted(os.listdir(d)), ["x.json", "x.md"])

    def test_versions(self):
        self.assertTrue(common.newer("6.7.0.52", "6.7.0.42"))
        self.assertFalse(common.newer("6.7.0.42", "6.7.0.42"))
        self.assertGreater(common.version_key("6.8.0-50-generic"), common.version_key("6.8.0-9-generic"))


class TrivyTest(unittest.TestCase):
    RESULT = {"Results": [{"Vulnerabilities": [
        {"VulnerabilityID": "CVE-1", "PkgName": "openssl", "Severity": "HIGH", "FixedVersion": "3.0.2", "InstalledVersion": "3.0.1"},
        {"VulnerabilityID": "CVE-1", "PkgName": "openssl", "Severity": "HIGH", "FixedVersion": "3.0.2"},
        {"VulnerabilityID": "CVE-2", "PkgName": "libc", "Severity": "CRITICAL", "FixedVersion": "2.1, 2.10"},
        {"VulnerabilityID": "CVE-3", "PkgName": "zlib", "Severity": "HIGH", "FixedVersion": ""},
    ]}]}

    def test_summarise(self):
        s = trivy_scan.summarise_scan(self.RESULT)
        self.assertEqual((s["critical"], s["high"]), (1, 1))
        self.assertEqual(s["top"][0]["pkg"], "libc")
        self.assertEqual(s["top"][0]["fixed"], "2.10")

    def test_ci_job_containers_skipped(self):
        self.assertTrue(trivy_scan.is_ci_job({"Name": "/runner-abc-project-4-concurrent-0"}))
        self.assertTrue(trivy_scan.is_ci_job({"Name": "/x", "Config": {"Labels": {"com.gitlab.gitlab-runner.job.id": "1"}}}))
        self.assertFalse(trivy_scan.is_ci_job({"Name": "/grafana", "Config": {"Labels": {}}}))

    def test_repo_of(self):
        self.assertEqual(trivy_scan.repo_of("nginx:1.31@sha256:ab"), "nginx")
        self.assertEqual(trivy_scan.repo_of("gitlab-registry.lab:443/root/x:latest"), "gitlab-registry.lab:443/root/x")
        self.assertEqual(trivy_scan.repo_of("grafana/grafana"), "grafana/grafana")

    def test_report_lines(self):
        imgs = [{"ref": "a/b:1@sha256:x", "services": {"b"}, "critical": 0, "high": 2,
                 "top": [{"pkg": "p", "fixed": "2"}]},
                {"ref": "c:1", "services": {"c"}, "critical": 0, "high": 0, "top": []}]
        summary, lines = trivy_scan.build_report(imgs, [])
        self.assertIn("1/2", summary)
        self.assertEqual(lines, ["b (a/b:1): 0C/2H — p→2"])


class LabelTest(unittest.TestCase):
    def test_title_match(self):
        m = trivy_label.mr_matches
        self.assertTrue(m("chore(deps): update grafana/grafana docker tag to v12.4.13", "x", ["grafana/grafana"], []))
        self.assertTrue(m("Update nginx Docker tag to v1.31.2", "x", ["nginx"], []))
        self.assertTrue(m("update lscr.io/linuxserver/tautulli docker digest", "x", ["lscr.io/linuxserver/tautulli"], []))
        self.assertTrue(m("update linuxserver/tautulli", "x", ["lscr.io/linuxserver/tautulli"], []))
        self.assertTrue(m("update nginx", "x", ["docker.io/library/nginx"], []))
        self.assertFalse(m("update nginx-proxy docker tag", "x", ["nginx"], []))
        self.assertFalse(m("update grafana/loki docker tag", "x", ["grafana/grafana"], []))

    def test_branch_match(self):
        m = trivy_label.mr_matches
        self.assertTrue(m("t", "renovate/grafana-grafana-12.x", ["grafana/grafana"], []))
        self.assertTrue(m("t", "renovate/compose-nginx-1.x", ["nginx"], []))
        self.assertFalse(m("t", "renovate/nginx-proxy-1.x", ["nginx"], []))
        self.assertTrue(m("t", "image-update/grafana", [], ["grafana"]))
        self.assertTrue(m("t", "image-update/major/grafana", [], ["grafana"]))
        self.assertFalse(m("t", "image-update/grafana-agent", [], ["grafana"]))


class AppUpdatesTest(unittest.TestCase):
    def test_nextcloud(self):
        out = ("Nextcloud 33.0.6 is available. Get more information on how to update at https://x.\n"
               "Update for notes to version 6.1.0 is available.\nUpdate for spreed to version 23.0.12 is available.\n"
               "3 updates available\n")
        r = app_updates.parse_nextcloud(out)
        self.assertEqual(r["core"], "Nextcloud 33.0.6")
        self.assertEqual([a["app"] for a in r["apps"]], ["notes", "spreed"])

    def test_ha(self):
        states = [{"entity_id": "update.hacs_update", "state": "on",
                   "attributes": {"title": "HACS", "installed_version": "1", "latest_version": "2"}},
                  {"entity_id": "update.x", "state": "off", "attributes": {}},
                  {"entity_id": "update.y", "state": "on",
                   "attributes": {"title": "Y", "latest_version": "3", "skipped_version": "3"}},
                  {"entity_id": "sensor.update", "state": "on", "attributes": {}}]
        self.assertEqual([u["title"] for u in app_updates.parse_ha_updates(states)], ["HACS"])

    def test_grafana_and_wp_parsers(self):
        out = "installed plugins:\n\ngrafana-piechart-panel @ 1.5.0\ngrafana-worldmap-panel @ 0.3.2\n"
        self.assertEqual(app_updates.parse_grafana_plugins(out)[0], ("grafana-piechart-panel", "1.5.0"))
        self.assertEqual(app_updates.parse_css_version("/*\nTheme Name: Astra\nVersion: 4.14.1\n*/"), "4.14.1")
        self.assertEqual(app_updates.image_tag_version("wordpress:7.1.0-php8.3-apache@sha256:ab"), "7.1.0")

    def test_report(self):
        d = {"nextcloud": {"core": None, "apps": [{"app": "notes", "latest": "6.1.0"}]},
             "homeassistant": {"error": "HA API: URLError"},
             "grafana": {"plugins": [{"id": "p", "installed": "1.0", "latest": "1.1", "update": True}]},
             "wordpress": {"core": "7.1.3", "image_version": "7.1.0", "core_latest": "7.1.3",
                           "themes": [], "plugins": []}}
        summary, lines = app_updates.build_report(d)
        self.assertIn("Nextcloud 1 apps", summary)
        self.assertIn("check failed: homeassistant", summary)
        self.assertTrue(any("volume drifted" in l for l in lines))


class FirmwareTest(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(firmware.parse_netgear_header("0x4E47 x\nconfig-file-header\n\nv6.7.0.42 / RNG510\n"), "6.7.0.42")
        v, m = firmware.parse_aruba_version("AOS-8 (MODEL: 515), Version 8.13.1.0 LSR\nWebsite")
        self.assertEqual((v, m), ("8.13.1.0 LSR", "515"))
        v, m = firmware.parse_aruba_version('"ArubaOS (MODEL: ArubaS2500-48P-US), Version 7.4.1.12 (72393)"')
        self.assertEqual((v, m), ("7.4.1.12", "ArubaS2500-48P-US"))
        self.assertEqual(firmware.parse_smart_info("Device Model:     WDC X\nFirmware Version: 83.00A83\n"),
                         {"model": "WDC X", "firmware": "83.00A83"})

    def test_report(self):
        d = {"netgear": [{"name": "netgear-movie", "model": "MS510TXPP", "version": "6.7.0.42"}],
             "aruba_iap": {"name": "aruba-iap", "model": "AP-515", "version": "8.13.1.0 LSR"},
             "aruba_s2500": {"name": "aruba-s2500", "model": "S2500", "error": "credentials pending"},
             "router": {"bios-version": "3.3"},
             "drives": [{"model": "A", "firmware": "1"}, {"model": "A", "firmware": "1"}]}
        summary, lines = firmware.build_report(d)
        self.assertIn("netgear-movie", summary)
        self.assertIn("6.7.0.52 available", lines[0])
        self.assertIn("2x A fw 1", lines[-1])


class OsPatchesTest(unittest.TestCase):
    TODAY = dt.date(2026, 10, 19)

    def test_softwareupdate(self):
        labels, restart = os_facts.parse_softwareupdate(
            "* Label: CLT 26.6\n\tTitle: CLT, Recommended: YES,\n* Label: macOS 26.4\n\tTitle: m, Action: restart,\n")
        self.assertEqual((labels, restart), (["CLT 26.6", "macOS 26.4"], True))
        self.assertEqual(os_facts.parse_softwareupdate("* Label: CLT\n\tTitle: CLT\n"), (["CLT"], False))

    def test_eol(self):
        self.assertTrue(os_patches.eol_status({"codename": "bullseye"}, self.TODAY).startswith("EOL since"))
        self.assertIsNone(os_patches.eol_status({"codename": "noble"}, self.TODAY))
        self.assertTrue(os_patches.eol_status({"id": "macos", "version_id": "13.6"}, self.TODAY).startswith("EOL"))
        self.assertIsNone(os_patches.eol_status({"id": "macos", "version_id": "26.1"}, self.TODAY))

    def test_report(self):
        hosts = {
            "router.lab": {"os": "Ubuntu 24.04", "id": "ubuntu", "codename": "noble",
                           "upgradable": [{"name": "openssl", "security": True}, {"name": "vim", "security": False}],
                           "reboot_required": False, "kernel_running": "6.8.0-45-generic",
                           "kernel_newest": "6.8.0-50-generic"},
            "zwave.lab": {"os": "Raspbian 11", "id": "raspbian", "codename": "bullseye", "upgradable": [],
                          "reboot_required": False, "kernel_running": "6.1.21-v8+", "kernel_newest": "6.1.21-v8+"},
            "macmini.lab": {"os": "macOS 15.6", "id": "macos", "version_id": "15.6", "softwareupdate": [],
                            "brew_outdated": [{"name": "git"}], "brew_error": ""},
            "satellite-2.lab": {"error": "unreachable"},
        }
        summary, lines = os_patches.build_report(hosts, self.TODAY)
        self.assertEqual(summary, "4 hosts: 3 pending (1 security); reboot: router; EOL OS: zwave; no data: satellite-2")
        self.assertTrue(any("kernel 6.8.0-45-generic → 6.8.0-50-generic" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
