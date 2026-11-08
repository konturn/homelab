"""State shared with the host services, as small files in the data volume
(/persistent_data/application/event-router/data/shared on the host).

  windows.json      open maintenance windows; the executor re-checks them at
                    execution time (doc 12.6 a)
  runtime_mode      {"mode", "by", "at"}: the router's own kill-switch override
                    (automation storm); the Telegram "Resume" button removes it
  outbox/*.json     messages for Noah; event-router-control sends and deletes

Files, not the SQLite database: root-owned processes opening the router's
database could leave WAL files the router cannot open.
"""

import json
import os
import tempfile
import uuid


class SharedState:
    def __init__(self, directory):
        self.dir = directory
        if directory:
            os.makedirs(os.path.join(directory, "outbox"), exist_ok=True)

    def _write(self, name, data):
        if not self.dir:
            return
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, sort_keys=True)
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(self.dir, name))

    def write_windows(self, windows):
        self._write("windows.json", {"windows": [
            {k: w[k] for k in ("id", "scope", "mode", "playbooks", "page_classes", "expires_at")}
            for w in windows]})

    def runtime_mode(self):
        if not self.dir:
            return getattr(self, "_mem_mode", None)
        try:
            with open(os.path.join(self.dir, "runtime_mode")) as f:
                v = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"mode": "notify-only", "by": "unreadable", "at": 0}
        return v if isinstance(v, dict) and "mode" in v else {"mode": "notify-only", "by": "invalid", "at": 0}

    def set_runtime_mode(self, value):
        if not self.dir:
            self._mem_mode = value
            return
        if value is None:
            try:
                os.unlink(os.path.join(self.dir, "runtime_mode"))
            except FileNotFoundError:
                pass
        else:
            self._write("runtime_mode", value)

    def queue_notification(self, now, text, urgent, due_at):
        if not self.dir:
            self.__dict__.setdefault("_mem_outbox", []).append(
                {"text": text, "urgent": urgent, "due_at": due_at})
            return
        name = f"{int(now * 1000)}-{uuid.uuid4().hex[:6]}.json"
        self._write(os.path.join("outbox", name),
                    {"created": now, "text": text, "urgent": urgent, "due_at": due_at})
