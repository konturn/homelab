#!/usr/bin/env python3
"""Open, update or close the GitLab issue for router BMC drift (docs/IPMI.md).

Used by the scheduled ipmi:drift job after it ran ``ipmictl check
--markdown`` on the router.

  drift_issue.py --rc <exit code of ipmictl check> --report <markdown file>

rc 0: no drift. An open drift issue gets a note and is closed.
rc 2: drift. The open drift issue gets the report as a note, or a new issue
      is opened (label ``ipmi-drift``). A note is added only when the report
      differs from the issue's last report, so a daily run does not repeat it.
other: the check itself failed; nothing is written, exit 1.

GitLab writes use AUTOMERGE_TOKEN (protected CI variable), like the other
scheduled jobs that open issues or MRs. Python stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = os.environ.get("CI_API_V4_URL", "https://gitlab.lab.nkontur.com/api/v4").rstrip("/")
PROJECT = os.environ.get("CI_PROJECT_ID", "4")
LABEL = "ipmi-drift"
TITLE = "Router BMC settings differ from the desired state (ipmictl)"
MARKER = "<!-- ipmi-drift-report -->"


class GitLab:
    def __init__(self, token, opener=None):
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def req(self, method, path, params=None, body=None):
        url = "%s/%s" % (API, path.lstrip("/"))
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("PRIVATE-TOKEN", self.token)
        if data is not None:
            r.add_header("Content-Type", "application/json")
        for attempt in range(4):
            try:
                with self.opener(r, timeout=30) as resp:
                    payload = resp.read()
                    return json.loads(payload) if payload else None
            except urllib.error.HTTPError as e:
                raise RuntimeError("%s %s -> HTTP %s: %s" % (method, path, e.code, e.read()[:300]))
            except (urllib.error.URLError, OSError):
                if attempt == 3 or method != "GET":
                    raise
                time.sleep(1 + attempt)


def last_report(gl, iid, description):
    """The most recent report body posted to the issue (note or description)."""
    notes = gl.req("GET", "projects/%s/issues/%s/notes" % (PROJECT, iid),
                   {"sort": "desc", "order_by": "created_at", "per_page": 50}) or []
    for n in notes:
        if MARKER in (n.get("body") or ""):
            return n["body"]
    return description or ""


def _body(report, job_url):
    return "%s\n%s\n%s" % (MARKER, report.strip(), ("\nJob: %s" % job_url) if job_url else "")


def _strip(body):
    """Report part only (without the job link) for comparison."""
    return body.split("\nJob: ")[0].strip()


def run(gl, rc, report, job_url):
    issues = gl.req("GET", "projects/%s/issues" % PROJECT,
                    {"state": "opened", "labels": LABEL, "per_page": 20}) or []
    issue = issues[0] if issues else None
    if rc == 0:
        if issue:
            gl.req("POST", "projects/%s/issues/%s/notes" % (PROJECT, issue["iid"]),
                   body={"body": "No drift any more: `ipmictl check` passed.%s" %
                         ((" Job: %s" % job_url) if job_url else "")})
            gl.req("PUT", "projects/%s/issues/%s" % (PROJECT, issue["iid"]), body={"state_event": "close"})
            print("closed issue #%s" % issue["iid"])
        else:
            print("no drift, no open issue")
        return 0
    body = _body(report, job_url)
    if issue:
        if _strip(last_report(gl, issue["iid"], issue.get("description"))) == _strip(body):
            print("drift unchanged; issue #%s already has this report" % issue["iid"])
            return 0
        gl.req("POST", "projects/%s/issues/%s/notes" % (PROJECT, issue["iid"]), body={"body": body})
        print("updated issue #%s" % issue["iid"])
        return 0
    new = gl.req("POST", "projects/%s/issues" % PROJECT,
                 body={"title": TITLE, "labels": LABEL, "description": body})
    print("opened issue #%s" % (new or {}).get("iid"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--rc", type=int, required=True)
    ap.add_argument("--report", required=True)
    a = ap.parse_args(argv)
    if a.rc not in (0, 2):
        print("ipmictl check failed (exit %d); no issue update" % a.rc, file=sys.stderr)
        return 1
    token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        print("AUTOMERGE_TOKEN is not set (protected CI variable)", file=sys.stderr)
        return 1
    report = ""
    if os.path.exists(a.report):
        with open(a.report) as f:
            report = f.read()
    return run(GitLab(token), a.rc, report, os.environ.get("CI_JOB_URL", ""))


if __name__ == "__main__":
    sys.exit(main())
