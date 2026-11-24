#!/usr/bin/env python3
"""Out-of-band upgrades of the services the pipeline itself depends on
(docs/AUTOMERGE.md, "Out-of-band updates"): GitLab and Vault.

Runs on the router from oob-upgrade@<service>.timer, outside GitLab and its
CI. The service's running image is pinned in <state_dir>/image; every compose
render on the router applies that pin (ansible render_compose.yml), so no CI
deploy changes the image. When main's image line for the service differs from
the pin, this script:

  1. checks: running container == pin, no hold on the new image, the
     service-specific preflight (GitLab: background migrations finished;
     Vault: unsealed, canary read works, no CI job running), healthy, free
     space; an auto-merged change (trailer "Automerge-Class: oob") must be a
     digest or patch update;
  2. pulls the new image;
  3. takes a restore point: warm rsync of the service's data directory into a
     ZFS dataset, `docker stop`, final rsync, `zfs snapshot`;
  4. moves the pin and the rendered compose line, starts the new image;
  5. runs the service's health gate (GitLab: readiness, API version, git
     ls-remote, runners back; Vault: unsealed at the new version, an AppRole
     login and a KV read);
  6. on failure restores the snapshot and the old image, re-checks health,
     holds the image, mails root, then opens a rollback issue and a revert MR.

Usage: oob-upgrade --service gitlab|vault [--dry-run | --warm]
Config: /etc/oob-upgrade/<service>.json. Log lines start with
"oob event=" and carry service=<name> (journald -> Loki).
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
import urllib.request
import ssl
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

CONFIG_DIR = "/etc/oob-upgrade"
# One out-of-band upgrade on the host at a time, whatever the service.
GLOBAL_LOCK = "/run/oob-upgrade.lock"
CLASS_TRAILER = "Automerge-Class: oob"
PENDING_LABEL = "automerge-oob-pending"
ROLLBACK_LABEL = "automerge-rollback"
HOLD_JOB = "oob"
BLOCKING_MIGRATIONS = {"active", "paused", "failed", "finalizing"}
MR_RE = re.compile(r"See merge request \S+!(\d+)")
SERVICE = "?"           # set by main(); used in log lines


# --------------------------------------------------------------------------
# Profiles: what differs per service
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    name: str
    image_names: Tuple[str, ...]      # accepted repository names
    tag_re: str                       # groups 1-3 = major, minor, patch; optional "flavour" and "rev"
    stop_timeout: int

    @property
    def image_re(self) -> "re.Pattern[str]":
        names = "|".join(re.escape(n) for n in self.image_names)
        return re.compile(rf"^(?P<name>{names}):(?P<tag>[\w][\w.-]*)@(?P<digest>sha256:[a-f0-9]{{64}})$")

    @property
    def line_re(self) -> "re.Pattern[str]":
        """The literal image line of the service in the compose template. The
        Jinja line next to it (a pin variable) does not match."""
        names = "|".join(re.escape(n) for n in self.image_names)
        return re.compile(
            rf"""^\s+image:\s*['"]?((?:{names}):[\w][\w.-]*@sha256:[a-f0-9]{{64}})['"]?\s*$""", re.M)


PROFILES: Dict[str, Profile] = {
    # 19.6.1-ee.0: major.minor.patch-flavour.rev; a rev bump is a patch.
    "gitlab": Profile("gitlab", ("gitlab/gitlab-ee", "gitlab/gitlab-ce"),
                      r"^(\d+)\.(\d+)\.(\d+)-(?P<flavour>ee|ce)\.(?P<rev>\d+)$", 300),
    # 2.2.2; anything else (-ent, -rc, ...) is "unknown" and never automatic.
    "vault": Profile("vault", ("hashicorp/vault",), r"^(\d+)\.(\d+)\.(\d+)$", 60),
}


# --------------------------------------------------------------------------
# Pure helpers (unit-tested in test_oob_upgrade.py)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Image:
    name: str
    tag: str
    digest: str

    @property
    def ref(self) -> str:
        return f"{self.name}:{self.tag}@{self.digest}"


def parse_image(p: Profile, ref: str) -> Image:
    m = p.image_re.match(ref.strip())
    if not m:
        raise ValueError(f"not a digest-pinned {p.name} image: {ref!r}")
    return Image(m["name"], m["tag"], m["digest"])


def desired_from_compose(p: Profile, text: str) -> Image:
    """The service's image named by the compose template: exactly one literal,
    digest-pinned image line of one of the profile's repositories."""
    refs = p.line_re.findall(text)
    if len(refs) != 1:
        raise ValueError(f"expected exactly one {p.name} image line in the compose file, found {len(refs)}")
    return parse_image(p, refs[0])


def _version(p: Profile, tag: str):
    m = re.match(p.tag_re, tag)
    if not m:
        return None
    gd = m.groupdict()
    return (tuple(int(x) for x in m.groups()[:3]), gd.get("flavour"), int(gd["rev"]) if gd.get("rev") else 0)


def classify(p: Profile, old: Image, new: Image) -> str:
    """digest | same | patch | minor | major | edition | downgrade | unknown."""
    if old.name != new.name:
        return "edition"
    if old.tag == new.tag:
        return "digest" if old.digest != new.digest else "same"
    a, b = _version(p, old.tag), _version(p, new.tag)
    if not a or not b or a[1] != b[1]:
        return "unknown"
    (ov, _, orev), (nv, _, nrev) = a, b
    if nv < ov or (nv == ov and nrev < orev):
        return "downgrade"
    if nv[0] != ov[0]:
        return "major"
    if nv[1] != ov[1]:
        return "minor"
    return "patch"


def gitlab_api_version(tag: str) -> str:
    """The version GitLab's API reports for an image tag: 19.6.1-ee.0 -> 19.6.1-ee."""
    m = re.match(PROFILES["gitlab"].tag_re, tag)
    return f"{m[1]}.{m[2]}.{m[3]}-{m['flavour']}" if m else tag


def blocking_migrations(rows: List[dict]) -> List[str]:
    return [f"{r.get('job_class_name', '?')} ({r.get('status')})" for r in rows
            if str(r.get("status", "")).lower() in BLOCKING_MIGRATIONS]


def vault_status_problem(status: dict, want_version: str) -> Optional[str]:
    """None when `vault status -format=json` shows an initialized, unsealed
    Vault at `want_version` (a "+ent" style suffix in the reported version is
    tolerated); else what is wrong."""
    if not status.get("initialized"):
        return "Vault reports not initialized"
    if status.get("sealed", True):
        return "Vault is sealed"
    v = str(status.get("version", ""))
    if v.split("+", 1)[0] != want_version:
        return f"Vault reports version {v!r}, expected {want_version!r}"
    return None


def snapshot_name(dataset: str, now: dt.datetime, old: Image, new: Image) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    clean = lambda t: re.sub(r"[^A-Za-z0-9._-]", "_", t)  # noqa: E731
    return f"{dataset}@oob-{stamp}-{clean(old.tag)}-to-{clean(new.tag)}"


def snapshots_to_prune(names: List[str], keep: int) -> List[str]:
    """Our snapshots (`@oob-`), oldest first as listed; all but the newest `keep`."""
    ours = [n for n in names if "@oob-" in n]
    return ours[:-keep] if keep > 0 else ours


def merge_info(message: str) -> Tuple[bool, Optional[int]]:
    """(auto-merged by the oob class, MR iid) from a merge commit message.
    "Automerge-Class: gitlab-oob" is the class's name before Vault joined it."""
    m = MR_RE.search(message)
    lines = message.splitlines()
    return (CLASS_TRAILER in lines or "Automerge-Class: gitlab-oob" in lines, int(m[1]) if m else None)


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


def decide(p: Profile, *, desired: Image, pinned: Image, running: Optional[str], hold: Optional[dict],
           auto: bool) -> Decision:
    if running != pinned.ref:
        return Decision("drift", f"running container image {running!r} is not the pinned {pinned.ref}")
    if desired == pinned:
        return Decision("noop", "main matches the pinned image")
    if hold and hold.get("image") == desired.ref:
        return Decision("hold", f"{desired.ref} failed on {hold.get('at')}: {hold.get('reason')}")
    kind = classify(p, pinned, desired)
    if kind in ("edition", "downgrade", "unknown"):
        return Decision("refuse", f"{kind} change {pinned.tag} -> {desired.tag} is never done automatically", kind)
    if auto and kind not in ("digest", "patch"):
        return Decision("refuse", f"auto-merged change is a {kind} update; only digest and patch "
                                  "updates may come through the oob class", kind)
    return Decision("upgrade", f"{kind} update {pinned.tag} -> {desired.tag}", kind)


# --------------------------------------------------------------------------
# Side effects
# --------------------------------------------------------------------------

def log(event: str, **kv) -> None:
    parts = " ".join(f"{k}={json.dumps(v) if isinstance(v, str) and (' ' in v or not v) else v}"
                     for k, v in kv.items())
    print(f"oob event={event} service={SERVICE} {parts}".rstrip(), flush=True)


def run(cmd: List[str], *, timeout: int = 600, check: bool = True, input: Optional[str] = None,
        cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input, cwd=cwd)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}... exited {res.returncode}: {res.stderr.strip()[:400]}")
    return res


class Host:
    """Everything that touches the router, the service, GitLab or ZFS."""

    def __init__(self, cfg: dict, dry_run: bool):
        self.cfg = cfg
        self.dry = dry_run
        self.state = cfg["state_dir"]
        self.profile = PROFILES[cfg.get("service", "gitlab")]

    # ---- state files
    def pin_path(self) -> str:
        return os.path.join(self.state, "image")

    def read_pin(self) -> Image:
        with open(self.pin_path()) as f:
            return parse_image(self.profile, f.read().strip())

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

    def data_dir(self) -> str:
        """`data_dir` from the config, or the host source of the container
        mount at `data_mount` (a named volume, e.g. Vault's file storage)."""
        if self.cfg.get("data_dir"):
            return self.cfg["data_dir"]
        res = run(["docker", "inspect", "-f", "{{json .Mounts}}", self.cfg["container"]])
        for m in json.loads(res.stdout):
            if m.get("Destination") == self.cfg["data_mount"] and m.get("Source"):
                return m["Source"]
        raise RuntimeError(f"{self.cfg['container']} has no mount at {self.cfg['data_mount']}")

    def compose(self, *args: str, timeout: int = 900) -> None:
        cmd = ["docker", "compose", "-p", self.cfg["compose_project"],
               "--project-directory", self.cfg["compose_dir"]]
        for f in self.cfg["compose_files"]:
            cmd += ["-f", os.path.join(self.cfg["compose_dir"], f)]
        run(cmd + list(args), timeout=timeout)

    def stop(self) -> None:
        t = int(self.cfg.get("stop_timeout", self.profile.stop_timeout))
        run(["docker", "stop", "-t", str(t), self.cfg["container"]], timeout=t + 120)

    # ---- GitLab API (through glab, as root)
    def api(self, path: str, method: str = "GET", fields: Optional[Dict[str, str]] = None,
            timeout: int = 60) -> object:
        cmd = ["glab", "api", "-X", method, path]
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        res = run(cmd, timeout=timeout, cwd=self.cfg["repo"])
        return json.loads(res.stdout) if res.stdout.strip() else None

    # ---- GitLab checks
    def gitlab_readiness(self) -> bool:
        res = run(["docker", "exec", self.cfg["container"], "curl", "-fsS", "-m", "20",
                   "http://localhost/-/readiness?all=1"], check=False, timeout=60)
        if res.returncode != 0:
            return False
        try:
            return json.loads(res.stdout).get("status") == "ok"
        except ValueError:
            return False

    def gitlab_migrations(self) -> List[dict]:
        rows: List[dict] = []
        for db in ("main", "ci"):
            rows += self.api(f"admin/batched_background_migrations?database={db}&per_page=100") or []
        return rows

    def gitlab_version(self) -> str:
        return (self.api("version") or {}).get("version", "")

    def online_runners(self) -> int:
        return len(self.api("runners/all?status=online&per_page=100") or [])

    def ls_remote(self) -> bool:
        return run(["git", "-C", self.cfg["repo"], "ls-remote", "origin", "HEAD"],
                   check=False, timeout=60).returncode == 0

    def running_jobs(self) -> int:
        return len(self.api(f"projects/{self.cfg['project_id']}/jobs?scope[]=running&per_page=100") or [])

    # ---- Vault checks (no secret is printed or logged)
    def vault_status(self) -> dict:
        res = run(["docker", "exec", self.cfg["container"], "vault", "status", "-format=json"],
                  check=False, timeout=60)
        # exit 0 = unsealed, 2 = sealed: both print the status document
        if res.returncode not in (0, 2):
            raise RuntimeError(f"vault status exited {res.returncode}: {res.stderr.strip()[:200]}")
        return json.loads(res.stdout)

    def vault_canary(self) -> Optional[str]:
        """None when an AppRole login (the router helper's credential, as CI
        deploys log in) and a KV read of `canary_path` both work; else why
        not. Only the status is looked at; the token and the data are
        dropped unread."""
        res = run([self.cfg["vault_token_helper"]], check=False, timeout=60)
        token = res.stdout.strip()
        if res.returncode != 0 or not token:
            return f"AppRole login failed (exit {res.returncode})"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE       # same as the helper: the address is an IP
        req = urllib.request.Request(f"{self.cfg['vault_addr']}/v1/{self.cfg['canary_path']}",
                                     headers={"X-Vault-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
                body = json.load(r)
                ok = r.status == 200 and isinstance(body.get("data"), dict)
        except Exception as e:  # noqa: BLE001
            return f"KV read failed: {type(e).__name__}"
        finally:
            token = ""
        return None if ok else "KV read returned no data"

    # ---- ZFS + rsync
    def mirror_dir(self) -> str:
        return os.path.join(self.cfg["mirror_mount"], self.cfg["service"])

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
        return os.path.join(self.cfg["mirror_mount"], ".zfs", "snapshot", name.split("@", 1)[1],
                            self.cfg["service"])

    # ---- notification
    def mail(self, subject: str, body: str) -> None:
        res = run(["mail", "-s", subject, "root"], input=body, check=False, timeout=60)
        log("mailed" if res.returncode == 0 else "mail_failed", subject=subject)


# --------------------------------------------------------------------------
# Service checks
# --------------------------------------------------------------------------

def health_once(h: Host, image: Image, ctx: dict) -> Optional[str]:
    """None when the service is healthy at `image`; else the first problem."""
    svc = h.cfg["service"]
    health = h.container_health()
    if health != "healthy":
        return f"container {health}"
    if svc == "gitlab":
        if not h.gitlab_readiness():
            return "readiness?all=1 not ok"
        want = gitlab_api_version(image.tag)
        if (v := h.gitlab_version()) != want:
            return f"API reports {v!r}, expected {want!r}"
        if not h.ls_remote():
            return "git ls-remote failed"
        if (n := h.online_runners()) < ctx.get("runners", 0):
            return f"{n} runners online, {ctx.get('runners')} before"
        return None
    if svc == "vault":
        if (prob := vault_status_problem(h.vault_status(), image.tag)) is not None:
            return prob
        return h.vault_canary()
    raise ValueError(f"no health checks for {svc}")


def health_gate(h: Host, image: Image, ctx: dict, timeout: int) -> Tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = "not checked yet"
    while time.monotonic() < deadline:
        try:
            prob = health_once(h, image, ctx)
            if prob is None:
                return True, "healthy"
            last = prob
        except Exception as e:  # noqa: BLE001 - keep polling until the deadline
            last = f"check error: {e}"
        time.sleep(int(h.cfg.get("health_poll", 20)))
    return False, last


def preflight(h: Host, pinned: Image) -> Tuple[dict, List[str]]:
    """(context for the health gate, problems that make the run wait)."""
    svc = h.cfg["service"]
    problems: List[str] = []
    ctx: dict = {}
    try:
        prob = health_once(h, pinned, {"runners": 0})
        if prob:
            problems.append(f"{svc} is not healthy now: {prob}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not check {svc} health: {e}")
    if svc == "gitlab":
        try:
            blocked = blocking_migrations(h.gitlab_migrations())
            if blocked:
                problems.append("background migrations not finished: " + ", ".join(blocked[:5]))
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not read background migrations: {e}")
        try:
            ctx["runners"] = h.online_runners()
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not count runners: {e}")
    if svc == "vault":
        # every deploy job reads Vault first: don't stop it under a running job
        try:
            if (n := h.running_jobs()):
                problems.append(f"{n} CI job(s) running")
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not list running jobs: {e}")
    try:
        need = int(h.used_bytes(h.data_dir()) * 1.2)
        have = h.free_bytes(os.path.dirname(h.cfg["mirror_mount"].rstrip("/")) or "/")
        if have < need:
            problems.append(f"not enough space for the restore point: {have >> 30} GiB free, {need >> 30} GiB needed")
    except Exception as e:  # noqa: BLE001
        problems.append(f"could not check space: {e}")
    return ctx, problems


# --------------------------------------------------------------------------
# The upgrade
# --------------------------------------------------------------------------

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
                f["remove_labels"] = PENDING_LABEL + ",automerge-gitlab-pending"
            if add_labels:
                f["add_labels"] = add_labels
            h.api(f"projects/{pid}/merge_requests/{iid}", "PUT", f)
    except Exception as e:  # noqa: BLE001
        log("gitlab_note_failed", mr=iid, error=str(e)[:200])


def open_rollback(h: Host, iid: Optional[int], old: Image, new: Image, why: str) -> None:
    """Rollback issue (holds further oob merges of the service) and a revert MR on main."""
    pid = h.cfg["project_id"]
    path = h.cfg["compose_repo_path"]
    svc = h.cfg["service"]
    try:
        h.fetch()
        text = h.main_compose()
        branch = f"automerge-revert/{svc}-{new.tag}-{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M}"
        if text.count(new.ref) == 1:
            h.api(f"projects/{pid}/repository/commits", "POST", {
                "branch": branch, "start_branch": "main",
                "commit_message": f"Revert {svc} to {old.tag}",
                "actions[][action]": "update", "actions[][file_path]": path,
                "actions[][content]": text.replace(new.ref, old.ref)})
            mr = h.api(f"projects/{pid}/merge_requests", "POST", {
                "source_branch": branch, "target_branch": "main", "remove_source_branch": "true",
                "title": f"Revert {svc} to {old.tag}",
                "labels": ROLLBACK_LABEL,
                "description": f"The out-of-band upgrade to {new.tag} failed and was rolled back "
                               f"({why}). Merge this to put main back on the running image."})
            log("revert_mr", mr=(mr or {}).get("iid"))
        issue = h.api(f"projects/{pid}/issues", "POST", {
            "title": f"Auto-merge rollback: {svc} {new.tag}" + (f" (!{iid})" if iid else ""),
            "labels": ROLLBACK_LABEL,
            "description": (f"The out-of-band upgrade of {svc} {old.tag} -> {new.tag} failed its health "
                            f"gate ({why}). The restore point was put back and {old.tag} runs again.\n\n"
                            f"While this issue is open, no {svc} update is auto-merged. Merge or close "
                            "the revert MR, then close this issue. The host keeps a hold on the failed "
                            "image in the agent's hold file until main names a different one.\n\n"
                            f"<!-- automerge-rollback job={HOLD_JOB} services={svc} -->")})
        log("rollback_issue", issue=(issue or {}).get("iid"))
    except Exception as e:  # noqa: BLE001
        log("rollback_gitlab_failed", error=str(e)[:300])
        h.mail(f"oob-upgrade {svc}: rollback done, but the GitLab issue/revert MR failed",
               f"Rollback of {svc} {new.tag} succeeded ({old.tag} runs again), but opening the "
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
    """Swap the service's image line in the rendered compose file, atomically."""
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


def upgrade(h: Host, old: Image, new: Image, kind: str, iid: Optional[int], ctx: dict) -> int:
    cfg = h.cfg
    svc = cfg["service"]
    data = h.data_dir()
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
        h.mail(f"oob-upgrade {svc}: restore point failed, upgrade not attempted",
               f"Taking the restore point for {svc} {old.tag} -> {new.tag} failed: {e}\n"
               f"{old.tag} was started again unchanged.")
        h.write_status(state="restore_point_failed", old=old.ref, new=new.ref, error=str(e)[:300])
        return 1
    log("restore_point", snapshot=snap, offline_seconds=int(time.monotonic() - down))

    # From here on the service is stopped: any failure goes through the
    # rollback, which puts the restore point and the old image back.
    try:
        rewrite_rendered(h, old, new)
        h.write_pin(new)
        h.write_status(state="upgrading", old=old.ref, new=new.ref, snapshot=snap)
        log("start", image=new.ref)
        h.compose("up", "-d", "--no-deps", cfg["container"])
        ok, why = health_gate(h, new, ctx, int(cfg["health_timeout"]))
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"start failed: {e}"
    if ok:
        log("upgraded", old=old.tag, new=new.tag, kind=kind,
            total_downtime_seconds=int(time.monotonic() - down))
        h.write_status(state="done", old=old.ref, new=new.ref, snapshot=snap)
        for s in snapshots_to_prune(h.snapshots(), int(cfg["keep"])):
            h.destroy(s)
            log("snapshot_pruned", snapshot=s)
        report_gitlab(h, iid, f"Out-of-band upgrade done: {svc} {old.tag} -> {new.tag} ({kind}), "
                              f"health gate passed. Restore point `{snap}` kept.", remove_pending=True)
        return 0
    return rollback(h, old, new, snap, why, iid, ctx, data)


def rollback(h: Host, old: Image, new: Image, snap: str, why: str, iid: Optional[int],
             ctx: dict, data: str) -> int:
    cfg = h.cfg
    svc = cfg["service"]
    log("rollback_start", reason=why, snapshot=snap)
    h.write_hold(new, why)
    restored = False
    try:
        h.stop()
        h.rsync(h.snapshot_dir(snap), data)
        if rendered_file(h, new):
            rewrite_rendered(h, new, old)
        h.write_pin(old)
        h.compose("up", "-d", "--no-deps", cfg["container"])
        after_ctx = dict(ctx, runners=max(0, ctx.get("runners", 0) - 1))
        restored, after = health_gate(h, old, after_ctx, int(cfg["health_timeout"]))
    except Exception as e:  # noqa: BLE001
        after = f"rollback error: {e}"
    if not restored:
        log("rollback_failed", reason=after)
        h.write_status(state="rollback_failed", old=old.ref, new=new.ref, snapshot=snap, error=after)
        h.mail(f"oob-upgrade {svc}: ROLLBACK FAILED, {svc} needs hands",
               f"Upgrade {svc} {old.tag} -> {new.tag} failed ({why}); the rollback did not pass its "
               f"health gate either ({after}).\n\nRestore point: {snap}\n"
               f"Runbook: docs/AUTOMERGE.md, 'Out-of-band updates' > 'Manual restore'.")
        return 2
    log("rolled_back", old=old.tag, new=new.tag, reason=why)
    h.write_status(state="rolled_back", old=old.ref, new=new.ref, snapshot=snap, reason=why)
    h.mail(f"oob-upgrade {svc}: upgrade to {new.tag} rolled back",
           f"The upgrade {svc} {old.tag} -> {new.tag} failed its health gate ({why}).\n"
           f"The restore point {snap} was put back and {old.tag} is healthy again.\n"
           f"The host holds {new.ref} and will not retry it. A rollback issue and revert MR follow.")
    report_gitlab(h, iid, f"Out-of-band upgrade to {new.tag} failed its health gate ({why}) and was "
                          f"rolled back to {old.tag} with data restored from `{snap}`.",
                  remove_pending=True, add_labels=ROLLBACK_LABEL)
    open_rollback(h, iid, old, new, why)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    global SERVICE
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service", required=True, choices=sorted(PROFILES))
    p.add_argument("--config", help=f"default {CONFIG_DIR}/<service>.json")
    p.add_argument("--dry-run", action="store_true", help="report the decision and checks, change nothing")
    p.add_argument("--warm", action="store_true", help="only refresh the mirror (no stop, no snapshot)")
    a = p.parse_args(argv)
    SERVICE = a.service
    with open(a.config or os.path.join(CONFIG_DIR, f"{a.service}.json")) as f:
        cfg = json.load(f)
    cfg["service"] = a.service
    h = Host(cfg, a.dry_run)
    os.makedirs(cfg["state_dir"], exist_ok=True)
    lock = open(GLOBAL_LOCK if not a.dry_run else os.path.join(cfg["state_dir"], ".dry.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("locked", note="another out-of-band run is in progress")
        return 0

    if a.warm:
        if a.dry_run:
            log("dry_run", would="refresh the mirror", data_dir=h.data_dir())
            return 0
        h.ensure_dataset()
        t0 = time.monotonic()
        h.rsync(h.data_dir(), h.mirror_dir())
        log("warm_sync_done", seconds=int(time.monotonic() - t0))
        return 0

    prof = h.profile
    try:
        h.fetch()
        desired = desired_from_compose(prof, h.main_compose())
        pinned = h.read_pin()
    except Exception as e:  # noqa: BLE001
        log("error", stage="read", error=str(e)[:300])
        return 1
    auto, iid = merge_info(h.merge_message(desired)) if desired != pinned else (False, None)
    d = decide(prof, desired=desired, pinned=pinned, running=h.running_image(), hold=h.read_hold(), auto=auto)
    log("decision", action=d.action, reason=d.reason, desired=desired.tag, pinned=pinned.tag,
        auto=auto, mr=iid or "")
    if d.action in ("noop", "hold"):
        if a.dry_run:       # still run the health checks, so a dry run proves they work
            try:
                prob = health_once(h, pinned, {"runners": 0})
            except Exception as e:  # noqa: BLE001
                prob = f"check error: {e}"
            log("health_check", result="healthy" if prob is None else prob)
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
                h.mail(f"oob-upgrade {a.service}: {d.action}", d.reason + "\n\nNothing was changed.")
            h.write_status(state=d.action, reason=d.reason)
        return 1

    ctx, problems = preflight(h, pinned)
    log("preflight", problems=len(problems), **{k: v for k, v in ctx.items()})
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
        return upgrade(h, pinned, desired, d.kind, iid, ctx)
    except Exception as e:  # noqa: BLE001
        log("error", stage="upgrade", error=str(e)[:300])
        h.mail(f"oob-upgrade {a.service}: upgrade aborted",
               f"Upgrade {a.service} {pinned.tag} -> {desired.tag} aborted: {e}\n"
               "Check `docker ps`, the pin file and the status file; see the runbook.")
        h.write_status(state="error", reason=str(e)[:300])
        return 1


if __name__ == "__main__":
    sys.exit(main())
