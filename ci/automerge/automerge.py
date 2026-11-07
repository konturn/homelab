#!/usr/bin/env python3
"""Auto-merge for image updates: everything but majors and the denylist. See docs/AUTOMERGE.md.

Subcommands
  mr-check      Informational check inside an MR pipeline (local git only, no token).
  evaluate      Read-only evaluation of one MR via the API (dry run; any state).
  sweep         Scheduled on main: label eligible MRs (and update MRs the policy
                leaves to a human `automerge-manual`), merge at most one ready MR.
                AUTOMERGE_DRY_RUN=1 dry-runs the general class only (fast track
                stays live); AUTOMERGE_DRY_RUN=all or --dry-run dry-runs both.
  plan-deploy   main pipeline: verify an auto-merged commit, write the deploy
                plan for one host (--host <deploy job prefix>).
  deploy-report main pipeline: record the deploy outcome (note / issue / revert MR / mail).
  notify        Mail root from a CI job (used when a deploy fails before deploy-report).
  daily-report  Scheduled on main: mail a summary of the last 24h.

Code in this file runs from `main` for every command that holds credentials;
MR content is only ever read as data through the API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402

try:
    import yaml  # PyYAML ships with ansible
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(os.environ.get("CI_PROJECT_DIR") or Path(__file__).resolve().parents[2])
POLICY_PATH = REPO_ROOT / "ci" / "automerge-policy.yml"
INVENTORY_PATH = REPO_ROOT / "ansible" / "inventory.yml"
FREEZE_FILE = REPO_ROOT / "ci" / "automerge-freeze"
CODEOWNERS_PATH = REPO_ROOT / "CODEOWNERS"

LABEL_ELIGIBLE = "automerge-eligible"
LABEL_MANUAL = "automerge-manual"
LABEL_RENOVATE = "renovate"
LABEL_MERGED = "automerged"
LABEL_ROLLBACK = "automerge-rollback"
LABEL_SECURITY = "security"
LABEL_FAST_TRACK = "automerge-fast-track"
TRAILER = "Automerge-Policy: image-update"
CLASS_TRAILER = "Automerge-Class: fast-track"
# Selects the per-service deploy jobs: "Automerge-Deploy: [router] [zwave]".
# plan-deploy re-derives the hosts from main's policy and refuses a mismatch.
DEPLOY_TRAILER = "Automerge-Deploy:"

API = os.environ.get("CI_API_V4_URL", "https://gitlab.lab.nkontur.com/api/v4").rstrip("/")
PROJECT = os.environ.get("CI_PROJECT_ID", "4")
PROJECT_PATH = os.environ.get("CI_PROJECT_PATH", "root/homelab")
ROUTER_HOST = os.environ.get("AUTOMERGE_ROUTER_HOST", "10.4.0.1")
SSH_KEY = os.environ.get("AUTOMERGE_SSH_KEY", str(REPO_ROOT / "tmp"))
# Pinned host keys, checked strictly (docs/DEPLOY.md, "SSH host keys").
KNOWN_HOSTS = str(REPO_ROOT / "ansible" / "known_hosts")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def dry_modes(cli_dry_run: bool = False) -> Tuple[bool, bool]:
    """(general class dry, fast-track class dry).

    AUTOMERGE_DRY_RUN=1 (or any other true value) dry-runs the general class
    only; the fast-track class stays live. AUTOMERGE_DRY_RUN=all, or --dry-run,
    dry-runs both."""
    env = (os.environ.get("AUTOMERGE_DRY_RUN") or "").strip().lower()
    if cli_dry_run or env == "all":
        return True, True
    return truthy(env), False


def load_allowlist() -> dict:
    """The auto-merge policy file (ci/automerge-policy.yml)."""
    if yaml is None:
        raise SystemExit("PyYAML is required")
    with open(POLICY_PATH) as f:
        data = yaml.safe_load(f) or {}
    data["services"] = {k: (v or {}) for k, v in (data.get("services") or {}).items()}
    return data


def load_deployments(allow: dict) -> Dict[str, List[policy.Deployment]]:
    """Compose file -> hosts, from this checkout's ansible/inventory.yml."""
    with open(INVENTORY_PATH) as f:
        return policy.compose_deployments(yaml.safe_load(f) or {}, allow)


def read_codeowners() -> str:
    try:
        return CODEOWNERS_PATH.read_text()
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# GitLab API
# ---------------------------------------------------------------------------

class GitLab:
    def __init__(self, token: Optional[str], dry_run: bool = False):
        self.token = token
        self.dry_run = dry_run

    def _req(self, method: str, path: str, params: Optional[dict] = None,
             body: Optional[dict] = None, raw: bool = False) -> Any:
        url = f"{API}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.token:
            req.add_header("PRIVATE-TOKEN", self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        # The CI image resolves through several nameservers in parallel (musl),
        # some of which do not know internal names: retry transient lookup and
        # connection errors. HTTP errors are real answers and are not retried;
        # writes are retried only if the request never reached the server.
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = resp.read()
                    if raw:
                        return payload.decode()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
            except (urllib.error.URLError, OSError) as e:
                if attempt == 5 or (method != "GET" and not _is_lookup_error(e)):
                    raise
                time.sleep(1 + attempt)

    def get(self, path: str, **params: Any) -> Any:
        return self._req("GET", path, params=params)

    def get_all(self, path: str, **params: Any) -> List[Any]:
        out: List[Any] = []
        page = 1
        while True:
            batch = self._req("GET", path, params={**params, "per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def write(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        if self.dry_run:
            log(f"  [dry-run] would {method} {path} {json.dumps(body) if body else ''}")
            return None
        return self._req(method, path, body=body)

    def file_at(self, path: str, ref: str) -> Optional[str]:
        enc = urllib.parse.quote(path, safe="")
        try:
            return self._req("GET", f"projects/{PROJECT}/repository/files/{enc}/raw",
                             params={"ref": ref}, raw=True)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise

    def ensure_labels(self) -> None:
        wanted = {
            LABEL_ELIGIBLE: ("#2da160", "Passes the auto-merge policy (set by CI)"),
            LABEL_MERGED: ("#1f75cb", "Merged by the auto-merge policy"),
            LABEL_ROLLBACK: ("#dd2b0e", "Auto-merged deploy failed health checks and was rolled back"),
            LABEL_FAST_TRACK: ("#6699cc", "Merged under the fast-track class (docs/AUTOMERGE.md)"),
            LABEL_MANUAL: ("#e9be74", "Update the auto-merge policy leaves to a human: major, denylist "
                                      "(e.g. GitLab) or restricted kind. Reason in the MR notes"),
            "freeze": ("#808080", "Block auto-merge for this MR"),
        }
        existing = {l["name"] for l in self.get_all(f"projects/{PROJECT}/labels")}
        for name, (color, desc) in wanted.items():
            if name not in existing:
                self.write("POST", f"projects/{PROJECT}/labels",
                           {"name": name, "color": color, "description": desc})


def _is_lookup_error(e: Exception) -> bool:
    """True if the request never reached the server (safe to retry a write)."""
    reason = getattr(e, "reason", e)
    return isinstance(reason, (socket.gaierror, ConnectionRefusedError))


def gitlab_from_env(dry_run: bool) -> GitLab:
    token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        raise SystemExit("AUTOMERGE_TOKEN is not set (protected CI variable; see docs/AUTOMERGE.md)")
    return GitLab(token, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Global gates
# ---------------------------------------------------------------------------

def global_gates(gl: GitLab, allow: dict) -> List[str]:
    """Return the reasons that block every auto-merge right now. Empty = open."""
    reasons: List[str] = []
    if truthy(os.environ.get("AUTOMERGE_FREEZE")):
        reasons.append("AUTOMERGE_FREEZE variable is set")
    if FREEZE_FILE.exists():
        reasons.append(f"{FREEZE_FILE.relative_to(REPO_ROOT)} exists on main")
    busy = deploys_in_progress(gl)
    if busy:
        reasons.append("per-service deploy still running: " + ", ".join(busy))
    return reasons


# Machine-readable scope of a rollback issue, written by deploy-report.
ROLLBACK_MARKER = re.compile(r"<!--\s*automerge-rollback\s+job=(\S+)\s+services=(\S+)\s*-->")
# Issues opened before the marker existed: "Auto-merge rollback: a, b on router (!123)".
ROLLBACK_TITLE = re.compile(r"^Auto-merge rollback: (.+) on (\S+) \(!\d+\)$")


def rollback_scope(issue: dict) -> Optional[Set[Tuple[str, str]]]:
    """The (deploy job, service) pairs an open rollback issue holds back, or
    None when the issue does not say (the hold then applies to every MR)."""
    m = ROLLBACK_MARKER.search(issue.get("description") or "")
    if m:
        job, services = m.group(1), m.group(2).split(",")
    else:
        t = ROLLBACK_TITLE.match((issue.get("title") or "").strip())
        if not t:
            return None
        job, services = t.group(2), [s.strip() for s in t.group(1).split(",")]
    scope = {(job, s) for s in services if s}
    return scope or None


def rollback_holds(gl: GitLab) -> List[Tuple[dict, Optional[Set[Tuple[str, str]]]]]:
    issues = gl.get_all(f"projects/{PROJECT}/issues", state="opened", labels=LABEL_ROLLBACK)
    return [(i, rollback_scope(i)) for i in issues]


def rollback_gate(holds: List[Tuple[dict, Optional[Set[Tuple[str, str]]]]],
                  v: policy.Verdict) -> Optional[str]:
    """An open rollback issue holds back only MRs that update one of its
    services on the same host. Other services keep auto-merging: their
    per-service deploys recreate only their own containers."""
    mine = {(j, c.service) for j in v.deploy_jobs for c in v.changes}
    hits = [f"#{i['iid']}" for i, scope in holds if scope is None or scope & mine]
    if hits:
        return "open rollback issue(s) for this service: " + ", ".join(hits)
    return None


def deploys_in_progress(gl: GitLab) -> List[str]:
    """Per-service deploy jobs that have not finished. The next merge waits
    for them, so a failing deploy pauses auto-merge (rollback issue) before
    another update lands on top of it."""
    jobs = gl.get(f"projects/{PROJECT}/jobs",
                  **{"scope[]": ["created", "pending", "running", "waiting_for_resource"]}, per_page=100)
    return [f"{j['name']} (job {j['id']})" for j in jobs or []
            if str(j.get("name", "")).endswith(":deploy-service")]


def host_unreachable(address: str, port: int = 22, timeout: float = 5.0) -> Optional[str]:
    """None if the host accepts a TCP connection on `port` (its deploy uses SSH)."""
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return None
    except OSError as e:
        return f"{address}:{port} unreachable ({e.__class__.__name__})"


def rate_gate(recent: List[dict], allow: dict, fast_track: bool) -> Optional[str]:
    """Each class has its own rolling-24h budget: `fast_track.max_per_24h` for
    fast-track merges, `max_per_24h` for all others."""
    ft = [m for m in recent if LABEL_FAST_TRACK in (m.get("labels") or [])]
    if fast_track:
        n, limit, what = len(ft), policy.fast_track_config(allow)["max_per_24h"], "fast-track auto-merges"
    else:
        n, limit, what = len(recent) - len(ft), int(allow.get("max_per_24h", 3)), "auto-merges"
    if n >= limit:
        return f"rate limit: {n} {what} in the last 24h (max {limit})"
    return None


def recent_automerges(gl: GitLab, hours: int = 24) -> List[dict]:
    since = now() - dt.timedelta(hours=hours)
    mrs = gl.get_all(f"projects/{PROJECT}/merge_requests", state="merged", labels=LABEL_MERGED,
                     updated_after=since.isoformat())
    return [m for m in mrs if (parse_ts(m.get("merged_at")) or since) >= since]


# ---------------------------------------------------------------------------
# MR evaluation via API
# ---------------------------------------------------------------------------

def mr_file_changes(gl: GitLab, iid: int) -> List[policy.FileChange]:
    diffs = gl.get_all(f"projects/{PROJECT}/merge_requests/{iid}/diffs")
    out = []
    for d in diffs:
        a, b = d.get("a_mode"), d.get("b_mode")
        out.append(policy.FileChange(
            path=d["new_path"], old_path=d.get("old_path"),
            new_file=bool(d.get("new_file")), deleted_file=bool(d.get("deleted_file")),
            renamed_file=bool(d.get("renamed_file")),
            mode_changed=bool(a and b and a != b and a != "0" and b != "0"),
        ))
    return out


def evaluate_api(gl: GitLab, mr: dict, allow: dict, merger: Optional[str] = None,
                 deployments: Optional[Dict[str, List[policy.Deployment]]] = None) -> policy.Verdict:
    refs = mr.get("diff_refs") or {}
    base_sha, head_sha = refs.get("base_sha"), refs.get("head_sha")
    files = mr_file_changes(gl, mr["iid"])
    composes: Dict[str, policy.ComposePair] = {}
    if base_sha and head_sha:
        for path in sorted({f.path for f in files} & set(policy.compose_files(allow))):
            composes[path] = (gl.file_at(path, base_sha), gl.file_at(path, head_sha))
    return policy.evaluate_mr(
        author=mr["author"]["username"], target_branch=mr["target_branch"],
        labels=mr.get("labels") or [], files=files, composes=composes,
        deployments=deployments if deployments is not None else load_deployments(allow),
        allowlist=allow, codeowners_text=read_codeowners(), merger=merger)


def token_user(gl: GitLab) -> str:
    """Username of the identity behind the token (the merger)."""
    return gl.get("user")["username"]


# Merge status GitLab has not computed yet. Every merge to main resets the
# other open MRs to "unchecked", and GitLab recomputes only when asked.
PENDING_MERGE_STATUS = ("unchecked", "checking", "preparing", "approvals_syncing")


def fetch_mr(gl: GitLab, iid: int, wait: float = 30.0) -> dict:
    """The MR's full record with a current merge status: ask GitLab to recheck
    (with_merge_status_recheck) and poll briefly while it does."""
    deadline = time.monotonic() + wait
    while True:
        mr = gl.get(f"projects/{PROJECT}/merge_requests/{iid}", with_merge_status_recheck="true")
        if mr.get("detailed_merge_status") not in PENDING_MERGE_STATUS or time.monotonic() >= deadline:
            return mr
        time.sleep(3)


def readiness(mr: dict) -> List[str]:
    """Reasons an eligible MR cannot be merged yet."""
    r = []
    if mr.get("state") != "opened":
        r.append(f"state is {mr.get('state')}")
    if mr.get("draft") or mr.get("work_in_progress"):
        r.append("MR is a draft")
    if mr.get("has_conflicts"):
        r.append("MR has conflicts")
    pipe = mr.get("head_pipeline") or {}
    if pipe.get("status") != "success":
        r.append(f"MR pipeline is {pipe.get('status') or 'missing'}")
    elif pipe.get("sha") != mr.get("sha"):
        r.append("MR pipeline is not for the current head commit")
    if mr.get("diff_refs", {}).get("head_sha") != mr.get("sha"):
        r.append("diff refs are stale")
    status = mr.get("detailed_merge_status")
    if status not in ("mergeable", "not_approved"):
        r.append(f"merge status is {status}")
    return r


def print_verdict(mr: dict, v: policy.Verdict, extra: List[str] = ()) -> None:
    log(f"!{mr['iid']} {mr['title'][:100]}")
    log(f"  author={mr['author']['username']} state={mr['state']} {mr.get('web_url', '')}")
    for n in v.notes:
        log(f"  + {n}")
    for r in list(v.reasons) + list(extra):
        log(f"  - {r}")
    log(f"  => {'ELIGIBLE' if v.eligible and not extra else 'not eligible'}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def git(*args: str) -> str:
    # The CI image runs as a non-root user while the runner checks the repo out
    # as root; without safe.directory git refuses with "dubious ownership".
    return subprocess.run(["git", "-c", f"safe.directory={REPO_ROOT}", "-C", str(REPO_ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def git_show(ref: str, path: str) -> Optional[str]:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def cmd_mr_check(args: argparse.Namespace) -> int:
    """MR pipeline (untrusted ref, no credentials): informational only."""
    allow = load_allowlist()
    base, head = args.base, args.head
    names = git("diff", "--name-status", "--no-renames", base, head).splitlines()
    files = []
    for line in names:
        status, _, path = line.partition("\t")
        files.append(policy.FileChange(path=path, new_file=status == "A", deleted_file=status == "D",
                                       mode_changed=status == "T"))
    listed = set(policy.compose_files(allow))
    composes = {f.path: (git_show(base, f.path), git_show(head, f.path)) for f in files if f.path in listed}
    v = policy.evaluate_mr(
        author=args.author or "", target_branch=args.target or "",
        labels=[l for l in (args.labels or "").split(",") if l], files=files,
        composes=composes, deployments=load_deployments(allow),
        allowlist=allow, codeowners_text=read_codeowners())
    for n in v.notes:
        log(f"  + {n}")
    for r in v.reasons:
        log(f"  - {r}")
    log("Policy (MR-local checks): " + ("PASS" if v.eligible else "not eligible"))
    log("Note: this job is informational. The authoritative decision, including freeze,")
    log("rate-limit and host checks, is made by the scheduled automerge:sweep job on main.")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    gl = gitlab_from_env(dry_run=True)
    allow = load_allowlist()
    deployments = load_deployments(allow)
    merger = token_user(gl)
    for iid in args.iids:
        mr = gl.get(f"projects/{PROJECT}/merge_requests/{iid}")
        v = evaluate_api(gl, mr, allow, merger=merger, deployments=deployments)
        if args.json:
            log(json.dumps({"iid": iid, "title": mr["title"], **v.as_dict()}))
        else:
            print_verdict(mr, v)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    dry, ft_dry = dry_modes(args.dry_run)
    gl = gitlab_from_env(dry_run=dry)            # writes for the general class
    gl_ft = GitLab(gl.token, dry_run=ft_dry)     # writes for fast-track MRs
    allow = load_allowlist()
    deployments = load_deployments(allow)
    (gl_ft if dry else gl).ensure_labels()
    merger = token_user(gl)
    log(f"Merging identity: {merger}")
    log(f"General class: {'DRY RUN (no labels, approvals or merges)' if dry else 'live'}; "
        f"fast track: {'DRY RUN' if ft_dry else 'live'}")

    candidates: Dict[int, dict] = {}
    for author in allow.get("authors") or []:
        for mr in gl.get_all(f"projects/{PROJECT}/merge_requests", state="opened",
                             author_username=author, target_branch=allow.get("target_branch", "main")):
            candidates[mr["iid"]] = mr
    for mr in gl.get_all(f"projects/{PROJECT}/merge_requests", state="opened", labels=LABEL_ELIGIBLE):
        candidates.setdefault(mr["iid"], mr)
    if not candidates:
        log("No candidate MRs.")
        return 0

    verdicts = {}
    for iid in sorted(candidates):
        mr = fetch_mr(gl, iid)  # full record (pipeline, merge status)
        candidates[iid] = mr
        verdicts[iid] = evaluate_api(gl, mr, allow, merger=merger, deployments=deployments)

    gates: List[str] = []
    holds: List[Tuple[dict, Optional[Set[Tuple[str, str]]]]] = []
    rate: Dict[bool, Optional[str]] = {False: None, True: None}
    if any(v.eligible for v in verdicts.values()):
        gates = global_gates(gl, allow)
        log("Global gates: " + ("CLOSED" if gates else "open"))
        for g in gates:
            log(f"  - {g}")
        holds = rollback_holds(gl)
        for i, scope in holds:
            held = ", ".join(f"{s} on {j}" for j, s in sorted(scope)) if scope else "ALL MRs (scope unknown)"
            log(f"  - rollback #{i['iid']} holds: {held}")
        recent = recent_automerges(gl)
        rate = {ft: rate_gate(recent, allow, ft) for ft in (False, True)}
        for ft, r in rate.items():
            if r:
                log(f"  - {'fast track' if ft else 'general class'}: {r}")
    log()

    # MRs labelled `security` (e.g. by the running-image CVE scan) go first.
    order = sorted(candidates, key=lambda i: (LABEL_SECURITY not in (candidates[i].get("labels") or []), i))
    merged_one = False
    for iid in order:
        mr, v = candidates[iid], verdicts[iid]
        rb = rollback_gate(holds, v) if v.eligible else None
        mr_gates = gates + [g for g in (rate[v.fast_track], rb) if g]
        eligible = v.eligible and not mr_gates
        print_verdict(mr, v, mr_gates if v.eligible else [])
        if v.eligible:
            log(f"  class: {'fast track' if v.fast_track else 'general'}"
                f"{' (dry run)' if (ft_dry if v.fast_track else dry) else ''}")
        writer = gl_ft if v.fast_track else gl
        has_label = LABEL_ELIGIBLE in (mr.get("labels") or [])
        if eligible and not has_label:
            writer.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": LABEL_ELIGIBLE})
        elif not eligible and has_label:
            # remove a stale label with whichever client is live
            (gl if not dry else gl_ft).write("PUT", f"projects/{PROJECT}/merge_requests/{iid}",
                                             {"remove_labels": LABEL_ELIGIBLE})
        mark_manual(gl, mr, v)
        if not eligible:
            continue
        blockers = readiness(mr)
        for d in v.deploy_hosts:
            if d.job != "router":
                down = host_unreachable(d.address)
                if down:
                    blockers.append(f"{d.host} ({down})")
        if blockers:
            log("  not ready to merge: " + "; ".join(blockers))
            continue
        if merged_one:
            log("  ready, deferred: one auto-merge per run")
            continue
        # A dry-run merge does not use up the run's one real merge.
        merged_one = do_merge(writer, mr, v) and not writer.dry_run
    return 0


def is_update_mr(mr: dict, v: policy.Verdict) -> bool:
    """A dependency-update MR: opened by Renovate, or an image change in a
    compose file. Other MRs by the listed authors (moltbot's config changes)
    are simply not eligible and get no label or note."""
    return LABEL_RENOVATE in (mr.get("labels") or []) or bool(v.changes) or v.major


def mark_manual(gl: GitLab, mr: dict, v: policy.Verdict) -> None:
    """Keep updates the policy leaves to a human visible: label them
    `automerge-manual` and say why once (the dependency dashboard and the
    daily report list them too). Drop the label when the MR qualifies again."""
    iid, labels = mr["iid"], mr.get("labels") or []
    manual = v.manual and is_update_mr(mr, v)
    if manual and LABEL_MANUAL not in labels:
        gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": LABEL_MANUAL})
        body = ["This update is left to a human by the auto-merge policy (docs/AUTOMERGE.md):", ""]
        body += [f"- {r}" for r in v.reasons]
        body += ["", "Review and merge it by hand, or close it. The sweep will not merge it."]
        gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes", {"body": "\n".join(body)})
    elif not manual and LABEL_MANUAL in labels and not v.frozen:
        gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"remove_labels": LABEL_MANUAL})


def deploy_trailer(v: policy.Verdict) -> str:
    return DEPLOY_TRAILER + " " + " ".join(f"[{j}]" for j in v.deploy_jobs)


def do_merge(gl: GitLab, mr: dict, v: policy.Verdict) -> bool:
    iid, sha = mr["iid"], mr["sha"]
    services = ", ".join(c.service for c in v.changes)
    if not v.deploy_jobs:
        log(f"  not merging !{iid}: no per-service deploy target")
        return False
    trailers = (CLASS_TRAILER + "\n" if v.fast_track else "") + deploy_trailer(v) + "\n" + TRAILER
    message = (f"Merge branch '{mr['source_branch']}' into '{mr['target_branch']}'\n\n"
               f"Image update: {services}\n\n"
               f"See merge request {PROJECT_PATH}!{iid}\n\n{trailers}\n")
    labels = LABEL_MERGED + ("," + LABEL_FAST_TRACK if v.fast_track else "")
    if gl.dry_run:
        log(f"  [dry-run] would merge !{iid} at {sha[:12]} ({services})")
    else:
        log(f"  merging !{iid} at {sha[:12]} ({services}){' [fast track]' if v.fast_track else ''}")
    # Label first so the main pipeline can verify provenance as soon as it starts.
    gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": labels})
    approved = False
    try:
        if mr.get("detailed_merge_status") == "not_approved":
            gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/approve", {"sha": sha})
            approved = True
        gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}/merge", {
            "sha": sha, "merge_commit_message": message, "should_remove_source_branch": True})
    except RuntimeError as e:
        log(f"  MERGE FAILED: {e}")
        try:
            if approved:
                gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/unapprove")
            gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"remove_labels": labels})
        except RuntimeError as e2:
            log(f"  could not remove {LABEL_MERGED} label: {e2}")
        return False
    cls = " under the fast-track class" if v.fast_track else ""
    lines = [f"Merged by the auto-merge policy{cls} (pipeline {os.environ.get('CI_PIPELINE_URL', 'n/a')}).", ""]
    lines += [f"- `{c.service}`: {c.kind} `{c.old}` -> `{c.new}`" for c in v.changes]
    lines += ["", "The main pipeline deploys only these services (" +
              ", ".join(f"`{j}:deploy-service`" for j in v.deploy_jobs) +
              "), waits for them to become healthy, and rolls back automatically on failure."]
    gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes", {"body": "\n".join(lines)})
    return True


def cmd_plan_deploy(args: argparse.Namespace) -> int:
    gl = gitlab_from_env(dry_run=False)
    allow = load_allowlist()
    deployments = load_deployments(allow)
    sha = os.environ.get("CI_COMMIT_SHA") or git("rev-parse", "HEAD").strip()

    def abort(msg: str) -> int:
        log(f"ERROR: {msg}")
        log(f"Nothing was deployed. Run the manual {args.host}:deploy job on this pipeline "
            "if a full deploy is wanted.")
        return 1

    parents = git("rev-list", "--parents", "-n", "1", sha).split()
    if len(parents) != 3:
        return abort(f"{sha[:12]} is not a two-parent merge commit")
    first_parent = parents[1]

    mrs = gl.get(f"projects/{PROJECT}/repository/commits/{sha}/merge_requests")
    mr = next((m for m in mrs if m.get("merge_commit_sha") == sha), None)
    if not mr:
        return abort("no merged MR found for this merge commit")
    if LABEL_MERGED not in (mr.get("labels") or []):
        return abort(f"!{mr['iid']} does not carry the {LABEL_MERGED} label")
    if mr["author"]["username"] not in (allow.get("authors") or []):
        return abort(f"!{mr['iid']} author is not on the auto-merge author list")
    merged_by = (mr.get("merge_user") or mr.get("merged_by") or {}).get("username")
    if merged_by != token_user(gl) or merged_by == mr["author"]["username"]:
        return abort(f"!{mr['iid']} was not merged by the auto-merge identity")

    listed = set(policy.compose_files(allow))
    changed = [p for p in git("diff", "--name-only", first_parent, sha).splitlines() if p]
    outside = [p for p in changed if p not in listed]
    if not changed or outside:
        return abort("merge changes files other than the deployed compose files: " + ", ".join(outside or ["none"]))
    changes: List[policy.ImageChange] = []
    head_info: Dict[str, Dict[str, policy.ServiceInfo]] = {}
    base_info: Dict[str, Dict[str, policy.ServiceInfo]] = {}
    for path in changed:
        base, head = git_show(first_parent, path) or "", git_show(sha, path) or ""
        try:
            changes += policy.diff_compose(base, head, path, policy.zero_minor_services(allow))
        except policy.PolicyError as e:
            return abort(f"merged change to {path} is outside the policy: {e}")
        head_info[path], base_info[path] = policy.compose_service_info(head), policy.compose_service_info(base)
    if not changes:
        return abort("no image changes")
    off = policy.change_reasons(changes, allow, head_info, base_info)
    if off:
        return abort("merged change is outside the policy: " + "; ".join(off))
    if LABEL_FAST_TRACK in (mr.get("labels") or []):
        try:
            ok, why = policy.fast_track_verdict(mr["author"]["username"], changes, allow)
        except policy.PolicyError as e:
            ok, why = False, str(e)
        if not ok:
            return abort(f"!{mr['iid']} is labelled {LABEL_FAST_TRACK} but does not qualify: {why}")

    # The hosts this commit deploys to, from main's inventory and policy.
    targets: Dict[str, Tuple[policy.Deployment, List[str]]] = {}
    for path in changed:
        deps = deployments.get(path) or []
        if not deps:
            return abort(f"{path} is not deployed by any host")
        for d in deps:
            if not d.job:
                return abort(f"{path} runs on {d.host}, which has no per-service deploy")
            targets.setdefault(d.job, (d, []))[1].append(path)
    if args.host not in targets:
        return abort(f"this commit does not deploy to {args.host} (targets: {', '.join(sorted(targets))})")
    dep, host_files = targets[args.host]
    host_changes = [c for c in changes if c.file in host_files]

    out = Path(args.out)
    prev_dir = out.parent / "prev"
    prev_dir.mkdir(parents=True, exist_ok=True)
    # Every compose file as of the first parent: the rollback renders the
    # host's whole set (docker_compose_file_names) from this directory.
    for path in git("ls-tree", "--name-only", first_parent, "docker/").splitlines():
        if policy.COMPOSE_GLOB.match(path):
            (prev_dir / Path(path).name).write_text(git_show(first_parent, path) or "")
    timeout_default = int(allow.get("default_health_timeout", 600))
    services = allow["services"]
    try:
        pre_cfg = policy.pre_upgrade_config(allow)
        backups = policy.pre_upgrade_backups(host_changes, allow, now().strftime("%Y%m%dT%H%M%SZ"))
    except policy.PolicyError as e:
        return abort(f"pre_upgrade_backups: {e}")
    plan = {
        "mr_iid": mr["iid"], "mr_url": mr["web_url"], "mr_title": mr["title"],
        "merge_sha": sha, "first_parent": first_parent, "prev_compose_dir": str(prev_dir),
        "host": dep.host, "job": args.host, "project": dep.project,
        "changed_files": changed, "host_files": host_files,
        "services": [{
            "service": c.service, "file": c.file, "kind": c.kind, "old": str(c.old), "new": str(c.new),
            "old_digest": c.old.digest or "", "new_digest": c.new.digest or "",
            "probe": (services.get(c.service) or {}).get("probe") or "",
            "probe_via": (services.get(c.service) or {}).get("probe_via") or "",
            "timeout": int((services.get(c.service) or {}).get("timeout") or timeout_default),
            # pre-upgrade backup (version updates of migrating services only)
            "pre_upgrade_backup": (backups.get(c.service) or {}).get("type", ""),
            "backup_dir": (backups.get(c.service) or {}).get("dir", ""),
        } for c in host_changes],
        "pre_upgrade": {k: pre_cfg[k] for k in ("dir", "keep", "rate_mb", "timeout")},
    }
    out.write_text(json.dumps(plan, indent=2))
    log(f"Deploy plan for !{mr['iid']} ({sha[:12]}) on {dep.host}:")
    for s in plan["services"]:
        log(f"  {s['service']}: {s['old']} -> {s['new']} (probe: {s['probe'] or 'none'}, timeout {s['timeout']}s)")
        if s["pre_upgrade_backup"]:
            log(f"    pre-upgrade backup ({s['pre_upgrade_backup']}) to {s['backup_dir']}")
    return 0


def cmd_deploy_report(args: argparse.Namespace) -> int:
    gl = gitlab_from_env(dry_run=False)
    plan = json.loads(Path(args.plan).read_text())
    result: Dict[str, Any] = {}
    try:
        result = json.loads(Path(args.result).read_text())
    except (FileNotFoundError, ValueError):
        pass
    iid = plan["mr_iid"]
    job = os.environ.get("CI_JOB_URL", "n/a")
    host = plan.get("host") or "router.lab.nkontur.com"
    svc_list = ", ".join(s["service"] for s in plan["services"])
    backups = [s for s in plan["services"] if s.get("pre_upgrade_backup")]
    if args.status == "success":
        body = f"Deployed and healthy on {host}: {svc_list} ({job})."
        if backups:
            body += " Pre-upgrade backup kept on the host: " + ", ".join(
                f"`{s['backup_dir']}`" for s in backups) + "."
        gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes", {"body": body})
        return 0

    gl.ensure_labels()
    rolled_back = result.get("rollback_healthy")
    rb_text = {True: "rolled back to the previous image and healthy again",
               False: "rolled back to the previous image but STILL UNHEALTHY - needs attention",
               None: "rollback status unknown (deploy job did not finish) - check the host"}[rolled_back]
    title = f"Auto-merge rollback: {svc_list} on {host.split('.')[0]} (!{iid})"
    body = [
        f"The per-service deploy of !{iid} ({plan['mr_title']}) on {host} failed its health gate "
        f"and was {rb_text}.",
        "",
        f"- Deploy job: {job}",
        f"- Merge commit: {plan['merge_sha']}",
        f"- Failure: {result.get('failure') or 'see job log'}",
    ] + restore_lines(backups, result) + [
        "",
        "| service | from | to |", "|---|---|---|",
    ] + [f"| {s['service']} | `{s['old']}` | `{s['new']}` |" for s in plan["services"]] + [
        "",
        f"While this issue is open, auto-merge is paused for {svc_list} on {host.split('.')[0]}; other",
        "services keep auto-merging. `main` still references the new image, so the next full deploy of",
        "this host would re-apply it; merge the revert MR or fix forward, then close this issue.",
        "",
        f"<!-- automerge-rollback job={plan.get('job') or host.split('.')[0]} "
        f"services={','.join(s['service'] for s in plan['services'])} -->",
    ]
    existing = [i for i in gl.get(f"projects/{PROJECT}/issues", state="opened", labels=LABEL_ROLLBACK,
                                  search=f"!{iid}", per_page=20) if f"(!{iid})" in i["title"]]
    revert = open_revert_mr(gl, plan)
    if revert:
        body += ["", f"Revert MR: !{revert}"]
    if existing:
        issue = existing[0]
        gl.write("POST", f"projects/{PROJECT}/issues/{issue['iid']}/notes", {"body": "\n".join(body)})
        log(f"Updated rollback issue #{issue['iid']}")
    else:
        issue = gl.write("POST", f"projects/{PROJECT}/issues",
                         {"title": title, "description": "\n".join(body), "labels": LABEL_ROLLBACK})
        log(f"Opened rollback issue #{issue['iid'] if issue else '?'}")
    gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": LABEL_ROLLBACK})
    gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes",
             {"body": f"Deploy failed health checks; {rb_text}. See {job}."})
    issue_ref = f"#{issue['iid']}" if issue else "(see job)"
    mail_root(f"Homelab auto-merge ROLLBACK: {svc_list} on {host.split('.')[0]} (!{iid})",
              "\n".join(body + ["", f"Issue: {issue_ref}", f"MR: {plan['mr_url']}"]) + "\n")
    return 0


def restore_lines(backups: List[dict], result: dict) -> List[str]:
    """Issue lines on the pre-upgrade backups of a failed deploy. `result`
    (service-update.yml) says whether the new image was started
    (`upgrade_started`) and lists the restores it ran (`restored`)."""
    if not backups:
        return []
    restored = {r.get("service"): r.get("ok") for r in result.get("restored") or []}
    lines = []
    for s in backups:
        svc, where = s["service"], s["backup_dir"]
        if not result.get("upgrade_started"):
            state = "new image never started; nothing to restore"
        elif svc not in restored:
            state = "restore did not run - check the host"
        elif restored[svc]:
            state = "restored before the previous image was started"
        else:
            state = "RESTORE FAILED - restore by hand (docs/AUTOMERGE.md, \"Pre-upgrade backups\")"
        lines.append(f"- Pre-upgrade backup of {svc} (`{where}`): {state}")
    return lines


def open_revert_mr(gl: GitLab, plan: dict) -> Optional[int]:
    """Branch from the merge commit and restore the pre-merge compose file. Idempotent."""
    iid = plan["mr_iid"]
    branch = f"automerge-revert/{iid}"
    try:
        found = gl.get(f"projects/{PROJECT}/merge_requests", source_branch=branch, state="opened")
        if found:
            return found[0]["iid"]
        changed = plan.get("changed_files") or [policy.COMPOSE_PATH]
        actions = [{"action": "update", "file_path": path,
                    "content": Path(plan["prev_compose_dir"], Path(path).name).read_text()}
                   for path in changed]
        try:
            gl.get(f"projects/{PROJECT}/repository/branches/{urllib.parse.quote(branch, safe='')}")
        except RuntimeError:
            gl.write("POST", f"projects/{PROJECT}/repository/commits", {
                "branch": branch, "start_sha": plan["merge_sha"],
                "commit_message": "Revert image update for " + ", ".join(s["service"] for s in plan["services"]),
                "actions": actions,
            })
        mr = gl.write("POST", f"projects/{PROJECT}/merge_requests", {
            "source_branch": branch, "target_branch": "main", "remove_source_branch": True,
            "title": f"Revert image update from !{iid}",
            "description": f"Restores the image lines changed by !{iid}, matching what is now running "
                           f"after the automatic rollback ({os.environ.get('CI_JOB_URL', '')}).",
            "labels": LABEL_ROLLBACK,
        })
        return mr["iid"] if mr else None
    except (RuntimeError, OSError) as e:
        log(f"Could not open revert MR: {e}")
        return None


def cmd_daily_report(args: argparse.Namespace) -> int:
    dry = args.dry_run or truthy(os.environ.get("AUTOMERGE_DRY_RUN"))
    gl = gitlab_from_env(dry_run=dry)
    since = now() - dt.timedelta(hours=24)
    merged = recent_automerges(gl)
    rb_new = [i for i in gl.get_all(f"projects/{PROJECT}/issues", labels=LABEL_ROLLBACK,
                                    created_after=since.isoformat())]
    rb_open = gl.get_all(f"projects/{PROJECT}/issues", labels=LABEL_ROLLBACK, state="opened")
    eligible_open = gl.get_all(f"projects/{PROJECT}/merge_requests", state="opened", labels=LABEL_ELIGIBLE)
    manual_open = gl.get_all(f"projects/{PROJECT}/merge_requests", state="opened", labels=LABEL_MANUAL)
    lines = [f"Auto-merge summary for the 24h ending {now():%Y-%m-%d %H:%M} UTC", ""]
    lines.append(f"Auto-merged: {len(merged)}")
    lines += [f"  !{m['iid']} {m['title']}  {m['web_url']}"
              f"{'  [fast track]' if LABEL_FAST_TRACK in (m.get('labels') or []) else ''}" for m in merged]
    lines.append(f"Rollbacks (new): {len(rb_new)}")
    lines += [f"  #{i['iid']} {i['title']}  {i['web_url']}" for i in rb_new]
    lines.append(f"Rollback issues still open (auto-merge paused for their services): {len(rb_open)}")
    lines += [f"  #{i['iid']} {i['title']}  {i['web_url']}" for i in rb_open]
    lines.append(f"Open MRs currently labelled {LABEL_ELIGIBLE}: {len(eligible_open)}")
    lines += [f"  !{m['iid']} {m['title']}" for m in eligible_open]
    manual_security = [m for m in manual_open if LABEL_SECURITY in (m.get("labels") or [])]
    lines.append(f"Updates waiting for a human ({LABEL_MANUAL}: major, denylist, restricted kind): "
                 f"{len(manual_open)}")
    for m in sorted(manual_open, key=lambda m: m.get("created_at") or ""):
        age = now() - (parse_ts(m.get("created_at")) or now())
        sec = "  [security]" if m in manual_security else ""
        lines.append(f"  !{m['iid']} {m['title']}  (open {age.days}d){sec}  {m['web_url']}")
    body = "\n".join(lines) + "\n"
    log(body)
    if not (merged or rb_new or rb_open or manual_security) and not truthy(os.environ.get("AUTOMERGE_REPORT_ALWAYS")):
        log("Nothing to report; not sending mail.")
        return 0
    if dry:
        log("[dry-run] not sending mail")
        return 0
    subject = "Homelab auto-merge: " + (f"{len(rb_open)} rollback(s) open" if rb_open else f"{len(merged)} merged")
    if not mail_root(subject, body):
        return 1
    log("Summary mailed.")
    return 0


def mail_root(subject: str, body: str) -> bool:
    """Mail root through the router's msmtp. Never raises: alerting must not
    turn a recorded failure into a different one."""
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "LogLevel=ERROR",
           f"root@{ROUTER_HOST}", f"mail -s {shlex.quote(subject)} root"]
    try:
        res = subprocess.run(cmd, input=body, text=True, capture_output=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: sending mail failed: {e}")
        return False
    if res.returncode != 0:
        log(f"ERROR: sending mail failed: {res.stderr.strip()[:300]}")
        return False
    log(f"Mailed root: {subject}")
    return True


def cmd_notify(args: argparse.Namespace) -> int:
    """Alert root from a CI job (e.g. router:deploy-service failing before deploy-report)."""
    body = args.message + "\n\n" + f"Job: {os.environ.get('CI_JOB_URL', 'n/a')}\n" + \
        f"Commit: {os.environ.get('CI_COMMIT_SHA', 'n/a')}\n"
    return 0 if mail_root(args.subject, body) else 1


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("mr-check")
    s.add_argument("--base", required=True)
    s.add_argument("--head", required=True)
    s.add_argument("--author")
    s.add_argument("--target")
    s.add_argument("--labels")
    s.set_defaults(fn=cmd_mr_check)

    s = sub.add_parser("evaluate")
    s.add_argument("iids", nargs="+", type=int)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("sweep")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("plan-deploy")
    s.add_argument("--out", required=True)
    s.add_argument("--host", default="router", help="deploy job prefix (deploy_hosts in the policy file)")
    s.set_defaults(fn=cmd_plan_deploy)

    s = sub.add_parser("deploy-report")
    s.add_argument("--plan", required=True)
    s.add_argument("--result", required=True)
    s.add_argument("--status", choices=["success", "failed"], required=True)
    s.set_defaults(fn=cmd_deploy_report)

    s = sub.add_parser("notify")
    s.add_argument("--subject", required=True)
    s.add_argument("--message", required=True)
    s.set_defaults(fn=cmd_notify)

    s = sub.add_parser("daily-report")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_daily_report)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
