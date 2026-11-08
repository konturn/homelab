"""HTTP API.

  POST /grafana        Grafana webhook contact point (Bearer GRAFANA_TOKEN)
  POST /gitlab         GitLab project webhook, pipeline and job events (X-Gitlab-Token)
  GET  /mode           effective mode and overrides            (Bearer CONTROL_TOKEN)
  GET  /maint          open windows                              (Bearer CONTROL_TOKEN)
  POST /maint          {"scope": [...], "duration": "2h", "reason": "...", "by": "..."}
  POST /maint/<id>/close
  Read and report endpoints, CONTROL_TOKEN or an ALLOWED_CLIENTS address
  (the OpenClaw gateway):
  GET  /summary?days=7 weekly summary
  GET  /decisions?days=7  raw decision log (routing review)
  GET  /incident/<id>  incident, its decisions, forward and outcome
  GET  /ack?fp=<grafana fingerprint>&status=firing|resolved&starts_at=<startsAt>
                       does the router own this alert? (OpenClaw's Grafana route)
  POST /outcome        OpenClaw's outcome record for an escalation (router/outcomes.py)
  POST /maint/text     {"text": "/maint zwave_js 2h kernel update"} from Telegram,
                       parsed strictly here (router/maint_text.py)
  GET  /healthz        worker alive and database readable

Events are written to the SQLite inbox before the reply, so a restart never
loses an accepted event; the worker thread makes every decision.
"""

import hmac
import ipaddress
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .events import BadPayload, from_gitlab, from_grafana
from .maint_text import BadCommand
from .outcomes import BadOutcome
from .util import parse_duration

MAX_BODY = 1 << 20


class State:
    def __init__(self, engine, grafana_token, control_token, allowed_clients=(), gitlab_token=""):
        self.engine = engine
        self.grafana_token = grafana_token or ""
        self.control_token = control_token or ""
        self.gitlab_token = gitlab_token or ""
        self.allowed = [ipaddress.ip_network(c) for c in allowed_clients if c]
        self.heartbeat = time.time()


def _auth(header, token):
    if not token or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), token.encode())


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        server_version = "event-router"

        def log_message(self, *_):     # decisions are the log; no access log
            pass

        def _send(self, code, body):
            data = json.dumps(body, sort_keys=True, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise BadPayload("missing or oversized body")
            return json.loads(self.rfile.read(n))

        def _control(self):
            return _auth(self.headers.get("Authorization", ""), state.control_token)

        def _client_allowed(self):
            try:
                ip = ipaddress.ip_address(self.client_address[0])
            except ValueError:
                return False
            return any(ip in n for n in state.allowed)

        def do_GET(self):
            u = urlparse(self.path)
            eng = state.engine
            if u.path == "/healthz":
                ok = time.time() - state.heartbeat < 120
                try:
                    eng.store.setting("last_mode")
                except Exception:
                    ok = False
                return self._send(200 if ok else 503, {"ok": ok})
            q = parse_qs(u.query)
            if u.path in ("/summary", "/decisions", "/ack") or u.path.startswith("/incident/"):
                if not (self._control() or self._client_allowed()):
                    return self._send(401, {"error": "unauthorized"})
                if u.path == "/ack":
                    fp = (q.get("fp") or [""])[0]
                    st = (q.get("status") or ["firing"])[0]
                    starts = q["starts_at"][0][:40] if "starts_at" in q else None
                    if not re.match(r"^[0-9a-f]{1,32}$", fp):
                        return self._send(400, {"error": "fp must be a Grafana fingerprint"})
                    return self._send(200, {"accepted": eng.ack(f"grafana:{fp}", st, starts)})
                if u.path.startswith("/incident/"):
                    view = eng.incident_view(u.path[len("/incident/"):])
                    return self._send(200 if view else 404, view or {"error": "no such incident"})
                try:
                    days = max(1, min(90, int((q.get("days") or ["7"])[0])))
                except ValueError:
                    return self._send(400, {"error": "days must be an integer"})
                if u.path == "/decisions":
                    rows = eng.store.decisions_since(eng.clock.now() - days * 86400)
                    return self._send(200, {"decisions": rows[-5000:], "truncated": len(rows) > 5000})
                return self._send(200, eng.summary(days))
            if not self._control():
                return self._send(401, {"error": "unauthorized"})
            if u.path == "/mode":
                return self._send(200, {"mode": eng.mode(), "repo": eng.cfg["mode"],
                                        "host_override": eng.host_override(),
                                        "runtime_override": eng.shared.runtime_mode()})
            if u.path == "/maint":
                return self._send(200, {"windows": eng.store.windows()})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            eng = state.engine
            try:
                if u.path == "/grafana":
                    if not _auth(self.headers.get("Authorization", ""), state.grafana_token):
                        return self._send(401, {"error": "unauthorized"})
                    events = from_grafana(self._body())
                    eng.store.inbox_put(time.time(), [e.to_dict() for e in events])
                    return self._send(200, {"accepted": len(events)})
                if u.path == "/gitlab":
                    tok = self.headers.get("X-Gitlab-Token", "")
                    if not state.gitlab_token or not hmac.compare_digest(tok.encode(), state.gitlab_token.encode()):
                        return self._send(401, {"error": "unauthorized"})
                    ge = eng.cfg["gitlab_events"]
                    events = from_gitlab(self._body(), ge["projects"], ge["branch"], eng.cfg["deploy_jobs"])
                    if events:
                        eng.store.inbox_put(time.time(), [e.to_dict() for e in events])
                    return self._send(200, {"accepted": len(events)})
                if u.path in ("/outcome", "/maint/text"):
                    if not (self._control() or self._client_allowed()):
                        return self._send(401, {"error": "unauthorized"})
                    body = self._body()
                    if u.path == "/outcome":
                        try:
                            rec = eng.record_outcome(body)
                        except BadOutcome as e:
                            return self._send(400, {"error": str(e)})
                        return self._send(200, {"recorded": rec})
                    try:
                        reply = eng.maint_command(str(body.get("text") or ""), by="telegram")
                    except (BadCommand, ValueError) as e:
                        return self._send(400, {"reply": str(e)})
                    return self._send(200, {"reply": reply})
                if not self._control():
                    return self._send(401, {"error": "unauthorized"})
                body = self._body()
                by = str(body.get("by") or "api")[:40]
                if u.path == "/maint":
                    scope = body.get("scope")
                    if isinstance(scope, str):
                        scope = [s for s in re.split(r"[,\s]+", scope) if s]
                    wid = eng.open_window(
                        scope or [], parse_duration(body.get("duration", "")),
                        str(body.get("reason") or "")[:200], by,
                        mode=body.get("mode", "quiet"), playbooks=body.get("playbooks") or [],
                        page_classes=body.get("page_classes") or [])
                    return self._send(200, {"window": wid})
                m = re.match(r"^/maint/(maint-[0-9a-f]{8})/close$", u.path)
                if m:
                    eng.close_window(m.group(1), by)
                    return self._send(200, {"closed": m.group(1)})
                return self._send(404, {"error": "not found"})
            except (BadPayload, ValueError) as e:
                return self._send(400, {"error": str(e)})

    return H


def serve(state, port=8080):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd
