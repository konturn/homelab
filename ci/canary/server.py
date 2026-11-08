#!/usr/bin/env python3
"""Canary service: runs canary.py for CI jobs (docs/CANARY.md).

MR pipelines run on the unprotected runner, which has no Docker access (and
the docker-socket-proxy refuses containers to everyone). This service is the
one narrow door: a CI job POSTs /run with its CI_JOB_TOKEN, and the service

  1. asks GitLab who the token belongs to (GET /api/v4/job): it must be a
     running job of this project. The commit to test is the job's pipeline
     SHA as GitLab reports it, never anything the caller says;
  2. fetches that commit and main with the same job token;
  3. picks the services whose config or compose definition changed against
     the merge base with main (canary.py plan);
  4. runs one canary at a time (canary.py run, which holds a lock) and streams
     the output back. The last line is `CANARY-RESULT: PASS|FAIL ...`.

If the caller disconnects (job canceled or timed out), the running canary is
sent SIGTERM and tears itself down.

Listens on the br-ci gateway address only; networking/iptables/rules.v4 lets
the CI job network reach this port and nothing else on the router.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CANARY = os.path.join(HERE, "canary.py")
LISTEN = os.environ.get("CANARY_LISTEN", "172.28.0.1:8743")
GITLAB = os.environ.get("CANARY_GITLAB_URL", "https://gitlab.lab.nkontur.com").rstrip("/")
PROJECT_ID = int(os.environ.get("CANARY_PROJECT_ID", "4"))
REPO_URL = os.environ.get("CANARY_REPO_URL", GITLAB + "/root/homelab.git")
STATE = os.environ.get("CANARY_STATE_DIR", "/var/lib/homelab-canary")
GIT_DIR = os.path.join(STATE, "repo.git")
LOG_DIR = os.path.join(STATE, "logs")
KEEP_LOGS = 30
_git_lock = threading.Lock()


def verify_job(token):
    req = urllib.request.Request(GITLAB + "/api/v4/job", headers={"JOB-TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            job = json.load(r)
    except urllib.error.HTTPError as e:
        raise PermissionError(f"GitLab rejected the job token ({e.code})")
    pipeline = job.get("pipeline") or {}
    if job.get("status") != "running":
        raise PermissionError(f"job {job.get('id')} is {job.get('status')}, not running")
    if pipeline.get("project_id") != PROJECT_ID:
        raise PermissionError(f"job {job.get('id')} is not a project {PROJECT_ID} job")
    return job


def git(*args, token=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        # Header via env, so the token is never in a process argument list.
        basic = b64encode(f"gitlab-ci-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return subprocess.run(["git", "--git-dir", GIT_DIR, *args], env=env, check=True,
                          capture_output=True, text=True, timeout=600).stdout.strip()


def fetch(sha, token):
    with _git_lock:
        if not os.path.isdir(GIT_DIR):
            subprocess.run(["git", "init", "-q", "--bare", GIT_DIR], check=True)
        git("fetch", "-q", "--no-tags", REPO_URL,
            "+refs/heads/main:refs/remotes/origin/main", sha, token=token)
        return git("merge-base", "refs/remotes/origin/main", sha)


def prune_logs():
    if not os.path.isdir(LOG_DIR):
        return
    runs = sorted(os.listdir(LOG_DIR), key=lambda d: os.path.getmtime(os.path.join(LOG_DIR, d)))
    for d in runs[:-KEEP_LOGS]:
        shutil.rmtree(os.path.join(LOG_DIR, d), ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "homelab-canary"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _say(self, line):
        self.wfile.write((line.rstrip("\n") + "\n").encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.split("?")[0] != "/run":
            return self.send_error(404)
        token = self.headers.get("JOB-TOKEN", "")
        try:
            job = verify_job(token)
        except PermissionError as e:
            return self.send_error(403, str(e))
        except Exception as e:
            return self.send_error(502, f"cannot verify the job token: {e}")
        sha = job["pipeline"]["sha"]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            self._run(job, sha, token)
        except (BrokenPipeError, ConnectionResetError):
            sys.stderr.write(f"job {job['id']}: client went away\n")

    def _run(self, job, sha, token):
        self._say(f"canary: job {job['id']} ({job.get('ref')}) commit {sha}")
        try:
            base = fetch(sha, token)
            planned = json.loads(subprocess.run(
                [sys.executable, CANARY, "plan", "--git-dir", GIT_DIR, "--rev", sha,
                 "--base", base], check=True, capture_output=True, text=True,
                timeout=300).stdout)
        except subprocess.CalledProcessError as e:
            self._say(f"canary: planning failed: {(e.stderr or '').strip()[-2000:]}")
            self._say("CANARY-RESULT: FAIL (planning)")
            return
        except Exception as e:
            self._say(f"canary: planning failed: {e}")
            self._say("CANARY-RESULT: FAIL (planning)")
            return
        if not planned:
            self._say(f"canary: no canary service changed against {base[:12]}")
            self._say("CANARY-RESULT: PASS (nothing to test)")
            return
        self._say(f"canary: services to test: {', '.join(planned)} (base {base[:12]})")
        prune_logs()
        results = {}
        for name in planned:
            out = os.path.join(LOG_DIR, f"{job['id']}-{name}")
            proc = subprocess.Popen(
                [sys.executable, CANARY, "run", "--git-dir", GIT_DIR, "--rev", sha,
                 "--service", name, "--out", out],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                start_new_session=True)
            try:
                for line in proc.stdout:
                    self._say(line)
                results[name] = proc.wait()
            except (BrokenPipeError, ConnectionResetError):
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=300)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    subprocess.run([sys.executable, CANARY, "gc"], timeout=300)
                raise
        failed = [n for n, rc in results.items() if rc != 0]
        verdict = "FAIL" if failed else "PASS"
        detail = ", ".join(f"{n}={'fail' if rc else 'pass'}" for n, rc in results.items())
        self._say(f"CANARY-RESULT: {verdict} {detail}")


def main():
    host, port = LISTEN.rsplit(":", 1)
    os.makedirs(LOG_DIR, exist_ok=True)
    subprocess.run([sys.executable, CANARY, "gc"], timeout=600)
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(f"homelab-canary listening on {LISTEN}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
