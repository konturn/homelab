#!/usr/bin/env python3
"""CI side of Aruba Instant firmware updates (see README.md, "Firmware updates in CI").

Subcommands
  bump-mr   weekly (IAP_TASK=firmware-check): run check-latest; if a newer release on
            the declared branch was found, declare it in firmware.yml (and KNOWN_LATEST)
            in an MR. Idempotent. Exits 3 when the vendor cannot be checked and nothing
            newer is staged (the job shows a warning, not a failure).
  rollout   daily in the maintenance window (IAP_TASK=firmware), or started by a person or
            OpenClaw: `iapctl.py firmware upgrade` for the whole cluster, then mail the
            result. A failure opens an issue labelled aruba-iap-firmware-failed; while
            one is open, rollouts do not run. One firmware rollout per night across the
            NETGEAR and Aruba jobs (networking/firmware_night.py).

GitLab writes use AUTOMERGE_TOKEN and mail goes through the router like the
auto-merge jobs (ci/automerge/automerge.py), so this runs only from main.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "networking"))
sys.path.insert(0, str(REPO / "ci" / "automerge"))
import iapfw  # noqa: E402
import firmware_night  # noqa: E402

LABEL = "aruba-iap-firmware"            # bump MRs and rollout failure issues
LABEL_FAILED = "aruba-iap-firmware-failed"
DEFAULT_WINDOW = "04:45-06:00"
ALERTED = ".aruba-iap-firmware-alerted"  # the job's after_script mails root unless this exists
FAMILY = "aruba-iap"
PORTAL = "https://networkingsupport.hpe.com/downloads"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def _am():
    import automerge  # noqa: E402 - imported late so the offline tests do not need it
    return automerge


def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() not in ("", "0", "false", "no", "off")


def in_window(window: str, now: Optional[time.struct_time] = None) -> bool:
    sys.path.insert(0, str(REPO / "networking" / "switches" / "netgear"))
    import ngswitch as ng
    try:
        return ng.in_window(window, now)
    except ng.NgError as e:
        raise iapfw.FwError(str(e)) from None


# -- bump MR -------------------------------------------------------------------

def mr_description(fw: dict, new: dict, res: dict, files: List[str]) -> str:
    lines = [
        f"A newer Instant release on the {fw['branch']} branch was found for the {fw['model']} "
        f"cluster: **{new['version']}** (declared: {fw['version']}).",
        "",
        f"- Found: {res['source']} ({res.get('image') or 'vendor listing'})",
        f"- Image: `{new['image']}` (class {fw['image_class']}), SHA-256 `{new['sha256']}`",
        f"- Release notes: HPE Networking Support Portal ({PORTAL}), Instant {new['version'].split('_')[0]}",
        "",
        "Changed: " + ", ".join(f"`{f}`" for f in files),
        "",
        "### Before merging",
        "",
        f"The image must be on the router at `{iapfw.ROUTER_IMAGE_DIR}/{new['image']}` "
        "(it is when this MR was opened from a staged image). The rollout checks its SHA-256 against "
        "`firmware.yml` and refuses a file that differs.",
        "",
        "### What merging does",
        "",
        "Merging is the approval. The next `aruba-iap:firmware` run in the maintenance window "
        f"({DEFAULT_WINDOW} local, daily pipeline schedule with `IAP_TASK=firmware`, on a night when no NETGEAR "
        "rollout changed anything) upgrades **both APs at once** (Instant runs one release per cluster). "
        "The whole-house Wi-Fi is down for about 5-10 minutes. A failure rolls back where Instant allows it, "
        f"opens an issue labelled `{LABEL_FAILED}` and mails root; rollouts pause while it is open.",
        "",
        "To upgrade right away instead: Build > Pipelines > Run pipeline on `main` with "
        "`IAP_TASK=firmware` (optionally `IAP_FIRMWARE_DRY_RUN=1`).",
    ]
    return "\n".join(lines) + "\n"


def cmd_bump_mr(args) -> int:
    import iapctl
    fw = iapfw.load_firmware(iapfw.FIRMWARE_FILE)
    res = iapfw.check_latest(fw, Path(args.image_dir), iapctl.vault_kv_read)
    for ln in iapfw.format_check(res):
        log(ln)
    if not res["newer"]:
        return iapfw.EXIT_CANNOT_CHECK if res["vendor_error"] else 0
    if res["source"] != "staged":
        log("a newer release is listed but its image is not staged; download it (README, \"Staging an image\")")
        return iapfw.EXIT_CANNOT_CHECK
    latest = res["latest"]
    branch = f"aruba-iap-firmware/{latest}"
    am = _am()
    gl = am.gitlab_from_env(dry_run=args.dry_run)
    for mr in gl.get(f"projects/{am.PROJECT}/merge_requests", source_branch=branch, state="all") or []:
        log(f"!{mr['iid']} ({mr['state']}) already proposes {latest}: {mr['web_url']}; not opening another")
        return 0
    out = iapfw.bump(iapfw.FIRMWARE_FILE, image=Path(res["image"]), log=log)
    new = out["new"]
    changed = [str(iapfw.FIRMWARE_FILE.relative_to(REPO)), str(iapfw.KNOWN_LATEST_PATH.relative_to(REPO))]
    actions = [{"action": "update", "file_path": p, "content": (REPO / p).read_text()} for p in changed]
    enc = branch.replace("/", "%2F")
    try:
        gl.get(f"projects/{am.PROJECT}/repository/branches/{enc}")
        exists = True
    except RuntimeError:
        exists = False
    if exists:  # left over from a run that failed before opening the MR: replace it
        gl.write("DELETE", f"projects/{am.PROJECT}/repository/branches/{enc}")
    title = f"aruba-iap: firmware {fw['version']} -> {latest}"
    gl.write("POST", f"projects/{am.PROJECT}/repository/commits", {
        "branch": branch, "start_sha": os.environ.get("CI_COMMIT_SHA") or "main",
        "commit_message": f"{title}\n\nDeclares Instant {latest} ({new['image']}) with the SHA-256 of the\n"
                          "staged image and updates KNOWN_LATEST in ci/reports/firmware.py.",
        "actions": actions})
    mr = gl.write("POST", f"projects/{am.PROJECT}/merge_requests", {
        "source_branch": branch, "target_branch": "main", "remove_source_branch": True, "title": title,
        "description": mr_description(fw, new, res, changed), "labels": LABEL})
    ref = f"!{mr['iid']} {mr['web_url']}" if mr else "(dry run)"
    log(f"opened {ref}")
    if not args.dry_run:
        am.mail_root(f"Homelab: Aruba Instant firmware {latest} ready ({ref.split()[0]})",
                     f"{title}\n\n{ref}\n\nMerging the MR approves the upgrade; the next maintenance window "
                     f"({DEFAULT_WINDOW}) upgrades both APs at once (Wi-Fi down ~5-10 min).\n")
    return 0


# -- rollout -------------------------------------------------------------------

def open_failure_issues(gl, am) -> list:
    return gl.get(f"projects/{am.PROJECT}/issues", state="opened", labels=LABEL_FAILED, per_page=20) or []


def rollout_plan(env: dict) -> dict:
    """What an aruba-iap:firmware run does, from its variables. Raises FwError on bad input."""
    scheduled = env.get("CI_PIPELINE_SOURCE") == "schedule"
    window = (env.get("IAP_FIRMWARE_WINDOW") or DEFAULT_WINDOW) if scheduled else None
    if window:
        in_window(window, time.localtime())  # validates the format
    return {"window": window, "scheduled": scheduled, "dry_run": truthy(env.get("IAP_FIRMWARE_DRY_RUN")),
            "tftp": (env.get("IAP_TFTP_SERVER") or "").strip() or None}


def summarize(log_text: str) -> List[str]:
    keys = ("== ", "!!", "ERROR", "refused:", "upgrade started", "rolled back", "after rollback")
    return [ln for ln in log_text.splitlines() if any(k in ln for k in keys)]


def touched_devices(log_text: str) -> bool:
    """Whether the run got as far as starting an upgrade (so it used tonight's window)."""
    return "upgrade started" in log_text or "== upgraded" in log_text


def cmd_rollout(args) -> int:
    plan = rollout_plan(os.environ)
    am = _am()
    gl = am.gitlab_from_env(dry_run=plan["dry_run"])
    blocking = open_failure_issues(gl, am)
    if blocking:
        for i in blocking:
            log(f"open issue #{i['iid']} {i['title']}: {i['web_url']}")
        log(f"not upgrading while an issue labelled {LABEL_FAILED} is open; close it once the cluster is sorted out")
        return 0
    if plan["window"] and not in_window(plan["window"]):
        log(f"== outside the maintenance window {plan['window']} (local time {time.strftime('%H:%M %Z')}); not starting")
        return 0
    cmd = [sys.executable, "-u", str(HERE / "iapctl.py"), "firmware", "upgrade",
           "--image-dir", args.image_dir, "--backup-dir", "aruba-iap-backup"]
    if plan["tftp"]:
        cmd += ["--tftp-server", plan["tftp"]]
    if plan["dry_run"]:
        cmd.append("--dry-run")
    if plan["window"]:
        log(f"maintenance window {plan['window']}, local time now {time.strftime('%H:%M %Z')} "
            f"(TZ={os.environ.get('TZ', 'unset')})")
    import contextlib
    lock = contextlib.nullcontext() if plan["dry_run"] else firmware_night.NightLock(
        FAMILY, scheduled=plan["scheduled"], log=log)
    try:
        with lock as lk:
            log("$ " + " ".join(cmd))
            out: List[str] = []
            with open("aruba-iap-firmware.log", "w") as fh:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    sys.stdout.write(line)
                    fh.write(line)
                    out.append(line)
                rc = p.wait()
            text = "".join(out)
            if lk is not None and touched_devices(text):
                lk.used("upgraded" if rc == 0 else f"upgrade exited {rc}")
    except firmware_night.Busy as e:
        log(f"== not starting: {e}")
        return 0
    job = os.environ.get("CI_JOB_URL", "n/a")
    summary = "\n".join(summarize(text)) or "(no output)"
    if plan["dry_run"]:
        log(f"dry run finished with exit code {rc}; no issue or mail")
        return rc
    if rc != 0:
        tail = "\n".join(text.splitlines()[-60:])
        body = (f"`aruba-iap:firmware` exited {rc}. Rollouts stay paused while this issue is open.\n\n- Job: {job}\n\n"
                f"Summary:\n```\n{summary}\n```\n\nEnd of the log:\n```\n{tail}\n```\n\n"
                "Check `iapctl.py firmware status` and `iapctl.py drift`. If the only difference is new firmware "
                "defaults in the configuration, export it in an MR, then close this issue.\n")
        issue = gl.write("POST", f"projects/{am.PROJECT}/issues",
                         {"title": f"Aruba Instant firmware rollout failed ({time.strftime('%Y-%m-%d')})",
                          "description": body, "labels": f"{LABEL},{LABEL_FAILED}"})
        ref = f"#{issue['iid']} {issue['web_url']}" if issue else "(issue not created)"
        if am.mail_root("Homelab: Aruba Instant firmware rollout FAILED", f"{summary}\n\nIssue: {ref}\nJob: {job}\n"):
            Path(ALERTED).touch()
        return rc
    if "== upgraded" in text:
        am.mail_root("Homelab: Aruba Instant firmware upgraded", f"{summary}\n\nJob: {job}\n")
    else:
        log("nothing was upgraded; not sending mail")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_dir = os.environ.get("IAP_FIRMWARE_DIR", iapfw.DEFAULT_IMAGE_DIR)
    s = sub.add_parser("bump-mr")
    s.add_argument("--dry-run", action="store_true", help="log the GitLab writes instead of making them")
    s.add_argument("--image-dir", default=default_dir)
    s.set_defaults(fn=cmd_bump_mr)
    s = sub.add_parser("rollout")
    s.add_argument("--image-dir", default=default_dir)
    s.set_defaults(fn=cmd_rollout)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except iapfw.FwError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
