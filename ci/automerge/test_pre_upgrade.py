"""Tests for pre-upgrade backups (docs/AUTOMERGE.md, "Pre-upgrade backups"):
the policy option, the deploy plan, the playbook's backup/rollback ordering,
the rollback issue text, and ci/automerge/pre-upgrade-backup.sh itself (run
against scratch directories with a stub `docker`).

Run: python3 -m unittest discover -s ci/automerge -v
"""

import copy
import filecmp
import gzip
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
  nextcloud:
    image: nextcloud:35.0.2-apache@{D1}
"""

ALLOW = {
    "authors": [RENOVATE],
    "target_branch": "main",
    "compose_files": [policy.COMPOSE_PATH],
    "services": {
        "homeassistant": {"pre_upgrade_backup": "homeassistant"},
        "influxdb": {"pre_upgrade_backup": "influxdb"},
        "nextcloud": {"updates": ["digest", "patch"], "pre_upgrade_backup": "nextcloud"},
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

    def test_nextcloud_backup_for_patch(self):
        # !1249: nextcloud 35.0.2-apache -> 35.0.3-apache
        cases = {"35.0.3-apache": "patch", "35.0.2-apache": "digest"}
        for tag, kind in cases.items():
            with self.subTest(kind=kind):
                v = evaluate(BASE.replace(f"nextcloud:35.0.2-apache@{D1}", f"nextcloud:{tag}@{D2}"))
                self.assertTrue(v.eligible, v.reasons)
                self.assertEqual(v.changes[0].kind, kind)
                b = policy.pre_upgrade_backups(v.changes, ALLOW, "20261115T120000Z")
                if kind == "patch":
                    self.assertEqual(b, {"nextcloud": {
                        "type": "nextcloud",
                        "dir": "/mpool/backups/automerge-pre-upgrade/nextcloud/"
                               "20261115T120000Z-35.0.2-apache-to-35.0.3-apache"}})
                    self.assertIn("nextcloud: nextcloud pre-upgrade backup", " ".join(v.notes))
                else:
                    self.assertEqual(b, {})

    def test_nextcloud_minor_and_major_refused(self):
        for tag in ("35.1.0-apache", "36.0.0-apache"):
            with self.subTest(tag=tag):
                v = evaluate(BASE.replace(f"nextcloud:35.0.2-apache@{D1}", f"nextcloud:{tag}@{D2}"))
                self.assertFalse(v.eligible)

    def test_backup_kinds_cover_every_type(self):
        self.assertEqual(set(policy.PRE_UPGRADE_TYPES), set(policy.PRE_UPGRADE_KINDS_BY_TYPE))
        for t, kinds in policy.PRE_UPGRADE_KINDS_BY_TYPE.items():
            self.assertNotIn("digest", kinds, t)    # a digest refresh never migrates
            self.assertTrue(set(kinds) <= {"patch", "minor"}, t)

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

    def test_nextcloud_version_check_before_backup(self):
        c = self.index(self.block, "Nextcloud - refuse an image whose packaged version is not the expected one")
        self.assertLess(c, self.index(self.block, "Pre-upgrade backup"))
        task = self.block[c]
        self.assertNotIn("ignore_errors", task)
        cmd = task["ansible.builtin.shell"]["cmd"]
        self.assertIn("pre_upgrade_backup", cmd)     # a version change only with a backup
        self.assertIn('[ "$want" = "$have" ]', cmd)   # otherwise the version must not change
        self.assertIn("cut -d. -f1,2", cmd)          # and never across a minor

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
        self.assertEqual(cfg["services"], {"homeassistant": "homeassistant", "influxdb": "influxdb",
                                           "nextcloud": "nextcloud", "bitwarden": "vaultwarden"})
        self.assertTrue(cfg["dir"].startswith("/mpool/"))
        self.assertEqual(cfg["keep"], 2)
        self.assertGreaterEqual(cfg["timeout"], 1200)

    def test_minors_allowed(self):
        for svc, image in (("homeassistant", "homeassistant/home-assistant"), ("influxdb", "influxdb")):
            self.assertIn("minor", policy.allowed_kinds(svc, image, self.allow))

    def test_nextcloud_patches_only(self):
        kinds = policy.allowed_kinds("nextcloud", "nextcloud", self.allow)
        self.assertEqual(set(kinds), {"digest", "patch"})
        self.assertEqual(self.allow["services"]["nextcloud"]["pre_upgrade_backup"], "nextcloud")

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
  # A container with its own state_<cid> file (the Nextcloud database) is
  # tracked apart from the service's ($STUB_DIR/state).
  ps) svc=$(sed -nE 's/.*com[.]docker[.]compose[.]service=([^ ]+).*/\1/p' <<<"$*")
      if [ -f "$STUB_DIR/cid_$svc" ]; then cat "$STUB_DIR/cid_$svc"; else cat "$STUB_DIR/cid"; fi ;;
  pull) exit 0 ;;
  stop) cid="${*: -1}"
        if [ -f "$STUB_DIR/state_$cid" ]; then echo stopped > "$STUB_DIR/state_$cid"
        else echo stopped > "$STUB_DIR/state"; fi ;;
  run) echo "$STUB_IMAGE_SCHEMA" ;;
  # exec -i: a database restore (stdin is saved); exec: a dump (prints $STUB_DIR/dump)
  exec) if [ "$2" = -i ]; then cat > "$STUB_DIR/loaded"; else cat "$STUB_DIR/dump"; fi ;;
  inspect)
    tmpl="$3"
    case "$tmpl" in
      *Mounts*) dest=$(sed -E 's/.*Destination "([^"]+)".*/\1/' <<<"$tmpl")
                cat "$STUB_DIR/mount$(tr / _ <<<"$dest")" ;;
      *State.Running*) f="$STUB_DIR/state"; [ ! -f "$STUB_DIR/state_$4" ] || f="$STUB_DIR/state_$4"
                       [ "$(cat "$f")" = running ] && echo true || echo false ;;
      *State.Restarting*) echo false ;;
      *.Name*) echo /stub ;;
      *Config.Image*) echo "old/image:1.0@sha256:aaaa" ;;
      *Config.Env*) cat "$STUB_DIR/env" 2>/dev/null; true ;;
      *) echo "stub: unknown template $tmpl" >&2; exit 1 ;;
    esac ;;
  *) echo "stub: unknown command $1" >&2; exit 1 ;;
esac
"""

TOOLS = ("bash", "rsync", "pv", "sqlite3", "sha256sum", "numfmt", "findmnt", "gzip")


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

    # --- Vaultwarden -----------------------------------------------------------

    def vw_data(self) -> Path:
        data = self.tmp / "vwdata"
        (data / "attachments" / "a1").mkdir(parents=True)
        (data / "icon_cache").mkdir()
        (data / "db.sqlite3").write_bytes(b"schema v1" * 100)
        (data / "db.sqlite3-wal").write_bytes(b"wal v1")
        (data / "rsa_key.pem").write_text("key")
        (data / "attachments" / "a1" / "f").write_bytes(b"attachment")
        (data / "icon_cache" / "x.png").write_bytes(b"icon")
        self.mount("/data", data)
        return data

    def vw(self, cmd, d, *extra, ok=True):
        return self.run_script(cmd, "--type", "vaultwarden", "--service", "bitwarden", "--dir", str(d),
                               *extra, ok=ok)

    def test_vw_backup_and_restore(self):
        data = self.vw_data()
        d = self.root / "bitwarden" / "20261124T010000Z-1.37.5-alpine-to-1.37.6-alpine"
        self.vw("backup", d, "--new-image", "vaultwarden/server:1.37.6-alpine")
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        calls = self.calls()
        self.assertLess(calls.index("pull"), calls.index("stop"))
        self.assertTrue((d / "data" / "db.sqlite3").exists())
        self.assertTrue((d / "data" / "attachments" / "a1" / "f").exists())
        self.assertFalse((d / "data" / "icon_cache").exists())
        self.assertIn("files=4", (d / "INFO").read_text())
        # the new version migrates the database, rewrites the WAL and adds a file
        (self.stub / "state").write_text("running\n")
        (data / "db.sqlite3").write_bytes(b"schema v2")
        (data / "db.sqlite3-wal").write_bytes(b"wal v2")
        (data / "db.sqlite3-shm").write_bytes(b"shm v2")
        (data / "sends").mkdir()
        (data / "sends" / "s").write_bytes(b"new")
        self.vw("restore", d)
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        self.assertEqual((data / "db.sqlite3").read_bytes(), b"schema v1" * 100)
        self.assertEqual((data / "db.sqlite3-wal").read_bytes(), b"wal v1")
        self.assertFalse((data / "db.sqlite3-shm").exists())
        self.assertFalse((data / "sends").exists())
        self.assertTrue((data / "icon_cache" / "x.png").exists())      # excluded: left alone

    def test_vw_restore_refuses_bad_checksum(self):
        data = self.vw_data()
        d = self.root / "bitwarden" / "20261124T010000Z-a-to-b"
        self.vw("backup", d, "--new-image", "vaultwarden/server:new")
        (d / "data" / "db.sqlite3").write_bytes(b"corrupt")
        (data / "db.sqlite3").write_bytes(b"schema v2")
        self.vw("restore", d, ok=False)
        self.assertEqual((data / "db.sqlite3").read_bytes(), b"schema v2")

    def test_vw_external_database_refused(self):
        self.vw_data()
        (self.stub / "env").write_text("TZ=UTC\nDATABASE_URL=postgresql://x\n")
        d = self.root / "bitwarden" / "20261124T010000Z-a-to-b"
        out = self.vw("backup", d, "--new-image", "vaultwarden/server:new", ok=False).stdout
        self.assertIn("DATABASE_URL", out)
        self.assertFalse((d / "COMPLETE").exists())

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

    # --- Nextcloud ------------------------------------------------------------

    DUMP = ("-- MariaDB dump\nDROP DATABASE IF EXISTS `nextcloud`;\nCREATE DATABASE `nextcloud`;\n"
            "INSERT INTO oc_appconfig VALUES ('core','vendor','nextcloud');\n"
            "-- Dump completed on 2026-11-15  3:00:00\n")

    def nc_setup(self, version="35,0,2,1", instance="oc1a2b3c", maintenance=False):
        html = self.tmp / "html"
        (html / "config").mkdir(parents=True)
        (html / "apps" / "files").mkdir(parents=True)
        (html / "custom_apps" / "calendar").mkdir(parents=True)
        (html / "version.php").write_text(f"<?php\n$OC_Version = array({version});\n$OC_VersionString = 'x';\n")
        (html / "config" / "config.php").write_text(
            "<?php\n$CONFIG = array (\n"
            f"  'instanceid' => '{instance}',\n  'version' => '{version.replace(',', '.')}',\n"
            + ("  'maintenance' => true,\n" if maintenance else "  'maintenance' => false,\n") + ");\n")
        (html / "config" / "custom.config.php").write_text("")       # the bind mount's placeholder
        (html / "apps" / "files" / "appinfo.xml").write_text("<version>2.0</version>")
        (html / "custom_apps" / "calendar" / "info.xml").write_text("<version>6.0</version>")
        data = self.tmp / "data"
        app = data / f"appdata_{instance}"
        (app / "js" / "core").mkdir(parents=True)
        (app / "preview" / "1").mkdir(parents=True)
        (app / "js" / "core" / "merged.js").write_text("old js")
        (app / "preview" / "1" / "256.png").write_bytes(b"png")
        (data / "alice" / "files").mkdir(parents=True)
        (data / "alice" / "files" / "doc.txt").write_text("user file")
        self.mount("/var/www/html", html)
        self.mount("/data", data)
        (self.stub / "cid_nextcloud_db").write_text("dbc1\n")
        (self.stub / "state_dbc1").write_text("running\n")
        (self.stub / "dump").write_text(self.DUMP)
        return html, app

    def nc(self, cmd, d, *extra, ok=True):
        return self.run_script(cmd, "--type", "nextcloud", "--service", "nextcloud", "--dir", str(d),
                               "--rate", "50", *extra, ok=ok)

    def test_nc_backup_and_restore(self):
        html, app = self.nc_setup()
        d = self.root / "nextcloud" / "20261115T120000Z-35.0.2-apache-to-35.0.3-apache"
        self.nc("backup", d, "--new-image", "nextcloud:35.0.3-apache")
        self.assertTrue((d / "COMPLETE").exists())
        self.assertEqual(gzip.decompress((d / "nextcloud.sql.gz").read_bytes()).decode(), self.DUMP)
        self.assertEqual((d / "html" / "version.php").read_text(), (html / "version.php").read_text())
        self.assertTrue((d / "html" / "custom_apps" / "calendar" / "info.xml").exists())
        self.assertFalse((d / "html" / "config" / "custom.config.php").exists())
        self.assertTrue((d / "appdata" / "js" / "core" / "merged.js").exists())
        self.assertFalse((d / "appdata" / "preview").exists())          # previews are not copied
        self.assertFalse(any("doc.txt" in str(p) for p in d.rglob("*")))  # nor user files
        info = (d / "INFO").read_text()
        self.assertIn("version=35.0.2.1", info)
        self.assertIn("appdata=appdata_oc1a2b3c", info)
        calls = self.calls()
        self.assertLess(calls.index("pull"), calls.index("stop"))
        self.assertLess(calls.index("stop"), calls.index("exec"))     # dumped with Nextcloud stopped
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        self.assertEqual((self.stub / "state_dbc1").read_text().strip(), "running")

        # the new version: new code and config.php version, a new app, the
        # app data cache rewritten; meanwhile a preview and a user file appear
        (self.stub / "state").write_text("running\n")
        (html / "version.php").write_text("<?php\n$OC_Version = array(35,0,3,1);\n")
        (html / "config" / "config.php").write_text(
            (html / "config" / "config.php").read_text().replace("35.0.2.1", "35.0.3.1"))
        (html / "apps" / "new_in_35_0_3").mkdir()
        (html / "config" / "custom.config.php").write_text("ansible-managed")
        (app / "js" / "core" / "merged.js").write_text("new js")
        (app / "js" / "core" / "extra.js").write_text("new")
        (app / "preview" / "1" / "512.png").write_bytes(b"png")
        (app.parent / "alice" / "files" / "new.txt").write_text("uploaded meanwhile")

        self.nc("restore", d)
        self.assertEqual((self.stub / "state").read_text().strip(), "stopped")
        self.assertEqual((self.stub / "loaded").read_text(), self.DUMP)
        self.assertIn("array(35,0,2,1)", (html / "version.php").read_text())
        self.assertIn("'version' => '35.0.2.1'", (html / "config" / "config.php").read_text())
        self.assertFalse((html / "apps" / "new_in_35_0_3").exists())
        self.assertEqual((html / "config" / "custom.config.php").read_text(), "ansible-managed")
        self.assertEqual((app / "js" / "core" / "merged.js").read_text(), "old js")
        self.assertFalse((app / "js" / "core" / "extra.js").exists())
        self.assertTrue((app / "preview" / "1" / "512.png").exists())     # previews left alone
        self.assertTrue((app.parent / "alice" / "files" / "new.txt").exists())

    def test_nc_truncated_dump_refused(self):
        self.nc_setup()
        (self.stub / "dump").write_text("-- MariaDB dump\nCREATE TABLE x;\n")
        d = self.root / "nextcloud" / "20261115T120000Z-a-to-b"
        self.assertIn("truncated", self.nc("backup", d, "--new-image", "nextcloud:new", ok=False).stdout)
        self.assertFalse((d / "COMPLETE").exists())

    def test_nc_maintenance_mode_refused_before_stop(self):
        self.nc_setup(maintenance=True)
        d = self.root / "nextcloud" / "20261115T120000Z-a-to-b"
        self.assertIn("maintenance", self.nc("backup", d, "--new-image", "nextcloud:new", ok=False).stdout)
        self.assertEqual((self.stub / "state").read_text().strip(), "running")

    def test_nc_database_not_running_refused(self):
        self.nc_setup()
        (self.stub / "state_dbc1").write_text("stopped\n")
        d = self.root / "nextcloud" / "20261115T120000Z-a-to-b"
        self.assertIn("not running", self.nc("backup", d, "--new-image", "nextcloud:new", ok=False).stdout)
        self.assertEqual((self.stub / "state").read_text().strip(), "running")

    def test_nc_restore_refuses_bad_backup(self):
        html, app = self.nc_setup()
        d = self.root / "nextcloud" / "20261115T120000Z-a-to-b"
        self.nc("backup", d, "--new-image", "nextcloud:new")
        (self.stub / "state").write_text("running\n")
        (html / "version.php").write_text("<?php\n$OC_Version = array(35,0,3,1);\n")
        good = (d / "nextcloud.sql.gz").read_bytes()
        (d / "nextcloud.sql.gz").write_bytes(gzip.compress(b"DROP DATABASE `nextcloud`;\n"))
        self.assertIn("checksums", self.nc("restore", d, ok=False).stdout)
        (d / "nextcloud.sql.gz").write_bytes(good)
        info = (d / "INFO").read_text()
        (d / "INFO").write_text(info.replace("appdata=appdata_oc1a2b3c", "appdata=appdata_other"))
        self.assertIn("another Nextcloud instance", self.nc("restore", d, ok=False).stdout)
        self.assertFalse((self.stub / "loaded").exists())
        self.assertIn("array(35,0,3,1)", (html / "version.php").read_text())
        self.assertEqual((self.stub / "state").read_text().strip(), "running")

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
