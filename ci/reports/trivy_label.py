#!/usr/bin/env python3
"""Label open image-update MRs for images with fixable HIGH/CRITICAL CVEs (CI side).

Reads cves.json from the router (written by trivy_scan.py) and adds the
`security` label to open update MRs (Renovate bot or moltbot) whose title or
branch names an affected image or service. Adds labels only; never removes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from typing import Iterable, List, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_remote import SSH  # noqa: E402

API = os.environ.get("CI_API_V4_URL", "https://gitlab.lab.nkontur.com/api/v4").rstrip("/")
PROJECT = os.environ.get("CI_PROJECT_ID", "4")
LABEL = "security"
UPDATE_AUTHORS = {a for a in os.environ.get(
    "REPORT_UPDATE_AUTHORS", "project_4_bot_af78a2ea227eba9e10006515fbde375e,moltbot").split(",") if a}
REPORT_JSON = os.environ.get(
    "CVE_REPORT_JSON", "/persistent_data/application/openclaw/workspace/reports/updates/cves.json")


def image_keys(repo: str) -> Set[str]:
    """Names an MR might use for an image repository (with/without registry, library/)."""
    keys = {repo}
    parts = repo.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
        keys.add("/".join(parts))
    if len(parts) > 1 and parts[0] == "library":
        keys.add("/".join(parts[1:]))
    return keys


def mr_matches(title: str, branch: str, repos: Iterable[str], services: Iterable[str]) -> bool:
    t = title.lower()
    b = branch.lower()
    for repo in repos:
        for k in image_keys(repo.lower()):
            if re.search(r"(?<![\w./-])" + re.escape(k) + r"(?![\w/.-])", t):
                return True
            san = k.replace("/", "-").replace("@", "")
            if re.match(r"^renovate/(?:[a-z0-9]+-)?" + re.escape(san) + r"-(?:\d|digest)", b):
                return True
    for s in services:
        if re.match(r"^image-update/(?:major/)?" + re.escape(s.lower()) + r"(?:$|/|-v?\d)", b):
            return True
    return False


def api(method: str, path: str, token: str, params=None, body=None):
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def open_update_mrs(token: str) -> List[dict]:
    out, page = [], 1
    while True:
        batch = api("GET", f"projects/{PROJECT}/merge_requests", token,
                    {"state": "opened", "per_page": 100, "page": page})
        out += [m for m in batch if m["author"]["username"] in UPDATE_AUTHORS]
        if len(batch) < 100:
            return out
        page += 1


def main() -> int:
    token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        print("AUTOMERGE_TOKEN not set; skipping MR labelling")
        return 0
    r = subprocess.run(SSH + [f"cat {REPORT_JSON}"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"could not read {REPORT_JSON}: {r.stderr.strip()[:200]}")
        return 1
    images = [i for i in json.loads(r.stdout)["data"]["images"] if i.get("critical") or i.get("high")]
    repos = {i["repo"] for i in images}
    services = {s for i in images for s in i.get("services", [])}
    mrs = open_update_mrs(token)
    print(f"{len(images)} affected images; {len(mrs)} open update MRs")
    dry = os.environ.get("REPORT_DRY_RUN", "").lower() in ("1", "true", "yes")
    for m in mrs:
        if LABEL in (m.get("labels") or []):
            continue
        if mr_matches(m["title"], m["source_branch"], repos, services):
            print(f"  !{m['iid']}: add label {LABEL}{' (dry run)' if dry else ''}")
            if not dry:
                api("PUT", f"projects/{PROJECT}/merge_requests/{m['iid']}", token, body={"add_labels": LABEL})
    return 0


if __name__ == "__main__":
    sys.exit(main())
