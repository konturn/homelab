#!/usr/bin/env python3
"""Minimal fake Vault for the fetch-vault-secrets fail-closed tests.

Usage: mock_vault.py PORT

Serves JWT/AppRole login and KV v2 reads for the paths in test.yml. The
behaviour of each path is set per request by the X-Scenario header the test
sends via vault_addr (http://127.0.0.1:PORT/<scenario>), so one server
handles every scenario:

  ok              every path returns its fields
  timeout         svc/b never answers in time
  forbidden       svc/b returns 403
  missing-path    svc/b returns 404 (required path)
  missing-opt     opt/c returns 404 (optional path)
  missing-field   svc/a is missing field "two"
  empty-field     svc/a field "two" is ""
  bad-auth        logins return 400
  real-timeout    for a run of the real router.yml: infrastructure/omapi
                  never answers in time, every other path returns no fields
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = {
    "svc/a": {"one": "value-one", "two": "value-two\n"},
    "svc/b": {"three": "value-three"},
    "opt/c": {"four": "value-four"},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep test output short
        pass

    def reply(self, code, body=None):
        raw = json.dumps(body or {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:  # client gave up (the timeout scenario)
            pass

    def route(self):
        # /<scenario>/v1/<rest>
        parts = self.path.lstrip("/").split("/", 2)
        if len(parts) < 3 or parts[1] != "v1":
            return None, None
        return parts[0], parts[2]

    def do_POST(self):
        scenario, rest = self.route()
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if rest in ("auth/jwt/login", "auth/approle/login"):
            if scenario == "bad-auth":
                return self.reply(400, {"errors": ["invalid credentials"]})
            return self.reply(200, {"auth": {"client_token": "test-token"}})
        self.reply(404, {"errors": []})

    def do_GET(self):
        scenario, rest = self.route()
        if self.headers.get("X-Vault-Token") != "test-token":
            return self.reply(403, {"errors": ["permission denied"]})
        if rest == "ssh-client-signer/config/ca":
            return self.reply(200, {"data": {"public_key": "ssh-ed25519 AAAA test"}})
        prefix = "homelab/data/"
        if not rest.startswith(prefix):
            return self.reply(404, {"errors": []})
        path = rest[len(prefix):]
        if scenario == "real-timeout":
            if path == "infrastructure/omapi":
                time.sleep(3)
            return self.reply(200, {"data": {"data": {}}})
        data = dict(DATA.get(path, {}))
        if path == "svc/b" and scenario == "timeout":
            time.sleep(3)
        if path == "svc/b" and scenario == "forbidden":
            return self.reply(403, {"errors": ["permission denied"]})
        if path == "svc/b" and scenario == "missing-path":
            return self.reply(404, {"errors": []})
        if path == "opt/c" and scenario == "missing-opt":
            return self.reply(404, {"errors": []})
        if path == "svc/a" and scenario == "missing-field":
            data.pop("two")
        if path == "svc/a" and scenario == "empty-field":
            data["two"] = ""
        if path not in DATA:
            return self.reply(404, {"errors": []})
        self.reply(200, {"data": {"data": data}})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
