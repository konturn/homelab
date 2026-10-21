#!/usr/bin/env python3
"""Read KV v2 paths from Vault for fetch-vault-secrets, in one process.

Runs on the Ansible controller. A uri task per path cost a module run each
(0.5-1s per path, 40-50s for ~40 paths); this reads them all in about a
second. It only fetches: deciding what is fatal stays in tasks/main.yml.

Input (stdin, JSON):
  {"url": "<vault>/v1/<mount>/data/", "token": "...", "paths": [...],
   "timeout": 10, "retries": 3, "delay": 5, "skip_verify": true}
Output (stdout, JSON): one entry per path, in input order:
  {"path": "...", "status": 200, "json": {...}}   (json only for 200)

Each path gets 1 + `retries` attempts, `delay` seconds apart, until it
answers 200 or 404 (the same rule as the uri task's until/retries). A
timeout or connection error is status -1. Any other final status is
returned as is and fails the play in main.yml. Nothing is printed except
the output document, which holds secrets: the task registering it is no_log.
"""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

OK = (200, 404)


def read(req, ctx, path):
    url = req["url"] + path
    status, body = -1, None
    for attempt in range(int(req["retries"]) + 1):
        if attempt:
            time.sleep(int(req["delay"]))
        r = urllib.request.Request(url, headers={"X-Vault-Token": req["token"]})
        try:
            with urllib.request.urlopen(r, timeout=int(req["timeout"]), context=ctx) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, b""
        except Exception:  # timeout, refused, DNS, TLS: retried, then -1
            status, raw = -1, b""
        if status in OK:
            break
    out = {"path": path, "status": status}
    if status == 200:
        try:
            body = json.loads(raw)
        except ValueError:
            # A 200 that is not JSON is a read error, not an empty secret.
            body, out["status"] = None, -2
        out["json"] = body
    return out


def main():
    req = json.load(sys.stdin)
    ctx = ssl.create_default_context()
    if req.get("skip_verify"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    paths = list(dict.fromkeys(req["paths"]))  # unique, order kept
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda p: read(req, ctx, p), paths))
    json.dump(results, sys.stdout)


if __name__ == "__main__":
    main()
