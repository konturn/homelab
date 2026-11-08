"""Outbound clients: the host executor (Unix socket) and GitLab (read-only)."""

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


class ExecutorClient:
    """Sends exactly {action_type, target, incident_id} (section 4.6)."""

    def __init__(self, path, timeout=180):
        self.path = path
        self.timeout = timeout

    def execute(self, action_type, target, incident_id):
        req = json.dumps({"action_type": action_type, "target": target,
                          "incident_id": incident_id}).encode() + b"\n"
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(self.timeout)
                s.connect(self.path)
                s.sendall(req)
                sent = True
                s.shutdown(socket.SHUT_WR)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            reply = json.loads(buf.decode() or "{}")
            if not isinstance(reply, dict) or "ok" not in reply:
                return {"ok": False, "unknown": True, "detail": "malformed executor reply"}
            return reply
        except (OSError, ValueError) as e:
            # Not connected: nothing ran. Sent but no reply: it may have run.
            return {"ok": False, "unknown": sent, "detail": f"executor: {e.__class__.__name__}: {e}"}


def _ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitLab:
    """Read-only GitLab API access for the reconciler and the deploy checks."""

    def __init__(self, url, token, project_id=4, timeout=15, cafile=None):
        self.url = url.rstrip("/")
        self.token = token
        self.project = project_id
        self.timeout = timeout
        self.ctx = ssl.create_default_context(cafile=cafile) if cafile else None

    def _get(self, path, params=None):
        q = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
        req = urllib.request.Request(f"{self.url}/api/v4/projects/{self.project}/{path}{q}",
                                     headers={"PRIVATE-TOKEN": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
            return json.load(r)

    def running_jobs(self):
        """[{id, name, started_at}] or None when GitLab is unreachable."""
        try:
            jobs = self._get("jobs", {"scope[]": ["running"], "per_page": 100})
        except (OSError, ValueError, urllib.error.URLError):
            return None
        return [{"id": j["id"], "name": j["name"], "started_at": _ts(j.get("started_at"))}
                for j in jobs if isinstance(j, dict) and "id" in j]

    def deploy_running(self, names):
        jobs = self.running_jobs()
        if jobs is None:
            return None
        return any(j["name"] in names for j in jobs)

    def last_deploy_finished(self, names):
        """Epoch of the newest finished deploy job, 0 if none in the last page,
        None if GitLab cannot be asked."""
        try:
            jobs = self._get("jobs", {"scope[]": ["success", "failed", "canceled"], "per_page": 100})
        except (OSError, ValueError, urllib.error.URLError):
            return None
        times = [_ts(j.get("finished_at")) for j in jobs if j.get("name") in names]
        return max([t for t in times if t] or [0])

    def job_status(self, job_id):
        try:
            return self._get(f"jobs/{int(job_id)}").get("status")
        except (OSError, ValueError, urllib.error.URLError):
            return None
