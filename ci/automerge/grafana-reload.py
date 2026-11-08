#!/usr/bin/env python3
"""Reload Grafana provisioning through its API and check it took effect.

Used by ansible/service-update.yml for bot-config merges (docs/AUTOMERGE.md,
"Bot-config class") instead of restarting Grafana: a restart makes every
Loki-backed alert query fail while it is down (see CLAUDE.md), and dashboards
and alert rules can be reloaded in place.

  grafana-reload.py --host grafana.lab.nkontur.com --ip <lab_nginx_ip>
                    [--alerts FILE ...] [--dashboard FILE ...] [--check-only]

FILE is the deployed provisioning file on this host. After POST
/api/admin/provisioning/{alerting,dashboards}/reload, every rule in the alert
files must read back with the file's `for`, summary, description, severity
and threshold params, and every dashboard with its uid, title and version of
the panels list length. The admin password comes from GRAFANA_ADMIN_PASSWORD
and is never printed. Exits 1 (lines starting FAILED) on any mismatch.
"""

import argparse
import base64
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time

import yaml


class Conn(http.client.HTTPSConnection):
    """HTTPS to a fixed IP with the given host name for SNI and certificate checks."""

    def __init__(self, host, ip, **kw):
        super().__init__(host, 443, timeout=30, context=ssl.create_default_context(), **kw)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def api(args, method, path):
    auth = base64.b64encode(f"admin:{os.environ['GRAFANA_ADMIN_PASSWORD']}".encode()).decode()
    c = Conn(args.host, args.ip)
    try:
        c.request(method, path, headers={"Authorization": "Basic " + auth, "Accept": "application/json",
                                         "Content-Type": "application/json"}, body=b"" if method == "POST" else None)
        r = c.getresponse()
        body = r.read().decode(errors="replace")
        return r.status, body
    finally:
        c.close()


def seconds(d):
    if d is None:
        return 0
    if isinstance(d, (int, float)):
        return int(d)
    total = 0
    for n, unit in re.findall(r"(\d+)([smhd])", str(d)):
        total += int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return total


def thresholds(rule):
    out = []
    for q in rule.get("data") or []:
        for c in (q.get("model") or {}).get("conditions") or []:
            out.append(list((c.get("evaluator") or {}).get("params") or []))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--ip", required=True)
    p.add_argument("--alerts", nargs="*", default=[])
    p.add_argument("--dashboard", nargs="*", default=[])
    p.add_argument("--check-only", action="store_true", help="skip the reload (testing)")
    args = p.parse_args()
    failed = []

    def say(msg):
        print(msg, flush=True)

    for kind, files in (("alerting", args.alerts), ("dashboards", args.dashboard)):
        if not files or args.check_only:
            continue
        status, body = api(args, "POST", f"/api/admin/provisioning/{kind}/reload")
        say(f"reload {kind}: HTTP {status}")
        if status != 200:
            failed.append(f"reload {kind} returned HTTP {status}: {body[:200]}")
    time.sleep(2)

    for path in args.alerts:
        doc = yaml.safe_load(open(path)) or {}
        for g in doc.get("groups") or []:
            for rule in g.get("rules") or []:
                uid = rule.get("uid")
                status, body = api(args, "GET", f"/api/v1/provisioning/alert-rules/{uid}")
                if status != 200:
                    failed.append(f"alert rule {uid}: HTTP {status}")
                    continue
                live = json.loads(body)
                want = {"for": seconds(rule.get("for")),
                        "summary": (rule.get("annotations") or {}).get("summary"),
                        "description": (rule.get("annotations") or {}).get("description"),
                        "severity": (rule.get("labels") or {}).get("severity"),
                        "thresholds": thresholds(rule)}
                got = {"for": seconds(live.get("for")),
                       "summary": (live.get("annotations") or {}).get("summary"),
                       "description": (live.get("annotations") or {}).get("description"),
                       "severity": (live.get("labels") or {}).get("severity"),
                       "thresholds": thresholds(live)}
                diff = [k for k in want if want[k] != got[k]]
                if diff:
                    failed.append(f"alert rule {uid}: live {', '.join(diff)} differ from the file")
        say(f"checked alert rules in {path}")

    for path in args.dashboard:
        d = json.load(open(path))
        status, body = api(args, "GET", f"/api/dashboards/uid/{d['uid']}")
        if status != 200:
            failed.append(f"dashboard {d['uid']}: HTTP {status}")
            continue
        live = json.loads(body).get("dashboard") or {}
        if live.get("title") != d.get("title") or len(live.get("panels") or []) != len(d.get("panels") or []):
            failed.append(f"dashboard {d['uid']}: live title/panels differ from the file")
        say(f"checked dashboard {d['uid']}")

    for f in failed:
        say(f"FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
