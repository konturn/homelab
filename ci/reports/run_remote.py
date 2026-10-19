#!/usr/bin/env python3
"""Run one report script on the router host (CI side).

    run_remote.py SCRIPT [--secret NAME=vault/path ...] [--data DIR] [--extra FILE ...]

Ships ci/reports/*.py (plus --extra files and the --data directory) to a
private temp dir on the router, logs in to Vault with the job's VAULT_ID_TOKEN
(ci-deploy role) if secrets are requested, and runs `python3 SCRIPT` there with
the secrets as a JSON object on stdin. Secrets never appear in argv or logs.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("CI_PROJECT_DIR") or Path(__file__).resolve().parents[2])
HOST = os.environ.get("REPORT_HOST", "10.4.0.1")
SSH_KEY = os.environ.get("REPORT_SSH_KEY", str(ROOT / "tmp"))
SSH = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
       "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
       "-o", "ServerAliveInterval=30", f"root@{HOST}"]


def urlopen_retry(req, tries: int = 6, **kw):
    """urlopen with retries: name lookups of *.lab hosts fail intermittently on the
    runner (musl races pihole against public resolvers). HTTP errors are not retried."""
    for n in range(tries):
        try:
            return urllib.request.urlopen(req, **kw)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError):
            if n + 1 == tries:
                raise
            time.sleep(1 + n)


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def vault_secrets(pairs) -> dict:
    if not pairs:
        return {}
    addr = os.environ.get("VAULT_ADDR", "https://vault.lab.nkontur.com:8200").rstrip("/")
    jwt = os.environ.get("VAULT_ID_TOKEN")
    if not jwt:
        raise SystemExit("VAULT_ID_TOKEN is not set (id_tokens missing on this job?)")
    ctx = ssl_ctx()
    body = json.dumps({"role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": jwt}).encode()
    req = urllib.request.Request(f"{addr}/v1/auth/jwt/login", data=body, headers={"Content-Type": "application/json"})
    token = json.load(urlopen_retry(req, context=ctx, timeout=30))["auth"]["client_token"]
    out = {}
    for pair in pairs:
        name, _, path = pair.partition("=")
        req = urllib.request.Request(f"{addr}/v1/homelab/data/{path}", headers={"X-Vault-Token": token})
        try:
            out[name] = json.load(urlopen_retry(req, context=ctx, timeout=30))["data"]["data"]
        except Exception as e:  # noqa: BLE001 - report which path failed, never the value
            print(f"WARN: could not read Vault path {path}: {type(e).__name__}", file=sys.stderr)
    return out


def bundle(extra, data_dir) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted((ROOT / "ci" / "reports").glob("*.py")):
            tar.add(p, arcname=str(p.relative_to(ROOT)))
        for f in extra or []:
            tar.add(ROOT / f, arcname=f)
        if data_dir:
            tar.add(data_dir, arcname="data")
    return buf.getvalue()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("script")
    p.add_argument("--secret", action="append", default=[])
    p.add_argument("--extra", action="append", default=[])
    p.add_argument("--data")
    p.add_argument("--timeout", type=int, default=7200)
    a = p.parse_args()
    secrets = vault_secrets(a.secret)
    work = f"/tmp/ci-report-{os.environ.get('CI_JOB_ID', os.getpid())}"
    r = subprocess.run(SSH + [f"rm -rf {work} && mkdir -m 700 {work} && tar xz -C {work}"],
                       input=bundle(a.extra, a.data), timeout=120)
    if r.returncode != 0:
        print("ERROR: could not ship the report bundle to the router", file=sys.stderr)
        return 1
    args = " --data data" if a.data else ""
    cmd = (f"cd {work} && python3 ci/reports/{a.script}{args}; rc=$?; cd / && rm -rf {work}; exit $rc")
    r = subprocess.run(SSH + [cmd], input=json.dumps(secrets).encode(), timeout=a.timeout)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
