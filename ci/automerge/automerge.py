#!/usr/bin/env python3
"""Auto-merge for low-risk image updates. See docs/AUTOMERGE.md.

Subcommands
  mr-check      Informational check inside an MR pipeline (local git only, no token).
  evaluate      Read-only evaluation of one MR via the API (dry run; any state).
  sweep         Scheduled on main: label eligible MRs, merge at most one ready MR.
  plan-deploy   main pipeline: verify an auto-merged commit, write the deploy plan.
  deploy-report main pipeline: record the deploy outcome (note / issue / revert MR).
  daily-report  Scheduled on main: mail a summary of the last 24h.

Code in this file runs from `main` for every command that holds credentials;
MR content is only ever read as data through the API.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import policy  # noqa: E402

try:
    import yaml  # PyYAML ships with ansible
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(os.environ.get("CI_PROJECT_DIR") or Path(__file__).resolve().parents[2])
ALLOWLIST_PATH = REPO_ROOT / "ci" / "automerge-allowlist.yml"
FREEZE_FILE = REPO_ROOT / "ci" / "automerge-freeze"
CODEOWNERS_PATH = REPO_ROOT / "CODEOWNERS"

LABEL_ELIGIBLE = "automerge-eligible"
LABEL_MERGED = "automerged"
LABEL_ROLLBACK = "automerge-rollback"
LABEL_SECURITY = "security"
TRAILER = "Automerge-Policy: image-update"

API = os.environ.get("CI_API_V4_URL", "https://gitlab.lab.nkontur.com/api/v4").rstrip("/")
PROJECT = os.environ.get("CI_PROJECT_ID", "4")
PROJECT_PATH = os.environ.get("CI_PROJECT_PATH", "root/homelab")
ROUTER_HOST = os.environ.get("AUTOMERGE_ROUTER_HOST", "10.4.0.1")
SSH_KEY = os.environ.get("AUTOMERGE_SSH_KEY", str(REPO_ROOT / "tmp"))


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


def load_allowlist() -> dict:
    if yaml is None:
        raise SystemExit("PyYAML is required")
    with open(ALLOWLIST_PATH) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("services", {})
    data["services"] = {k: (v or {}) for k, v in data["services"].items()}
    return data


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

def host_storage_ok(known: Optional[dict] = None) -> Tuple[bool, str]:
    """Fail closed: any error, or a pool outside policy.storage_verdict, blocks auto-merge."""
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", f"root@{ROUTER_HOST}",
           "zpool list -H -o name,health && echo --- && zpool status -p"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return False, f"storage check failed to run: {e}"
    if out.returncode != 0 or "---" not in out.stdout:
        return False, f"storage check failed (rc={out.returncode}): {out.stderr.strip()[:200]}"
    pools, _, status = out.stdout.partition("---")
    return policy.storage_verdict(pools, status, known)


def global_gates(gl: GitLab, allow: dict, host_check: bool = True) -> List[str]:
    """Return reasons that block every auto-merge right now (empty = open)."""
    reasons: List[str] = []
    if truthy(os.environ.get("AUTOMERGE_FREEZE")):
        reasons.append("AUTOMERGE_FREEZE variable is set")
    if FREEZE_FILE.exists():
        reasons.append(f"{FREEZE_FILE.relative_to(REPO_ROOT)} exists on main")
    open_rb = gl.get(f"projects/{PROJECT}/issues", state="opened", labels=LABEL_ROLLBACK, per_page=20)
    if open_rb:
        reasons.append("open rollback issue(s): " + ", ".join(f"#{i['iid']}" for i in open_rb))
    recent = recent_automerges(gl)
    limit = int(allow.get("max_per_24h", 3))
    if len(recent) >= limit:
        reasons.append(f"rate limit: {len(recent)} auto-merges in the last 24h (max {limit})")
    if host_check:
        ok, msg = host_storage_ok(allow.get("storage_known_state"))
        if not ok:
            reasons.append(msg)
    return reasons


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


def evaluate_api(gl: GitLab, mr: dict, allow: dict, merger: Optional[str] = None) -> policy.Verdict:
    refs = mr.get("diff_refs") or {}
    base_sha, head_sha = refs.get("base_sha"), refs.get("head_sha")
    files = mr_file_changes(gl, mr["iid"])
    base = head = None
    if base_sha and head_sha and any(f.path == policy.COMPOSE_PATH for f in files):
        base = gl.file_at(policy.COMPOSE_PATH, base_sha)
        head = gl.file_at(policy.COMPOSE_PATH, head_sha)
    return policy.evaluate_mr(
        author=mr["author"]["username"], target_branch=mr["target_branch"],
        labels=mr.get("labels") or [], files=files, base_compose=base, head_compose=head,
        allowlist=allow, codeowners_text=read_codeowners(), merger=merger)


def token_user(gl: GitLab) -> str:
    """Username of the identity behind the token (the merger)."""
    return gl.get("user")["username"]


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
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], check=True,
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
    v = policy.evaluate_mr(
        author=args.author or "", target_branch=args.target or "",
        labels=[l for l in (args.labels or "").split(",") if l], files=files,
        base_compose=git_show(base, policy.COMPOSE_PATH), head_compose=git_show(head, policy.COMPOSE_PATH),
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
    merger = token_user(gl)
    for iid in args.iids:
        mr = gl.get(f"projects/{PROJECT}/merge_requests/{iid}")
        v = evaluate_api(gl, mr, allow, merger=merger)
        if args.json:
            log(json.dumps({"iid": iid, "title": mr["title"], **v.as_dict()}))
        else:
            print_verdict(mr, v)
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    dry = args.dry_run or truthy(os.environ.get("AUTOMERGE_DRY_RUN"))
    gl = gitlab_from_env(dry_run=dry)
    allow = load_allowlist()
    gl.ensure_labels()
    merger = token_user(gl)
    log(f"Merging identity: {merger}{' (dry run: no labels, approvals or merges)' if dry else ''}")

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
        mr = gl.get(f"projects/{PROJECT}/merge_requests/{iid}")  # full record (pipeline, merge status)
        candidates[iid] = mr
        verdicts[iid] = evaluate_api(gl, mr, allow, merger=merger)

    gates: List[str] = []
    if any(v.eligible for v in verdicts.values()):
        gates = global_gates(gl, allow, host_check=not args.no_host_check)
        log("Global gates: " + ("open" if not gates else "CLOSED"))
        for g in gates:
            log(f"  - {g}")
    log()

    # MRs labelled `security` (e.g. by the running-image CVE scan) go first.
    order = sorted(candidates, key=lambda i: (LABEL_SECURITY not in (candidates[i].get("labels") or []), i))
    merged_one = False
    for iid in order:
        mr, v = candidates[iid], verdicts[iid]
        eligible = v.eligible and not gates
        print_verdict(mr, v, gates if v.eligible else [])
        has_label = LABEL_ELIGIBLE in (mr.get("labels") or [])
        if eligible and not has_label:
            gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": LABEL_ELIGIBLE})
        elif not eligible and has_label:
            gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"remove_labels": LABEL_ELIGIBLE})
        if not eligible:
            continue
        blockers = readiness(mr)
        if blockers:
            log("  not ready to merge: " + "; ".join(blockers))
            continue
        if merged_one:
            log("  ready, deferred: one auto-merge per run")
            continue
        merged_one = do_merge(gl, mr, v)
    return 0


def do_merge(gl: GitLab, mr: dict, v: policy.Verdict) -> bool:
    iid, sha = mr["iid"], mr["sha"]
    services = ", ".join(c.service for c in v.changes)
    message = (f"Merge branch '{mr['source_branch']}' into '{mr['target_branch']}'\n\n"
               f"Image update: {services}\n\n"
               f"See merge request {PROJECT_PATH}!{iid}\n\n{TRAILER}\n")
    log(f"  merging !{iid} at {sha[:12]} ({services})")
    # Label first so the main pipeline can verify provenance as soon as it starts.
    gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"add_labels": LABEL_MERGED})
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
            gl.write("PUT", f"projects/{PROJECT}/merge_requests/{iid}", {"remove_labels": LABEL_MERGED})
        except RuntimeError as e2:
            log(f"  could not remove {LABEL_MERGED} label: {e2}")
        return False
    lines = [f"Merged by the auto-merge policy (pipeline {os.environ.get('CI_PIPELINE_URL', 'n/a')}).", ""]
    lines += [f"- `{c.service}`: {c.kind} `{c.old}` -> `{c.new}`" for c in v.changes]
    lines += ["", "The main pipeline deploys only these services, waits for them to become healthy, "
                  "and rolls back automatically on failure."]
    gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes", {"body": "\n".join(lines)})
    return True


def cmd_plan_deploy(args: argparse.Namespace) -> int:
    gl = gitlab_from_env(dry_run=False)
    allow = load_allowlist()
    sha = os.environ.get("CI_COMMIT_SHA") or git("rev-parse", "HEAD").strip()

    def abort(msg: str) -> int:
        log(f"ERROR: {msg}")
        log("Nothing was deployed. Run the manual router:deploy job on this pipeline if a full deploy is wanted.")
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

    changed = [p for p in git("diff", "--name-only", first_parent, sha).splitlines() if p]
    if changed != [policy.COMPOSE_PATH]:
        return abort("merge changes files other than the compose file: " + ", ".join(changed))
    base, head = git_show(first_parent, policy.COMPOSE_PATH), git_show(sha, policy.COMPOSE_PATH)
    try:
        changes = policy.diff_compose(base or "", head or "")
    except policy.PolicyError as e:
        return abort(f"merged compose change is outside the policy: {e}")
    services = allow["services"]
    off = [c.service for c in changes if c.service not in services]
    if off or not changes:
        return abort("services not on the allowlist: " + ", ".join(off) if off else "no image changes")

    out = Path(args.out)
    prev_dir = out.parent / "prev"
    prev_dir.mkdir(parents=True, exist_ok=True)
    (prev_dir / "docker-compose.yml").write_text(base or "")
    timeout_default = int(allow.get("default_health_timeout", 600))
    plan = {
        "mr_iid": mr["iid"], "mr_url": mr["web_url"], "mr_title": mr["title"],
        "merge_sha": sha, "first_parent": first_parent, "prev_compose_dir": str(prev_dir),
        "services": [{
            "service": c.service, "kind": c.kind, "old": str(c.old), "new": str(c.new),
            "old_digest": c.old.digest or "", "new_digest": c.new.digest or "",
            "probe": services[c.service].get("probe") or "",
            "timeout": int(services[c.service].get("timeout") or timeout_default),
        } for c in changes],
    }
    out.write_text(json.dumps(plan, indent=2))
    log(f"Deploy plan for !{mr['iid']} ({sha[:12]}):")
    for s in plan["services"]:
        log(f"  {s['service']}: {s['old']} -> {s['new']} (probe: {s['probe'] or 'none'}, timeout {s['timeout']}s)")
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
    svc_list = ", ".join(s["service"] for s in plan["services"])
    if args.status == "success":
        gl.write("POST", f"projects/{PROJECT}/merge_requests/{iid}/notes",
                 {"body": f"Deployed and healthy: {svc_list} ({job})."})
        return 0

    gl.ensure_labels()
    rolled_back = result.get("rollback_healthy")
    rb_text = {True: "rolled back to the previous image and healthy again",
               False: "rolled back to the previous image but STILL UNHEALTHY - needs attention",
               None: "rollback status unknown (deploy job did not finish) - check the host"}[rolled_back]
    title = f"Auto-merge rollback: {svc_list} (!{iid})"
    body = [
        f"The per-service deploy of !{iid} ({plan['mr_title']}) failed its health gate and was {rb_text}.",
        "",
        f"- Deploy job: {job}",
        f"- Merge commit: {plan['merge_sha']}",
        f"- Failure: {result.get('failure') or 'see job log'}",
        "",
        "| service | from | to |", "|---|---|---|",
    ] + [f"| {s['service']} | `{s['old']}` | `{s['new']}` |" for s in plan["services"]] + [
        "",
        "While this issue is open, auto-merge is paused for all MRs. `main` still references the new",
        "image, so the next full router deploy would re-apply it; merge or close the revert MR first.",
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
    return 0


def open_revert_mr(gl: GitLab, plan: dict) -> Optional[int]:
    """Branch from the merge commit and restore the pre-merge compose file. Idempotent."""
    iid = plan["mr_iid"]
    branch = f"automerge-revert/{iid}"
    try:
        found = gl.get(f"projects/{PROJECT}/merge_requests", source_branch=branch, state="opened")
        if found:
            return found[0]["iid"]
        prev = Path(plan["prev_compose_dir"], "docker-compose.yml").read_text()
        try:
            gl.get(f"projects/{PROJECT}/repository/branches/{urllib.parse.quote(branch, safe='')}")
        except RuntimeError:
            gl.write("POST", f"projects/{PROJECT}/repository/commits", {
                "branch": branch, "start_sha": plan["merge_sha"],
                "commit_message": "Revert image update for " + ", ".join(s["service"] for s in plan["services"]),
                "actions": [{"action": "update", "file_path": policy.COMPOSE_PATH, "content": prev}],
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
    lines = [f"Auto-merge summary for the 24h ending {now():%Y-%m-%d %H:%M} UTC", ""]
    lines.append(f"Auto-merged: {len(merged)}")
    lines += [f"  !{m['iid']} {m['title']}  {m['web_url']}" for m in merged]
    lines.append(f"Rollbacks (new): {len(rb_new)}")
    lines += [f"  #{i['iid']} {i['title']}  {i['web_url']}" for i in rb_new]
    lines.append(f"Rollback issues still open (auto-merge paused while any are open): {len(rb_open)}")
    lines += [f"  #{i['iid']} {i['title']}  {i['web_url']}" for i in rb_open]
    lines.append(f"Open MRs currently labelled {LABEL_ELIGIBLE}: {len(eligible_open)}")
    lines += [f"  !{m['iid']} {m['title']}" for m in eligible_open]
    body = "\n".join(lines) + "\n"
    log(body)
    if not (merged or rb_new or rb_open) and not truthy(os.environ.get("AUTOMERGE_REPORT_ALWAYS")):
        log("Nothing to report; not sending mail.")
        return 0
    if dry:
        log("[dry-run] not sending mail")
        return 0
    subject = "Homelab auto-merge: " + (f"{len(rb_open)} rollback(s) open" if rb_open else f"{len(merged)} merged")
    cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
           f"root@{ROUTER_HOST}", f"mail -s '{subject}' root"]
    res = subprocess.run(cmd, input=body, text=True, capture_output=True, timeout=60)
    if res.returncode != 0:
        log(f"ERROR: sending mail failed: {res.stderr.strip()[:300]}")
        return 1
    log("Summary mailed.")
    return 0


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
    s.add_argument("--no-host-check", action="store_true", help="dry runs only")
    s.set_defaults(fn=cmd_sweep)

    s = sub.add_parser("plan-deploy")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_plan_deploy)

    s = sub.add_parser("deploy-report")
    s.add_argument("--plan", required=True)
    s.add_argument("--result", required=True)
    s.add_argument("--status", choices=["success", "failed"], required=True)
    s.set_defaults(fn=cmd_deploy_report)

    s = sub.add_parser("daily-report")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_daily_report)

    args = p.parse_args(argv)
    if getattr(args, "no_host_check", False) and not (args.dry_run or truthy(os.environ.get("AUTOMERGE_DRY_RUN"))):
        p.error("--no-host-check is only allowed with --dry-run")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
