"""Tests for pre-upgrade backups (docs/AUTOMERGE.md, "Pre-upgrade backups"):
the policy option, the deploy plan, the playbook's backup/rollback ordering,
the rollback issue text, and ci/automerge/pre-upgrade-backup.sh itself (run
against scratch directories with a stub `docker`).

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import filecmp
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402
import policy  # noqa: E402
from policy import FileChange, PolicyError  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "ci" / "automerge" / "pre-upgrade-backup.sh"
RENOVATE = "project_4_bot_renovate"
D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64

BASE = f"""services:
  homeassistant:
    image: "homeassistant/home-assistant:2026.11.0@{D1}"
  influxdb:
    image: influxdb:2.9.1@{D1}
  grafana:
    image: grafana/grafana:13.3.0-ubuntu@{D1}
"""

ALLOW = {
    "authors": [RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH],
    "services": {
        "homeassistant": {"pre_upgrade_backup": "homeassistant"},
        "influxdb": {"pre_upgrade_backup": "influxdb"},
    },
    "pre_upgrade_backups": {"dir": "/mpool/backups/automerge-pre-upgrade", "keep": 2,
                            "rate_mb": 40, "timeout": 1800},
}


def evaluate(head, allow=ALLOW):
    return policy.evaluate_mr(
        author=RENOVATE, target_branch="main", labels=[], files=[FileChange(path=policy.COMPOSE_PATH)],
        base_compose=BASE, head_compose=head, allowlist=allow, codeowners_text="", merger="bot")


class PolicyTests(unittest.TestCase):
    def test_minor_updates_are_eligible_with_a_backup(self):
        for old, new, svc in (("home-assistant:2026.11.0", "home-assistant:2026.12.0", "homeassistant"),
                              ("influxdb:2.9.1", "influxdb:2.10.0", "influxdb")):
            with self.subTest(svc=svc):
                v = evaluate(BASE.replace(f"{old}@{D1}", f"{new}@{D2}"))
                self.assertTrue(v.eligible, v.reasons)
                self.assertEqual(v.changes[0].kind, "minor")
                self.assertIn(f"{svc}: {svc} pre-upgrade backup", " ".join(v.notes))

    def test_backup_only_for_minor(self):
        cases = {"2026.12.0": "minor", "2026.11.1": "patch", "2026.11.0": "digest"}
        for tag, kind in cases.items():
            with self.subTest(kind=kind):
                v = evaluate(BASE.replace(f"home-assistant:2026.11.0@{D1}", f"home-assistant:{tag}@{D2}"))
                self.assertTrue(v.eligible, v.reasons)
                b = policy.pre_upgrade_backups(v.changes, ALLOW, "20261107T120000Z")
                if kind == "minor":
                    self.assertEqual(b, {"homeassistant": {
                        "type": "homeassistant",
                        "dir": "/mpool/backups/automerge-pre-upgrade/homeassistant/"
                               "20261107T120000Z-2026.11.0-to-2026.12.0"}})
                else:
                    self.assertEqual(b, {})

    def test_other_services_take_no_backup(self):
        v = evaluate(BASE.replace(f"grafana:13.3.0-ubuntu@{D1}", f"grafana:13.4.0-ubuntu@{D2}"))
        self.assertTrue(v.eligible, v.reasons)
        self.assertEqual(policy.pre_upgrade_backups(v.changes, ALLOW, "x"), {})

    def test_major_still_refused(self):
        v = evaluate(BASE.replace(f"influxdb:2.9.1@{D1}", f"influxdb:3.0.0@{D2}"))
        self.assertFalse(v.eligible)
        self.assertTrue(v.major)

    def test_malformed_config_fails_closed(self):
        bad = [
            ({"homeassistant": {"pre_upgrade_backup": "postgres"}}, ALLOW["pre_upgrade_backups"]),
            ({"homeassistant": {"pre_upgrade_backup": True}}, ALLOW["pre_upgrade_backups"]),
            (ALLOW["services"], None),
            (ALLOW["services"], {"dir": "/persistent_data/backups"}),
            (ALLOW["services"], {"dir": "relative/dir"}),
            (ALLOW["services"], {"dir": "/"}),
            (ALLOW["services"], {"dir": "/mpool/x", "keep": 0}),
            (ALLOW["services"], {"dir": "/mpool/x", "rate_mb": "fast"}),
            (ALLOW["services"], {"dir": "/mpool/x", "timeout": True}),
        ]
        for services, section in bad:
            with self.subTest(services=services, section=section):
                allow = copy.deepcopy(ALLOW)
                allow["services"] = copy.deepcopy(services)
                if section is None:
                    allow.pop("pre_upgrade_backups")
                else:
                    allow["pre_upgrade_backups"] = section
                with self.assertRaises(PolicyError):
                    policy.pre_upgrade_config(allow)
                v = evaluate(BASE.replace(f"grafana:13.3.0-ubuntu@{D1}", f"grafana:13.3.0-ubuntu@{D2}"), allow)
                self.assertFalse(v.eligible)

    def test_no_section_needed_without_services(self):
        self.assertEqual(policy.pre_upgrade_config({"services": {}})["services"], {})


class RestoreLinesTests(unittest.TestCase):
    backups = [{"service": "homeassistant", "backup_dir": "/mpool/b/homeassistant/x"}]

    def test_states(self):
        f = automerge.restore_lines
        self.assertEqual(f([], {}), [])
        self.assertIn("never started", f(self.backups, {"upgrade_started": False})[0])
        self.assertIn("did not run", f(self.backups, {"upgrade_started": True})[0])
        ok = {"upgrade_started": True, "restored": [{"service": "homeassistant", "ok": True}]}
        self.assertIn("restored before", f(self.backups, ok)[0])
        bad = {"upgrade_started": True, "restored": [{"service": "homeassistant", "ok": False}]}
        self.assertIn("RESTORE FAILED", f(self.backups, bad)[0])


@unittest.skipIf(yaml is None, "PyYAML not installed")
class PlaybookOrderTests(unittest.TestCase):
    """The rollback logic of ansible/service-update.yml, checked statically:
    back up before the new image can start, restore before the previous image
    starts, and restore only once the new image may have run."""

    @classmethod
    def setUpClass(cls):
        play = yaml.safe_load((REPO / "ansible" / "service-update.yml").read_text())[0]
        block = next(t for t in play["tasks"] if "block" in t)
        cls.block = block["block"]
        cls.rescue = block["rescue"]
        cls.play = play

    @staticmethod
    def index(tasks, name):
        return next(i for i, t in enumerate(tasks) if t.get("name") == name)

    def test_backup_before_recreate(self):
        b = self.index(self.block, "Pre-upgrade backup")
        mark = self.index(self.block, "Mark the upgrade as started")
        up = self.index(self.block, "Pull and recreate only the target services")
        self.assertLess(b, mark)
        self.assertEqual(mark + 1, up)
        task = self.block[b]
        self.assertIn(" backup", task["ansible.builtin.script"]["cmd"])
        self.assertEqual(task["loop"], "{{ automerge_backups }}")
        self.assertNotIn("ignore_errors", task)       # a failed backup fails the deploy
        self.assertIn("automerge_upgrade_started: true", yaml.safe_dump(self.block[mark]))

    def test_prune_does_not_fail_the_deploy(self):
        t = self.block[self.index(self.block, "Prune old pre-upgrade backups")]
        self.assertTrue(t["ignore_errors"])
        self.assertLess(self.index(self.block, "Pre-upgrade backup"), self.index(self.block, "Prune old pre-upgrade backups"))

    def test_restore_before_previous_image_starts(self):
        r = self.index(self.rescue, "ROLLBACK - restore pre-upgrade backups")
        render = self.index(self.rescue, "ROLLBACK - render pre-merge compose file")
        up = self.index(self.rescue, "ROLLBACK - recreate target services on their previous image")
        self.assertTrue(render < r < up)
        task = self.rescue[r]
        self.assertIn(" restore", task["ansible.builtin.script"]["cmd"])
        self.assertEqual(task["when"], "automerge_upgrade_started | default(false)")
        self.assertTrue(task["ignore_errors"])        # the previous image starts regardless

    def test_backups_selected_from_plan(self):
        sel = self.play["vars"]["automerge_backups"]
        self.assertIn("selectattr('pre_upgrade_backup')", sel)

    def test_result_reports_restores(self):
        w = self.rescue[self.index(self.rescue, "Write failure result")]
        content = w["ansible.builtin.copy"]["content"]
        for key in ("'upgrade_started'", "'restored'", "automerge_rollback_ok"):
            self.assertIn(key, content)
        outcome = yaml.safe_dump(self.rescue[self.index(self.rescue, "ROLLBACK - outcome")])
        self.assertIn("rejectattr('ok')", outcome)   # a failed restore is not a healthy rollback


@unittest.skipIf(yaml is None, "PyYAML not installed")
class RealPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.allow = yaml.safe_load((REPO / "ci" / "automerge-policy.yml").read_text())
        cls.allow["services"] = {k: (v or {}) for k, v in cls.allow["services"].items()}

    def test_config(self):
        cfg = policy.pre_upgrade_config(self.allow)
        self.assertEqual(cfg["services"], {"homeassistant": "homeassistant", "influxdb": "influxdb"})
        self.assertTrue(cfg["dir"].startswith("/mpool/"))
        self.assertEqual(cfg["keep"], 2)
        self.assertGreaterEqual(cfg["timeout"], 1200)

    def test_minors_allowed(self):
        for svc, image in (("homeassistant", "homeassistant/home-assistant"), ("influxdb", "influxdb")):
            self.assertIn("minor", policy.allowed_kinds(svc, image, self.allow))

    def test_backed_up_services_run_on_the_router_only(self):
        # the backups go to the router's ZFS pool
        for svc in policy.pre_upgrade_config(self.allow)["services"]:
            users = []
            for path in policy.compose_files(self.allow):
                _, images = policy.compose_line_services((REPO / path).read_text())
                if svc in images.values():
                    users.append(path)
            self.assertEqual(users, [policy.COMPOSE_PATH], svc)


# ---------------------------------------------------------------------------
# The script, against scratch directories and a stub docker
# ---------------------------------------------------------------------------

STUB_DOCKER = r"""#!/bin/bash
# Stub docker for test_pre_upgrade.py. State lives in $STUB_DIR.
echo "$*" >> "$STUB_DIR/calls"
case "$1" in
  ps) cat "$STUB_DIR/cid" ;;
  pull) exit 0 ;;
  stop) echo stopped > "$STUB_DIR/state" ;;
  run) echo "$STUB_IMAGE_SCHEMA" ;;
  inspect)
    tmpl="$3"
    case "$tmpl" in
      *Mounts*) dest=$(sed -E 's/.*Destination "([^"]+)".*/\1/' <<<"$tmpl")
                cat "$STUB_DIR/mount$(tr / _ <<<"$dest")" ;;
      *State.Running*) [ "$(cat "$STUB_DIR/state")" = running ] && echo true || echo false ;;
      *State.Restarting*) echo false ;;
      *.Name*) echo /stub ;;
      *Config.Image*) echo "old/image:1.0@sha256:aaaa" ;;
      *Config.Env*) cat "$STUB_DIR/env" 2>/dev/null; true ;;
      *) echo "stub: unknown template $tmpl" >&2; exit 1 ;;
    esac ;;
  *) echo "stub: unknown command $1" >&2; exit 1 ;;
esac
"""

TOOLS = ("bash", "rsync", "pv", "sqlite3", "sha256sum", "numfmt", "findmnt")


def make_ha_db(path: Path, schema: int, rows: int = 200) -> None:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=wal")
    db.execute("CREATE TABLE schema_changes (change_id INTEGER PRIMARY KEY, schema_version INTEGER)")
    db.execute("INSERT INTO schema_changes (schema_version) VALUES (?)", (schema - 1,))
    db.execute("INSERT INTO schema_changes (schema_version) VALUES (?)", (schema,))
    db.execute("CREATE TABLE states (id INTEGER PRIMARY KEY, v TEXT)")
    db.executemany("INSERT INTO states (v) VALUES (?)", [(f"state {i}",) for i in range(rows)])
    db.commit()
    db.close()


def rows(path: Path) -> int:
    db = sqlite3.connect(f"file:{path}?immutable=1", uri=True)   # no -wal/-shm sidecars
    try:
        return db.execute("SELECT count(*) FROM states").fetchone()[0]
    finally:
        db.close()


@unittest.skipUnless(all(shutil.which(t) for t in TOOLS), "needs " + ", ".join(TOOLS))
class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(STUB_DOCKER)
        (bindir / "docker").chmod(0o755)
        (self.stub / "cid").write_text("c0ffee\n")
        (self.stub / "state").write_text("running\n")
        self.root = self.tmp / "backups"
        self.env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "STUB_DIR": str(self.stub),
                    "PRE_UPGRADE_REQUIRE_ZFS": "0", "STUB_IMAGE_SCHEMA": "53"}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def mount(self, dest: str, src: Path) -> None:
        (self.stub / ("mount" + dest.replace("/", "_"))).write_text(f"{src}\n")

    def run_script(self, *args, ok=True):
        r = subprocess.run(["bash", str(SCRIPT), *args], env=self.env, capture_output=True, text=True)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def calls(self):
        return (self.stub / "calls").read_text()

    # --- Home Assistant -------------------------------------------------------

    def ha_config(self, schema=53) -> Path:
        cfg = self.tmp / "config"
        (cfg / ".storage").mkdir(parents=True)
        (cfg / ".storage" / "core.config_entries").write_text('{"version": 1, "minor_version": 5}')
        (cfg / ".storage" / "core.entity_registry").write_text('{"version": 1, "minor_version": 18}')
        (cfg / "configuration.yaml").write_text("recorder:\n  purge_keep_days: 1000\n")
        (cfg / "custom_components" / "x").mkdir(parents=True)
        (cfg / "custom_components" / "x" / "manifest.json").write_text("{}")
        (cfg / "home-assistant.log").write_text("log\n")
        (cfg / ".cache").mkdir()
        (cfg / ".cache" / "blob").write_text("cache")
        make_ha_db(cfg / "home-assistant_v2.db", schema)
        self.mount("/config", cfg)
        return cfg

    def ha(self, cmd, d, *extra, ok=True):
        return self.run_script(cmd, "--type", "homeassistant", "--service", "homeassistant",
                               "--dir", str(d), "--rate", "50", *extra, ok=ok)

    def test_ha_backup_same_schema_skips_database(self):
        self.ha_config()
        d = self.root / "homeassistant" / "20261107T120000Z-2026.11.0-to-2026.12.0"
        out = self.ha("backup", d, "--new-image", "ha:2026.12.0").stdout
        self.assertIn("schema 53 is unchanged", out)
        self.assertTrue((d / "COMPLETE").exists())
        self.assertFalse((d / "home-assistant_v2.db").exists())
        self.assertTrue((d / "config" / ".storage" / "core.config_entries").exists())
        self.assertTrue((d / "config" / "custom_components" / "x" / "manifest.json").exists())
        for excluded in ("home-assistant_v2.db", "home-assistant.log", ".cache"):
            self.assertFalse((d / "config" / excluded).exists(), excluded)
        self.assertIn("db=skipped schema=53", (d / "INFO").read_text())
        calls = self.calls()
        self.assertLess(calls.index("pull"), calls.index("stop"))    # pulled before the stop
        self.assertEqual(oct((d.stat().st_mode) & 0o777), "0o700")

    def test_ha_backup_and_restore_with_database(self):
        cfg = self.ha_config()
        self.env["STUB_IMAGE_SCHEMA"] = "54"
        d = self.root / "homeassistant" / "20261107T120000Z-2026.11.0-to-2026.12.0"
        out = self.ha("backup", d, "--new-image", "ha:2026.12.0").stdout
        self.assertIn("recorder schema 53 -> 54", out)
        self.assertEqual(rows(d / "home-assistant_v2.db"), 200)
        self.assertIn("db=copied schema=53 new_schema=54", (d / "INFO").read_text())

        # the "new version" migrates .storage and the database, adds a file
        (self.stub / "state").write_text("running\n")
        (cfg / ".storage" / "core.config_entries").write_text('{"version": 2}')
        (cfg / ".storage" / "new_in_2026_12").write_text("{}")
        (cfg / "home-assistant_v2.db").unlink()
        make_ha_db(cfg / "home-assistant_v2.db", 54, rows=10)
        (cfg / "home-assistant_v2.db-wal").write_bytes(b"stale wal from the new version")
        (cfg / "home-assistant.log").write_text("new log\n")

        self.ha("restore", d)
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        cmp = filecmp.dircmp(d / "config" / ".storage", cfg / ".storage")
        self.assertEqual((cmp.left_only, cmp.right_only, cmp.diff_files), ([], [], []))
        self.assertEqual((cfg / ".storage" / "core.config_entries").read_text(), '{"version": 1, "minor_version": 5}')
        self.assertEqual(rows(cfg / "home-assistant_v2.db"), 200)
        self.assertFalse((cfg / "home-assistant_v2.db-wal").exists())
        self.assertFalse((cfg / "home-assistant_v2.db.restore").exists())
        self.assertEqual((cfg / "home-assistant.log").read_text(), "new log\n")   # excluded: left alone
        self.assertTrue((cfg / ".cache" / "blob").exists())

    def test_ha_backup_unknown_new_schema_copies_database(self):
        self.ha_config()
        self.env["STUB_IMAGE_SCHEMA"] = ""
        d = self.root / "homeassistant" / "20261107T120000Z-a-to-b"
        self.ha("backup", d, "--new-image", "ha:new")
        self.assertTrue((d / "home-assistant_v2.db").exists())

    def test_ha_external_recorder_refused(self):
        cfg = self.ha_config()
        (cfg / "configuration.yaml").write_text("recorder:\n  db_url: mysql://x\n")
        d = self.root / "homeassistant" / "20261107T120000Z-a-to-b"
        out = self.ha("backup", d, "--new-image", "ha:new", ok=False).stdout
        self.assertIn("db_url", out)
        self.assertFalse((d / "COMPLETE").exists())

    def test_restore_refuses_incomplete_or_foreign_backup(self):
        cfg = self.ha_config()
        d = self.root / "homeassistant" / "20261107T120000Z-a-to-b"
        self.ha("backup", d, "--new-image", "ha:new")
        (d / "COMPLETE").unlink()
        before = (cfg / ".storage" / "core.config_entries").read_text()
        self.ha("restore", d, ok=False)
        (d / "COMPLETE").touch()
        self.run_script("restore", "--type", "influxdb", "--service", "homeassistant", "--dir", str(d), ok=False)
        self.assertEqual((cfg / ".storage" / "core.config_entries").read_text(), before)

    def test_backup_refuses_existing_dir(self):
        self.ha_config()
        d = self.root / "homeassistant" / "20261107T120000Z-a-to-b"
        d.mkdir(parents=True)
        self.ha("backup", d, "--new-image", "ha:new", ok=False)
        self.assertNotIn("stop", self.calls() if (self.stub / "calls").exists() else "")

    def test_zfs_is_required(self):
        self.ha_config()
        self.env["PRE_UPGRADE_REQUIRE_ZFS"] = "1"
        d = self.root / "homeassistant" / "20261107T120000Z-a-to-b"
        r = self.ha("backup", d, "--new-image", "ha:new", ok=findmnt_fs(self.tmp) == "zfs")
        if findmnt_fs(self.tmp) != "zfs":
            self.assertIn("not ZFS", r.stdout)

    # --- InfluxDB -------------------------------------------------------------

    def influx_data(self) -> Path:
        data = self.tmp / "influxdb2"
        (data / "engine" / "data").mkdir(parents=True)
        (data / "influxd.bolt").write_bytes(b"bolt v1" * 100)
        (data / "influxd.sqlite").write_bytes(b"sqlite v1" * 100)
        (data / "engine" / "data" / "000001.tsm").write_bytes(b"tsm")
        self.mount("/var/lib/influxdb2", data)
        return data

    def influx(self, cmd, d, *extra, ok=True):
        return self.run_script(cmd, "--type", "influxdb", "--service", "influxdb", "--dir", str(d), *extra, ok=ok)

    def test_influx_backup_and_restore(self):
        data = self.influx_data()
        d = self.root / "influxdb" / "20261107T120000Z-2.9.1-to-2.10.0"
        self.influx("backup", d, "--new-image", "influxdb:2.10.0")
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["COMPLETE", "INFO", "SHA256SUMS", "influxd.bolt", "influxd.sqlite"])
        # the new version migrates the metadata and leaves a WAL behind
        (self.stub / "state").write_text("running\n")
        (data / "influxd.bolt").write_bytes(b"bolt v2")
        (data / "influxd.sqlite").write_bytes(b"sqlite v2")
        (data / "influxd.sqlite-wal").write_bytes(b"wal")
        (data / "engine" / "data" / "000002.tsm").write_bytes(b"new data")
        self.influx("restore", d)
        self.assertEqual((data / "influxd.bolt").read_bytes(), b"bolt v1" * 100)
        self.assertEqual((data / "influxd.sqlite").read_bytes(), b"sqlite v1" * 100)
        self.assertFalse((data / "influxd.sqlite-wal").exists())
        self.assertTrue((data / "engine" / "data" / "000002.tsm").exists())   # data is not touched

    def test_influx_restore_refuses_bad_checksum(self):
        data = self.influx_data()
        d = self.root / "influxdb" / "20261107T120000Z-a-to-b"
        self.influx("backup", d, "--new-image", "influxdb:new")
        (d / "influxd.bolt").write_bytes(b"corrupt")
        (data / "influxd.bolt").write_bytes(b"bolt v2")
        self.influx("restore", d, ok=False)
        self.assertEqual((data / "influxd.bolt").read_bytes(), b"bolt v2")

    def test_influx_custom_paths_refused(self):
        self.influx_data()
        (self.stub / "env").write_text("INFLUXD_BOLT_PATH=/elsewhere/influxd.bolt\n")
        d = self.root / "influxdb" / "20261107T120000Z-a-to-b"
        self.assertIn("default paths", self.influx("backup", d, "--new-image", "x", ok=False).stdout)

    # --- prune ----------------------------------------------------------------

    def test_prune_keeps_newest(self):
        svc = self.root / "influxdb"
        names = ["20261001T010000Z-2.7.0-to-2.8.0", "20261104T010000Z-2.8.0-to-2.9.0",
                 "20261107T010000Z-2.9.0-to-2.9.1", "20260901T010000Z-2.6.0-to-2.7.0"]
        for n in names:
            (svc / n).mkdir(parents=True)
        (svc / "unrelated").mkdir()
        self.run_script("prune", "--dir", str(svc), "--keep", "2")
        self.assertEqual(sorted(p.name for p in svc.iterdir()),
                         ["20261104T010000Z-2.8.0-to-2.9.0", "20261107T010000Z-2.9.0-to-2.9.1", "unrelated"])


def findmnt_fs(path: Path) -> str:
    r = subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)], capture_output=True, text=True)
    return r.stdout.strip()


if __name__ == "__main__":
    unittest.main()
