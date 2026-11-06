#!/usr/bin/env python3
"""CI side of NETGEAR firmware updates (see README.md, "Firmware updates in CI").

Subcommands
  bump-mr   weekly (NETGEAR_TASK=firmware-check): if NETGEAR's download server has a
            release newer than the one in netgear-*.yml, open an MR that declares it
            (URL, both SHA-256s, KNOWN_LATEST) with the release notes. Idempotent.
  rollout   daily in the maintenance window (NETGEAR_TASK=firmware), or started by a
            person or OpenClaw: upgrade the switches one at a time with
            `ngswitch.py firmware rollout`, then mail the result. A failure opens
            an issue labelled netgear-firmware; while one is open, rollouts do not run.

GitLab writes use AUTOMERGE_TOKEN and mail goes through the router like the
auto-merge jobs (ci/automerge/automerge.py), so this runs only from main.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "ci" / "automerge"))
import ngswitch as ng  # noqa: E402

LABEL = "netgear-firmware"            # bump MRs and rollout failure issues
LABEL_FAILED = "netgear-firmware-failed"
SWITCH_ORDER = ["netgear-movie", "netgear-office"]   # movie first: office carries the admin Wi-Fi
DEFAULT_WINDOW = "04:45-06:00"
ALERTED = ".netgear-firmware-alerted"   # the job's after_script mails root unless this exists
SUPPORT_URL = "https://www.netgear.com/support/product/{model}/"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _am():
    import automerge  # noqa: E402 - imported late so the offline tests do not need it
    return automerge


def desired_files() -> List[Path]:
    return [HERE / f"{n}.yml" for n in SWITCH_ORDER]


# -- bump MR -------------------------------------------------------------------

def mr_description(model: str, old: dict, new: dict, notes: str, files: List[str]) -> str:
    lines = [
        f"NETGEAR published {model} firmware **{new['version']}** (declared: {old['version']}).",
        "",
        f"- Download: {new['url']}",
        f"- Release notes: {SUPPORT_URL.format(model=model.lower())} (Documentation / Firmware); "
        "the notes shipped in the zip are below",
        f"- zip SHA-256: `{new['sha256']}`",
        f"- image: `{new['image']}`, SHA-256 `{new['image_sha256']}`",
        "",
        "NETGEAR publishes no checksums; the hashes are those of the file downloaded by "
        "`netgear:firmware-check`, the same way the current ones were declared.",
        "",
        "Changed: " + ", ".join(f"`{f}`" for f in files),
        "",
        "### What merging does",
        "",
        "Merging is the approval. The next `netgear:firmware` run in the maintenance window "
        f"({DEFAULT_WINDOW} local, daily pipeline schedule with `NETGEAR_TASK=firmware`) upgrades "
        "netgear-movie, checks it, and only then netgear-office. Each switch is down for about two "
        "minutes. A failure rolls the switch back where it can, stops before the next switch, opens "
        f"an issue labelled `{LABEL_FAILED}` and mails root.",
        "",
        "To upgrade right away instead: Build > Pipelines > Run pipeline on `main` with "
        "`NETGEAR_TASK=firmware` (optionally `NETGEAR_FIRMWARE_TARGET=netgear-movie`, "
        "`NETGEAR_FIRMWARE_DRY_RUN=1`).",
        "",
        "### Release notes (from the zip)",
        "",
        "```",
        (notes.strip() or "(the zip has no release notes)")[:6000],
        "```",
    ]
    return "\n".join(lines) + "\n"


def cmd_bump_mr(args) -> int:
    files = desired_files()
    fw, ds = ng._declared(files)
    model = ds[0][1]["model"]
    latest, url = ng.find_latest(fw, log=log)
    if ng.version_tuple(latest) <= ng.version_tuple(fw["version"]):
        log(f"{model}: {fw['version']} is the newest release on the download server; nothing to do")
        return 0
    log(f"{model}: newer release {latest} at {url}")
    branch = f"netgear-firmware/{latest}"
    am = _am()
    gl = am.gitlab_from_env(dry_run=args.dry_run)
    for mr in gl.get(f"projects/{am.PROJECT}/merge_requests", source_branch=branch, state="all") or []:
        log(f"!{mr['iid']} ({mr['state']}) already proposes {latest}: {mr['web_url']}; not opening another")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        notes, meta = Path(tmp) / "notes.txt", Path(tmp) / "bump.json"
        rc = ng.cmd_firmware_bump(argparse.Namespace(
            files=[str(f) for f in files], version=latest, url=url, cache_dir=args.cache_dir,
            known_latest=None, notes=str(notes), json=str(meta)))
        if rc != 0:
            return rc
        new = json.loads(meta.read_text())["new"]
        text = notes.read_text()
    changed = [str(f.relative_to(REPO)) for f in files] + [str(ng.KNOWN_LATEST_PATH.relative_to(REPO))]
    actions = [{"action": "update", "file_path": p, "content": (REPO / p).read_text()} for p in changed]
    try:
        gl.get(f"projects/{am.PROJECT}/repository/branches/{branch.replace('/', '%2F')}")
        exists = True
    except RuntimeError:
        exists = False
    if exists:  # left over from a run that failed before opening the MR: replace it
        gl.write("DELETE", f"projects/{am.PROJECT}/repository/branches/{branch.replace('/', '%2F')}")
    gl.write("POST", f"projects/{am.PROJECT}/repository/commits", {
        "branch": branch, "start_sha": os.environ.get("CI_COMMIT_SHA") or "main",
        "commit_message": f"netgear: firmware {fw['version']} -> {latest}\n\n"
                          f"Declares {model} firmware {latest} ({url}) with the SHA-256 of the\n"
                          "download and updates KNOWN_LATEST in ci/reports/firmware.py.",
        "actions": actions})
    title = f"netgear: firmware {fw['version']} -> {latest}"
    mr = gl.write("POST", f"projects/{am.PROJECT}/merge_requests", {
        "source_branch": branch, "target_branch": "main", "remove_source_branch": True, "title": title,
        "description": mr_description(model, fw, new, text, changed), "labels": LABEL})
    ref = f"!{mr['iid']} {mr['web_url']}" if mr else "(dry run)"
    log(f"opened {ref}")
    if not args.dry_run:
        am.mail_root(f"Homelab: NETGEAR firmware {latest} available ({ref.split()[0]})",
                     f"{title}\n\n{ref}\n\nMerging the MR approves the upgrade; the next maintenance window "
                     f"({DEFAULT_WINDOW}) applies it, netgear-movie first.\n")
    return 0


# -- rollout -------------------------------------------------------------------

def open_failure_issues(gl, am) -> list:
    return gl.get(f"projects/{am.PROJECT}/issues", state="opened", labels=LABEL_FAILED, per_page=20) or []


def rollout_plan(env: dict) -> dict:
    """What a netgear:firmware run does, from its variables. Raises NgError on bad input."""
    target = (env.get("NETGEAR_FIRMWARE_TARGET") or "").strip()
    if target and target not in SWITCH_ORDER:
        raise ng.NgError(f"NETGEAR_FIRMWARE_TARGET must be one of {', '.join(SWITCH_ORDER)}, not {target!r}")
    scheduled = env.get("CI_PIPELINE_SOURCE") == "schedule"
    window = (env.get("NETGEAR_FIRMWARE_WINDOW") or DEFAULT_WINDOW) if scheduled else None
    if window:
        ng.in_window(window, time.localtime())  # validates the format
    return {"targets": [target] if target else list(SWITCH_ORDER), "window": window,
            "dry_run": ng_truthy(env.get("NETGEAR_FIRMWARE_DRY_RUN")),
            "tftp": (env.get("NETGEAR_TFTP_SERVER") or "").strip() or None}


def ng_truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def summarize(log_text: str) -> List[str]:
    keys = ("upgraded ", "already running", "ERROR", "!!", "ROLLING BACK", "after rollback", "outside the maintenance",
            "came back running", "DID NOT VERIFY", "== firmware rollout")
    return [ln for ln in log_text.splitlines() if any(k in ln for k in keys)]


def cmd_rollout(args) -> int:
    plan = rollout_plan(os.environ)
    am = _am()
    gl = am.gitlab_from_env(dry_run=plan["dry_run"])
    blocking = open_failure_issues(gl, am)
    if blocking:
        for i in blocking:
            log(f"open issue #{i['iid']} {i['title']}: {i['web_url']}")
        log(f"not upgrading while an issue labelled {LABEL_FAILED} is open; close it once the switch is sorted out")
        return 0
    cmd = [sys.executable, "-u", str(HERE / "ngswitch.py"), "firmware", "rollout",
           "--cache-dir", args.cache_dir, "--backup-dir", "netgear-backup", "--export-dir", "netgear-export",
           "--report-prefix", "netgear-drift-"]
    if plan["window"]:
        cmd += ["--window", plan["window"]]
    if plan["tftp"]:
        cmd += ["--tftp-server", plan["tftp"]]
    if plan["dry_run"]:
        cmd.append("--dry-run")
    cmd += [str(HERE / f"{t}.yml") for t in plan["targets"]]
    if plan["window"]:
        log(f"maintenance window {plan['window']}, local time now {time.strftime('%H:%M %Z')} "
            f"(TZ={os.environ.get('TZ', 'unset')})")
    log("$ " + " ".join(cmd))
    out: List[str] = []
    with open("netgear-firmware.log", "w") as fh:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            sys.stdout.write(line)
            fh.write(line)
            out.append(line)
        rc = p.wait()
    text = "".join(out)
    job = os.environ.get("CI_JOB_URL", "n/a")
    summary = "\n".join(summarize(text)) or "(no output)"
    if plan["dry_run"]:
        log(f"dry run finished with exit code {rc}; no issue or mail")
        return rc
    if rc != 0:
        tail = "\n".join(text.splitlines()[-60:])
        body = (f"`netgear:firmware` exited {rc} for {', '.join(plan['targets'])}. It stops before the next "
                f"switch; rollouts stay paused while this issue is open.\n\n- Job: {job}\n\n"
                f"Summary:\n```\n{summary}\n```\n\nEnd of the log:\n```\n{tail}\n```\n\n"
                "Check `ngswitch.py firmware status` and `drift`. If the only difference is new firmware "
                "defaults in the exported configuration, refresh the snapshot in an MR, then close this "
                "issue; the next window carries on with the remaining switches.\n")
        issue = gl.write("POST", f"projects/{am.PROJECT}/issues",
                         {"title": f"NETGEAR firmware rollout failed ({time.strftime('%Y-%m-%d')})",
                          "description": body, "labels": f"{LABEL},{LABEL_FAILED}"})
        ref = f"#{issue['iid']} {issue['web_url']}" if issue else "(issue not created)"
        if am.mail_root("Homelab: NETGEAR firmware rollout FAILED", f"{summary}\n\nIssue: {ref}\nJob: {job}\n"):
            Path(ALERTED).touch()
        return rc
    if "upgraded " in text:
        am.mail_root("Homelab: NETGEAR firmware upgraded", f"{summary}\n\nJob: {job}\n")
    else:
        log("nothing was upgraded; not sending mail")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("bump-mr")
    s.add_argument("--dry-run", action="store_true", help="log the GitLab writes instead of making them")
    s.add_argument("--cache-dir", default=os.environ.get("NETGEAR_FIRMWARE_DIR", "/cache/netgear-firmware"))
    s.set_defaults(fn=cmd_bump_mr)
    s = sub.add_parser("rollout")
    s.add_argument("--cache-dir", default=os.environ.get("NETGEAR_FIRMWARE_DIR", "/cache/netgear-firmware"))
    s.set_defaults(fn=cmd_rollout)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ng.NgError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
