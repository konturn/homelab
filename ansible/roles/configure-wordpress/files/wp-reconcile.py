#!/usr/bin/env python3
"""Reconcile the WordPress install against its declared state.

Run on the router by ansible/roles/configure-wordpress. Input is a base64 JSON
config (declared state from docker/wordpress/site.yml plus host settings).

Order: core -> DB schema -> themes -> plugins -> ownership/modes -> checksums.
A DB dump is taken before the first core/theme/plugin change. With --check,
nothing is modified and the planned actions are reported.

Last stdout line is JSON: {"changed": bool, "actions": [...], "warnings": [...]}.
"""
import argparse
import base64
import datetime
import glob
import gzip
import json
import os
import re
import stat
import subprocess
import sys

CONTAINER_ROOT = "/var/www/html"


class Reconciler:
    def __init__(self, cfg, check):
        self.cfg = cfg
        self.check = check
        self.actions = []
        self.warnings = []
        self.backup_taken = False

    def log(self, msg):
        print(msg, flush=True)

    def wp(self, *args, ok_rc=(0,)):
        cmd = [
            "docker", "exec", "-u", f"{self.cfg['uid']}:{self.cfg['gid']}",
            "-e", "HOME=/tmp", self.cfg["container"],
            "php", self.cfg["wp_cli"], f"--path={CONTAINER_ROOT}", *args,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if res.returncode not in ok_rc:
            raise RuntimeError(
                f"wp {' '.join(args)} failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}")
        return res

    def wp_json(self, *args):
        return json.loads(self.wp(*args, "--format=json").stdout or "[]")

    # -- changes ----------------------------------------------------------

    def act(self, desc, *wp_args):
        """Record a change and apply it (unless in check mode)."""
        self.actions.append(desc)
        self.log(("would: " if self.check else "apply: ") + desc)
        if self.check or not wp_args:
            return
        self.ensure_backup()
        out = self.wp(*wp_args)
        for line in (out.stdout + out.stderr).strip().splitlines():
            self.log("  " + line)

    def ensure_backup(self):
        if self.backup_taken:
            return
        d = self.cfg["backup_dir"]
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        path = os.path.join(d, f"wordpress-db-{ts}.sql.gz")
        dump = (
            'exec mysqldump --no-tablespaces --single-transaction --set-gtid-purged=OFF '
            '--skip-lock-tables --databases "$MYSQL_DATABASE" '
            '-u"$MYSQL_USER" -p"$MYSQL_PASSWORD"'
        )
        proc = subprocess.Popen(
            ["docker", "exec", self.cfg["db_container"], "sh", "-c", dump],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        tail = b""
        with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
                gz.write(chunk)
                tail = (tail + chunk)[-256:]
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0 or b"-- Dump completed" not in tail:
            os.unlink(path)
            raise RuntimeError(f"DB dump failed: {err.strip()}")
        self.backup_taken = True
        self.log(f"db dump: {path}")
        dumps = sorted(glob.glob(os.path.join(d, "wordpress-db-*.sql.gz")))
        for old in dumps[:-int(self.cfg["backup_keep"])]:
            os.unlink(old)
            self.log(f"db dump pruned: {old}")

    # -- steps ------------------------------------------------------------

    def image_tag_check(self):
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.Config.Image}}", self.cfg["container"]],
            capture_output=True, text=True)
        m = re.search(r":(\d+\.\d+(?:\.\d+)?)", res.stdout)
        pin = self.cfg["core_version"]
        if m and m.group(1) != pin and not (pin.endswith(".0") and m.group(1) == pin[:-2]):
            self.warnings.append(
                f"image tag {m.group(1)} differs from core pin {pin}; core follows the pin")

    def core(self):
        want = self.cfg["core_version"]
        have = self.wp("core", "version").stdout.strip()
        self.log(f"core: {have} (declared {want})")
        if have != want:
            self.act(f"core {have} -> {want}",
                     "core", "update", f"--version={want}", "--force")
        if self.check and have != want:
            self.act("core update-db (after core update)")
            return
        dbv = self.wp("eval", 'global $wp_db_version; echo get_option("db_version"), " ", $wp_db_version;')
        stored, code = (dbv.stdout.split() + ["", ""])[:2]
        if stored != code:
            self.act(f"core update-db ({stored} -> {code})", "core", "update-db")

    def themes(self):
        want = self.cfg["themes"]
        active = self.cfg["active_theme"]
        if active not in want:
            raise RuntimeError(f"active_theme {active} is not in the theme list")
        have = {t["name"]: t for t in self.wp_json("theme", "list")}
        for name, ver in want.items():
            cur = have.get(name, {}).get("version")
            if cur != ver:
                self.act(f"theme {name} {cur or 'missing'} -> {ver}",
                         "theme", "install", name, f"--version={ver}", "--force")
        cur_active = self.wp("option", "get", "stylesheet").stdout.strip()
        if cur_active != active:
            self.act(f"theme activate {active} (was {cur_active})", "theme", "activate", active)
        for name in sorted(set(have) - set(want)):
            self.act(f"theme delete {name} (not declared)", "theme", "delete", name)

    def plugins(self):
        want = self.cfg["plugins"]
        have = {
            p["name"]: p for p in self.wp_json("plugin", "list")
            if p.get("status") in ("active", "inactive", "active-network")
        }
        for name, ver in want.items():
            cur = have.get(name, {}).get("version")
            if cur != ver:
                self.act(f"plugin {name} {cur or 'missing'} -> {ver}",
                         "plugin", "install", name, f"--version={ver}", "--force")
            if have.get(name, {}).get("status") != "active":
                self.act(f"plugin activate {name}", "plugin", "activate", name)
        for name in sorted(set(have) - set(want)):
            p = have[name]
            self.log(f"removing plugin not on the allowlist: {name} {p.get('version')} ({p.get('status')})")
            if p.get("status") != "inactive":
                self.act(f"plugin deactivate {name} (not declared)", "plugin", "deactivate", name)
            self.act(f"plugin delete {name} (not declared)", "plugin", "delete", name)

    def _fix(self, path, st, mode, uid, gid):
        diffs = []
        if stat.S_IMODE(st.st_mode) != mode:
            diffs.append(f"mode {stat.S_IMODE(st.st_mode):04o}->{mode:04o}")
        if (st.st_uid, st.st_gid) != (uid, gid):
            diffs.append(f"owner {st.st_uid}:{st.st_gid}->{uid}:{gid}")
        if not diffs:
            return False
        if not self.check:
            os.lchown(path, uid, gid)
            os.chmod(path, mode)
        return diffs

    def permissions(self):
        uid, gid = int(self.cfg["uid"]), int(self.cfg["gid"])
        fixed = []

        def visit(path, mode):
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                return
            d = self._fix(path, st, mode, uid, gid)
            if d:
                fixed.append(f"{path}: {', '.join(d)}")

        for p in self.cfg["permission_paths"]:
            if os.path.lexists(p["path"]):
                visit(p["path"], int(p["mode"], 8))
        for tree in self.cfg["permission_trees"]:
            root = tree["path"]
            if not os.path.isdir(root):
                continue
            dmode, fmode = int(tree["dir_mode"], 8), int(tree["file_mode"], 8)
            overrides = {os.path.join(root, k): int(v, 8) for k, v in tree["overrides"].items()}
            for dirpath, dirnames, filenames in os.walk(root):
                visit(dirpath, overrides.get(dirpath, dmode))
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if not stat.S_ISREG(os.lstat(fp).st_mode):
                        continue
                    visit(fp, overrides.get(fp, fmode))
        if fixed:
            self.act(f"permissions: {len(fixed)} path(s) corrected")
            for line in fixed[:50]:
                self.log("  " + line)
            if len(fixed) > 50:
                self.log(f"  ... and {len(fixed) - 50} more")

    def verify(self):
        if self.check and self.actions:
            self.log("checksums: skipped in check mode (changes pending)")
            return
        res = self.wp("core", "verify-checksums", ok_rc=(0, 1))
        out = (res.stdout + res.stderr).strip()
        for line in out.splitlines():
            self.log("checksums: " + line)
        if res.returncode != 0:
            raise RuntimeError("wp core verify-checksums failed")

    def run(self):
        self.image_tag_check()
        self.core()
        self.themes()
        self.plugins()
        self.permissions()
        self.verify()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-b64", required=True)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    cfg = json.loads(base64.b64decode(args.config_b64))
    r = Reconciler(cfg, args.check)
    error = None
    try:
        r.run()
    except Exception as e:  # report what was done before failing
        error = str(e)
        print(f"ERROR: {error}", flush=True)
    for w in r.warnings:
        print(f"WARNING: {w}", flush=True)
    print(json.dumps({
        "changed": bool(r.actions), "check": args.check, "actions": r.actions,
        "warnings": r.warnings, "backup_taken": r.backup_taken, "error": error,
    }))
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
