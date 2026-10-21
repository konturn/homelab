"""HTTP API: /healthz (open), /status, /digest, /context (client allowlist)."""
import ipaddress
import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def parse_networks(spec):
    nets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return nets


def allowed(addr, nets):
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in n for n in nets)


def make_handler(engine, lock, nets, extra_health=lambda: {}):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("%s %s", self.client_address[0], fmt % args)

        def _send(self, code, obj):
            body = json.dumps(obj, indent=1, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            if url.path == "/healthz":
                ok = engine.healthy()
                return self._send(200 if ok else 503, {"ok": ok, **extra_health()})
            if not allowed(self.client_address[0], nets):
                return self._send(403, {"error": "forbidden"})
            try:
                if url.path == "/status":
                    with lock:
                        return self._send(200, engine.status())
                if url.path == "/digest":
                    hours = max(1, min(int(qs.get("hours", ["24"])[0]), 168))
                    return self._send(200, engine.digest(hours))
                if url.path == "/context":
                    return self._send(200, engine.context())
            except Exception as e:  # report, don't kill the server thread
                log.exception("handler")
                return self._send(500, {"error": str(e)[:300]})
            return self._send(404, {"error": "not found",
                                    "endpoints": ["/healthz", "/status", "/digest?hours=24", "/context"]})

    return Handler


def serve(engine, lock, addr, port, nets, extra_health=lambda: {}):
    srv = ThreadingHTTPServer((addr, port), make_handler(engine, lock, nets, extra_health))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
