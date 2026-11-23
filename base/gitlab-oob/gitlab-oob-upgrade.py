#!/usr/bin/env python3
"""Out-of-band GitLab upgrades (docs/AUTOMERGE.md, "GitLab updates").

Runs on the router from gitlab-oob-upgrade.timer, outside GitLab and its CI.
GitLab's running image is pinned in <state_dir>/image; every compose render
on the router applies that pin (ansible render_compose.yml), so no CI deploy
changes GitLab's image. When main's gitlab
image line differs from the pin, this script:

  1. checks: running container == pin, no hold on the new image, background
     migrations finished, GitLab healthy, free space; an auto-merged change
     (trailer "Automerge-Class: gitlab-oob") must be digest or same-minor patch;
  2. pulls the new image;
  3. takes a restore point: warm rsync of GitLab's directory into a ZFS
     dataset, `docker stop gitlab`, final rsync, `zfs snapshot`;
  4. moves the pin and the rendered compose line, starts the new image;
  5. health gate: container healthy, /-/readiness?all=1, API version, git
     ls-remote, runners back online;
  6. on failure restores the snapshot and the old image, re-checks health,
     holds the image, mails root, then opens a rollback issue and a revert MR.

Modes: default (act), --dry-run (report only), --warm (refresh the mirror only).
Log lines start with "gitlab_oob event=" (journald -> Loki).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

CONFIG = "/etc/gitlab-oob/config.json"
CLASS_TRAILER = "Automerge-Class: gitlab-oob"
PENDING_LABEL = "automerge-gitlab-pending"
ROLLBACK_LABEL = "automerge-rollback"
HOLD_JOB = "gitlab-oob"
BLOCKING_MIGRATIONS = {"active", "paused", "failed", "finalizing"}

IMAGE_RE = re.compile(
    r"^(?P<name>gitlab/gitlab-(?:ee|ce)):(?P<tag>[\w][\w.-]*)@(?P<digest>sha256:[a-f0-9]{64})$")
# The literal image line of the gitlab service in the compose template.
# The Jinja line next to it ('{{ gitlab_oob_image }}') does not match.
LINE_RE = re.compile(
    r"""^\s+image:\s*['"]?(gitlab/gitlab-(?:ee|ce):[\w][\w.-]*@sha256:[a-f0-9]{64})['"]?\s*$""", re.M)
TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)-(ee|ce)\.(\d+)$")
MR_RE = re.compile(r"See merge request \S+!(\d+)")


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in test_gitlab_oob.py)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Image:
    name: str
    tag: str
    digest: str

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.tag}@{self.digest}"


def parse_image(ref: str) -> Image:
    m = IMAGE_RE.match(ref.strip())
    if not m:
        raise ValueError(f"not a digest-pinned GitLab image: {ref!r}")
    return Image(m["name"], m["tag"], m["digest"])


def desired_from_compose(text: str) -> Image:
    """The gitlab image named by the compose template: exactly one literal,
    digest-pinned gitlab/gitlab-{ee,ce} image line."""
    refs = LINE_RE.findall(text)
    if len(refs) != 1:
        raise ValueError(f"expected exactly one gitlab image line in the compose file, found {len(refs)}")
    return parse_image(refs[0])


def classify(old: Image, new: Image) -> str:
    """digest | patch | minor | major | edition | unknown."""
    if old.name != new.name:
        return "edition"
    if old.tag == new.tag:
        return "digest" if old.digest != new.digest else "same"
    a, b = TAG_RE.match(old.tag), TAG_RE.match(new.tag)
    if not a or not b or a[4] != b[4]:
        return "unknown"
    (oM, om, op), (nM, nm, np_) = (tuple(int(x) for x in a.groups()[:3]),
                                   tuple(int(x) for x in b.groups()[:3]))
    if (nM, nm, np_) < (oM, om, op):
        return "downgrade" if (nM, nm, np_, int(b[5])) < (oM, om, op, int(a[5])) else "unknown"
    if nM != oM:
        return "major"
    if nm != om:
        return "minor"
    return "patch"


def api_version(tag: str) -> str:
    """The version GitLab's API reports for an image tag: 19.6.1-ee.0 -> 19.6.1-ee."""
    m = TAG_RE.match(tag)
    return f"{m[1]}.{m[2]}.{m[3]}-{m[4]}" if m else tag


def blocking_migrations(rows: List[dict]) -> List[str]:
    return [f"{r.get('job_class_name', '?')} ({r.get('status')})" for r in rows
            if str(r.get("status", "")).lower() in BLOCKING_MIGRATIONS]


def snapshot_name(dataset: str, now: dt.datetime, old: Image, new: Image) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    clean = lambda t: re.sub(r"[^A-Za-z0-9._-]", "_", t)  # noqa: E731
    return f"{dataset}@oob-{stamp}-{clean(old.tag)}-to-{clean(new.tag)}"


def snapshots_to_prune(names: List[str], keep: int) -> List[str]:
    """Our snapshots (`@oob-`), oldest first as listed; all but the newest `keep`."""
    ours = [n for n in names if "@oob-" in n]
    return ours[:-keep] if keep > 0 else ours


def merge_info(message: str) -> Tuple[bool, Optional[int]]:
    """(auto-merged by the gitlab-oob class, MR iid) from a merge commit message."""
    m = MR_RE.search(message)
    return (CLASS_TRAILER in message.splitlines(), int(m[1]) if m else None)


def swap_compose_line(text: str, old: Image, new: Image) -> str:
    """Replace the one occurrence of the old image ref in the rendered compose file."""
    if text.count(old.ref) != 1:
        raise ValueError(f"rendered compose file has {text.count(old.ref)} occurrences of {old.ref}, expected 1")
    if new.ref in text:
        raise ValueError(f"rendered compose file already contains {new.ref}")
    return text.replace(old.ref, new.ref)


@dataclass
class Decision:
    action: str            # noop | hold | refuse | drift | upgrade
    reason: str = ""
    kind: str = ""


def decide(*, desired: Image, pinned: Image, running: Optional[str], hold: Optional[dict],
           auto: bool) -> Decision:
    if running != pinned.ref:
        return Decision("drift", f"running container image {running!r} is not the pinned {pinned.ref}")
    if desired == pinned:
        return Decision("noop", "main matches the pinned image")
    if hold and hold.get("image") == desired.ref:
        return Decision("hold", f"{desired.ref} failed on {hold.get('at')}: {hold.get('reason')}")
    kind = classify(pinned, desired)
    if kind in ("edition", "downgrade", "unknown"):
        return Decision("refuse", f"{kind} change {pinned.tag} -> {desired.tag} is never done automatically", kind)
    if auto and kind not in ("digest", "patch"):
        return Decision("refuse", f"auto-merged change is a {kind} update; only digest and patch "
                                  "updates may come through the gitlab-oob class", kind)
    return Decision("upgrade", f"{kind} update {pinned.tag} -> {desired.tag}", kind)


# --------------------------------------------------------------------------
# Side effects
# --------------------------------------------------------------------------

def log(event: str, **kv) -> None:
    parts = " ".join(f"{k}={json.dumps(v) if isinstance(v, str) and (' ' in v or not v) else v}"
                     for k, v in kv.items())
    print(f"gitlab_oob event={event} {parts}".rstrip(), flush=True)


def run(cmd: List[str], *, timeout: int = 600, check: bool = True, input: Optional[str] = None,
        cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input, cwd=cwd)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... exited {res.returncode}: {res.stderr.strip()[:400]}")
    return res


class Host:
    """Everything that touches the router, GitLab or ZFS."""

    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg = cfg
        self.dry = dry_run
        self.state = cfg["state_dir"]

    # ---- state files
    def pin_path(self) -> str:
        return os.path.join(self.state, "image")

    def read_pin(self) -> Image:
        with open(self.pin_path()) as f:
            return parse_image(f.read().strip())

    def write_pin(self, img: Image) -> None:
        tmp = self.pin_path() + ".tmp"
        with open(tmp, "w") as f:
            f.write(img.ref + "\n")
        os.replace(tmp, self.pin_path())

    def read_hold(self) -> Optional[dict]:
        try:
            with open(os.path.join(self.state, "hold.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def write_hold(self, image: Image, reason: str) -> None:
        with open(os.path.join(self.state, "hold.json"), "w") as f:
            json.dump({"image": image.ref, "reason": reason,
                       "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}, f)

    def write_status(self, **kv) -> None:
        kv["at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        if self.dry:
            return
        with open(os.path.join(self.state, "status.json"), "w") as f:
            json.dump(kv, f, indent=1)

    # ---- git (read-only: fetch, show, log)
    def fetch(self) -> None:
        run(["git", "-C", self.cfg["repo"], "fetch", "-q", "origin", "main"], timeout=120)

    def main_compose(self) -> str:
        return run(["git", "-C", self.cfg["repo"], "show",
                    f"origin/main:{self.cfg['compose_repo_path']}"]).stdout

    def merge_message(self, img: Image) -> str:
        """Message of the merge commit on main that brought in `img` (or of the
        commit itself when it was not merged through an MR)."""
        repo, path = self.cfg["repo"], self.cfg["compose_repo_path"]
        c = run(["git", "-C", repo, "log", "-1", "--format=%H", f"-S{img.digest}", "origin/main",
                 "--", path]).stdout.strip()
        if not c:
            return ""
        merges = run(["git", "-C", repo, "rev-list", "--ancestry-path", "--merges", "--reverse",
                      f"{c}..origin/main"]).stdout.split()
        target = merges[0] if merges else c
        return run(["git", "-C", repo, "log", "-1", "--format=%B", target]).stdout

    # ---- docker
    def running_image(self) -> Optional[str]:
        res = run(["docker", "inspect", "-f", "{{.Config.Image}}", self.cfg["container"]], check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def container_health(self) -> str:
        res = run(["docker", "inspect", "-f", "{{.State.Health.Status}}", self.cfg["container"]], check=False)
        return res.stdout.strip() if res.returncode == 0 else "missing"

    def readiness(self) -> bool:
        res = run(["docker", "exec", self.cfg["container"], "curl", "-fsS", "-m", "20",
                   "http://localhost/-/readiness?all=1"], check=False, timeout=60)
        if res.returncode != 0:
            return False
        try:
            return json.loads(res.stdout).get("status") == "ok"
        except ValueError:
            return False

    def compose(self, *args: str, timeout: int = 900) -> None:
        cmd = ["docker", "compose", "-p", self.cfg["compose_project"],
               "--project-directory", self.cfg["compose_dir"]]
        for f in self.cfg["compose_files"]:
            cmd += ["-f", os.path.join(self.cfg["compose_dir"], f)]
        run(cmd + list(args), timeout=timeout)

    def stop(self) -> None:
        run(["docker", "stop", "-t", "300", self.cfg["container"]], timeout=420)

    # ---- GitLab API (through glab, as root)
    def api(self, path: str, method: str = "GET", fields: Optional[Dict[str, str]] = None,
            timeout: int = 60) -> object:
        cmd = ["glab", "api", "-X", method, path]
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        res = run(cmd, timeout=timeout, cwd=self.cfg["repo"])
        return json.loads(res.stdout) if res.stdout.strip() else None

    def migrations(self) -> List[dict]:
        rows: List[dict] = []
        for db in ("main", "ci"):
            rows += self.api(f"admin/batched_background_migrations?database={db}&per_page=100") or []
        return rows

    def version(self) -> str:
        return (self.api("version") or {}).get("version", "")

    def online_runners(self) -> int:
        return len(self.api("runners/all?status=online&per_page=100") or [])

    def ls_remote(self) -> bool:
        return run(["git", "-C", self.cfg["repo"], "ls-remote", "origin", "HEAD"],
                   check=False, timeout=60).returncode == 0

    # ---- ZFS + rsync
    def mirror_dir(self) -> str:
        return os.path.join(self.cfg["mirror_mount"], "gitlab")

    def ensure_dataset(self) -> None:
        ds = self.cfg["zfs_dataset"]
        if run(["zfs", "list", "-H", "-o", "name", ds], check=False).returncode != 0:
            run(["zfs", "create", "-p", "-o", f"mountpoint={self.cfg['mirror_mount']}", ds])
            log("dataset_created", dataset=ds)
        os.makedirs(self.mirror_dir(), exist_ok=True)

    def rsync(self, src: str, dst: str) -> None:
        cmd = ["rsync", "-aHX", "--numeric-ids", "--delete",
               f"--bwlimit={int(self.cfg['rate_mb']) * 1024}"]
        for e in self.cfg.get("exclude", []):
            cmd.append(f"--exclude={e}")
        run(cmd + [src.rstrip("/") + "/", dst.rstrip("/") + "/"], timeout=int(self.cfg["copy_timeout"]))

    def free_bytes(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def used_bytes(self, path: str) -> int:
        out = run(["du", "-sb", "--exclude=logs", path], timeout=1800).stdout.split()
        return int(out[0])

    def snapshot(self, name: str) -> None:
        run(["zfs", "snapshot", name])

    def snapshots(self) -> List[str]:
        out = run(["zfs", "list", "-H", "-t", "snapshot", "-o", "name", "-s", "creation", "-d", "1",
                   self.cfg["zfs_dataset"]], check=False).stdout
        return [x for x in out.split() if x]

    def destroy(self, name: str) -> None:
        if "@oob-" not in name:
            raise ValueError(f"refusing to destroy {name}")
        run(["zfs", "destroy", name])

    def snapshot_dir(self, name: str) -> str:
        return os.path.join(self.cfg["mirror_mount"], ".zfs", "snapshot", name.split("@", 1)[1], "gitlab")

    # ---- notification
    def mail(self, subject: str, body: str) -> None:
        res = run(["mail", "-s", subject, "root"], input=body, check=False, timeout=60)
        log("mailed" if res.returncode == 0 else "mail_failed", subject=subject)


# --------------------------------------------------------------------------
# The upgrade
# --------------------------------------------------------------------------

def health_gate(h: Host, image: Image, runners_before: int, timeout: int) -> Tuple[bool, str]:
    want = api_version(image.tag)
    deadline = time.monotonic() + timeout
    last = "not checked yet"
    while time.monotonic() < deadline:
        try:
            health = h.container_health()
            if health != "healthy":
                last = f"container {health}"
            elif not h.readiness():
                last = "readiness?all=1 not ok"
            elif (v := h.version()) != want:
                last = f"API reports {v!r}, expected {want!r}"
            elif not h.ls_remote():
                last = "git ls-remote failed"
            elif (n := h.online_runners()) < runners_before:
                last = f"{n} runners online, {runners_before} before"
            else:
                return True, "healthy"
        except Exception as e:  # noqa: BLE001 - keep polling until the deadline
            last = f"check error: {e}"
        time.sleep(20)
    return False, last


def preflight(h: Host) -> Tuple[int, List[str]]:
    problems = []
    if h.container_health() != "healthy" or not h.readiness():
        problems.append("GitLab is not healthy now")
    try:
        blocked = blocking_migrations(h.migrations())
        if blocked:
            problems.append("background migrations not finished: " + ", ".join(blocked[:5]))
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not read background migrations: {e}")
    runners = 0
    try:
        runners = h.online_runners()
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not count runners: {e}")
    try:
        need = int(h.used_bytes(h.cfg["data_dir"]) * 1.2)
        have = h.free_bytes(os.path.dirname(h.cfg["mirror_mount"].rstrip("/")) or "/")
        if have < need:
            problems.append(f"not enough space for the restore point: {have >> 30} GiB free, {need >> 30} GiB needed")
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not check space: {e}")
    return runners, problems


def report_gitlab(h: Host, iid: Optional[int], body: str, *, remove_pending: bool = False,
                  add_labels: str = "") -> None:
    if not iid:
        return
    pid = h.cfg["project_id"]
    try:
        h.api(f"projects/{pid}/merge_requests/{iid}/notes", "POST", {"body": body})
        if remove_pending or add_labels:
            f = {}
            if remove_pending:
                f["remove_labels"] = PENDING_LABEL
            if add_labels:
                f["add_labels"] = add_labels
            h.api(f"projects/{pid}/merge_requests/{iid}", "PUT", f)
    except Exception as e:  # noqa: BLE001
        log("gitlab_note_failed", mr=iid, error=str(e)[:200])


def open_rollback(h: Host, iid: Optional[int], old: Image, new: Image, why: str) -> None:
    """Rollback issue (holds further gitlab-oob merges) and a revert MR on main."""
    pid = h.cfg["project_id"]
    path = h.cfg["compose_repo_path"]
    try:
        h.fetch()
        text = h.main_compose()
        branch = f"automerge-revert/gitlab-{new.tag}-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M}"
        if text.count(new.ref) == 1:
            h.api(f"projects/{pid}/repository/commits", "POST", {
                "branch": branch, "start_branch": "main",
                "commit_message": f"Revert gitlab to {old.tag}",
                "actions[][action]": "update", "actions[][file_path]": path,
                "actions[][content]": text.replace(new.ref, old.ref)})
            mr = h.api(f"projects/{pid}/merge_requests", "POST", {
                "source_branch": branch, "target_branch": "main", "remove_source_branch": "true",
                "title": f"Revert gitlab to {old.tag}",
                "labels": ROLLBACK_LABEL,
                "description": f"The out-of-band upgrade to {new.tag} failed and was rolled back "
                               f"({why}). Merge this to put main back on the running image."})
            log("revert_mr", mr=(mr or {}).get("iid"))
        issue = h.api(f"projects/{pid}/issues", "POST", {
            "title": f"Auto-merge rollback: gitlab {new.tag}" + (f" (!{iid})" if iid else ""),
            "labels": ROLLBACK_LABEL,
            "description": (f"The out-of-band upgrade of gitlab {old.tag} -> {new.tag} failed its health "
                            f"gate ({why}). The restore point was put back and {old.tag} runs again.\n\n"
                            "While this issue is open, no gitlab update is auto-merged. Merge or close "
                            "the revert MR, then close this issue. The host keeps a hold on the failed "
                            "image in the agent's hold file until main names a different one.\n\n"
                            f"<!-- automerge-rollback job={HOLD_JOB} services=gitlab -->")})
        log("rollback_issue", issue=(issue or {}).get("iid"))
    except Exception as e:  # noqa: BLE001
        log("rollback_gitlab_failed", error=str(e)[:300])
        h.mail("gitlab-oob: rollback done, but the GitLab issue/revert MR failed",
               f"Rollback of gitlab {new.tag} succeeded ({old.tag} runs again), but opening the "
               f"rollback issue or revert MR failed: {e}\nOpen them by hand.")


def rendered_file(h: Host, img: Image) -> Optional[str]:
    """The rendered compose file (of this host's compose files) naming `img`."""
    for f in h.cfg["compose_files"]:
        path = os.path.join(h.cfg["compose_dir"], f)
        try:
            with open(path) as fh:
                if img.ref in fh.read():
                    return path
        except FileNotFoundError:
            continue
    return None


def rewrite_rendered(h: Host, old: Image, new: Image) -> None:
    """Swap the gitlab image line in the rendered compose file, atomically."""
    path = rendered_file(h, old)
    if not path:
        if rendered_file(h, new):
            return      # already swapped (e.g. an interrupted earlier run)
        raise ValueError(f"no rendered compose file names {old.ref}")
    with open(path) as f:
        text = swap_compose_line(f.read(), old, new)
    tmp = path + ".oob.tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)


def upgrade(h: Host, old: Image, new: Image, kind: str, iid: Optional[int], runners_before: int) -> int:
    cfg = h.cfg
    data = cfg["data_dir"]
    h.ensure_dataset()
    log("pull", image=new.ref)
    run(["docker", "pull", new.ref], timeout=1800)

    log("warm_sync_start")
    t0 = time.monotonic()
    h.rsync(data, h.mirror_dir())
    log("warm_sync_done", seconds=int(time.monotonic() - t0))

    snap = snapshot_name(cfg["zfs_dataset"], dt.datetime.now(dt.timezone.utc), old, new)
    down = time.monotonic()
    log("stop", container=cfg["container"])
    h.stop()
    try:
        h.rsync(data, h.mirror_dir())
        h.snapshot(snap)
    except Exception as e:  # noqa: BLE001 - restart the old version untouched
        log("restore_point_failed", error=str(e)[:300])
        h.compose("up", "-d", "--no-deps", cfg["container"])
        h.mail("gitlab-oob: restore point failed, upgrade not attempted",
               f"Taking the restore point for gitlab {old.tag} -> {new.tag} failed: {e}\n"
               f"{old.tag} was started again unchanged.")
        h.write_status(state="restore_point_failed", old=old.ref, new=new.ref, error=str(e)[:300])
        return 1
    log("restore_point", snapshot=snap, offline_seconds=int(time.monotonic() - down))

    # From here on GitLab is stopped: any failure goes through the rollback,
    # which puts the restore point and the old image back and starts it.
    try:
        rewrite_rendered(h, old, new)
        h.write_pin(new)
        h.write_status(state="upgrading", old=old.ref, new=new.ref, snapshot=snap)
        log("start", image=new.ref)
        h.compose("up", "-d", "--no-deps", cfg["container"])
        ok, why = health_gate(h, new, runners_before, int(cfg["health_timeout"]))
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"start failed: {e}"
    if ok:
        log("upgraded", old=old.tag, new=new.tag, kind=kind,
            total_downtime_seconds=int(time.monotonic() - down))
        h.write_status(state="done", old=old.ref, new=new.ref, snapshot=snap)
        for s in snapshots_to_prune(h.snapshots(), int(cfg["keep"])):
            h.destroy(s)
            log("snapshot_pruned", snapshot=s)
        report_gitlab(h, iid, f"Out-of-band upgrade done: gitlab {old.tag} -> {new.tag} ({kind}), "
                              f"health gate passed. Restore point `{snap}` kept.", remove_pending=True)
        return 0
    return rollback(h, old, new, snap, why, iid, runners_before)


def rollback(h: Host, old: Image, new: Image, snap: str, why: str, iid: Optional[int],
             runners_before: int) -> int:
    cfg = h.cfg
    log("rollback_start", reason=why, snapshot=snap)
    h.write_hold(new, why)
    restored = False
    try:
        h.stop()
        h.rsync(h.snapshot_dir(snap), cfg["data_dir"])
        if rendered_file(h, new):
            rewrite_rendered(h, new, old)
        h.write_pin(old)
        h.compose("up", "-d", "--no-deps", cfg["container"])
        restored, after = health_gate(h, old, max(0, runners_before - 1), int(cfg["health_timeout"]))
    except Exception as e:  # noqa: BLE001
        after = f"rollback error: {e}"
    if not restored:
        log("rollback_failed", reason=after)
        h.write_status(state="rollback_failed", old=old.ref, new=new.ref, snapshot=snap, error=after)
        h.mail("gitlab-oob: ROLLBACK FAILED, GitLab needs hands",
               f"Upgrade gitlab {old.tag} -> {new.tag} failed ({why}); the rollback did not pass its "
               f"health gate either ({after}).\n\nRestore point: {snap}\n"
               f"Runbook: docs/AUTOMERGE.md, 'GitLab updates' > 'Manual restore'.")
        return 2
    log("rolled_back", old=old.tag, new=new.tag, reason=why)
    h.write_status(state="rolled_back", old=old.ref, new=new.ref, snapshot=snap, reason=why)
    h.mail(f"gitlab-oob: upgrade to {new.tag} rolled back",
           f"The upgrade gitlab {old.tag} -> {new.tag} failed its health gate ({why}).\n"
           f"The restore point {snap} was put back and {old.tag} is healthy again.\n"
           f"The host holds {new.ref} and will not retry it. A rollback issue and revert MR follow.")
    report_gitlab(h, iid, f"Out-of-band upgrade to {new.tag} failed its health gate ({why}) and was "
                          f"rolled back to {old.tag} with data restored from `{snap}`.",
                  remove_pending=True, add_labels=ROLLBACK_LABEL)
    open_rollback(h, iid, old, new, why)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=CONFIG)
    p.add_argument("--dry-run", action="store_true", help="report the decision and checks, change nothing")
    p.add_argument("--warm", action="store_true", help="only refresh the mirror (no stop, no snapshot)")
    a = p.parse_args(argv)
    with open(a.config) as f:
        cfg = json.load(f)
    h = Host(cfg, a.dry_run)
    os.makedirs(cfg["state_dir"], exist_ok=True)
    lock = open(os.path.join(cfg["state_dir"], ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("locked", note="another run is in progress")
        return 0

    if a.warm:
        if a.dry_run:
            log("dry_run", would="refresh the mirror")
            return 0
        h.ensure_dataset()
        t0 = time.monotonic()
        h.rsync(cfg["data_dir"], h.mirror_dir())
        log("warm_sync_done", seconds=int(time.monotonic() - t0))
        return 0

    try:
        h.fetch()
        desired = desired_from_compose(h.main_compose())
        pinned = h.read_pin()
    except Exception as e:  # noqa: BLE001
        log("error", stage="read", error=str(e)[:300])
        return 1
    auto, iid = merge_info(h.merge_message(desired)) if desired != pinned else (False, None)
    d = decide(desired=desired, pinned=pinned, running=h.running_image(), hold=h.read_hold(), auto=auto)
    log("decision", action=d.action, reason=d.reason, desired=desired.tag, pinned=pinned.tag,
        auto=auto, mr=iid or "")
    if d.action == "noop":
        return 0
    if d.action == "hold":
        return 0
    if d.action in ("drift", "refuse"):
        if not a.dry_run:
            prev = {}
            try:
                with open(os.path.join(cfg["state_dir"], "status.json")) as f:
                    prev = json.load(f)
            except (FileNotFoundError, ValueError):
                pass
            if prev.get("state") != d.action or prev.get("reason") != d.reason:
                h.mail(f"gitlab-oob: {d.action}", d.reason + "\n\nNothing was changed.")
            h.write_status(state=d.action, reason=d.reason)
        return 1

    runners, problems = preflight(h)
    log("preflight", runners_online=runners, problems=len(problems))
    for prob in problems:
        log("preflight_problem", problem=prob)
    if a.dry_run:
        log("dry_run", would=f"upgrade {pinned.ref} -> {desired.ref}" if not problems else "wait",
            snapshot=snapshot_name(cfg["zfs_dataset"], dt.datetime.now(dt.timezone.utc), pinned, desired))
        return 0
    if problems:
        h.write_status(state="waiting", reason="; ".join(problems))
        return 0
    try:
        return upgrade(h, pinned, desired, d.kind, iid, runners)
    except Exception as e:  # noqa: BLE001
        log("error", stage="upgrade", error=str(e)[:300])
        h.mail("gitlab-oob: upgrade aborted", f"Upgrade gitlab {pinned.tag} -> {desired.tag} aborted: {e}\n"
               "Check `docker ps`, the pin file and the status file; see the runbook.")
        h.write_status(state="error", reason=str(e)[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
