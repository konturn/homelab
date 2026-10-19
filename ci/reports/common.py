"""Shared helpers for the scheduled read-only reports (see ci/reports/README.md).

Each report writes one compact markdown file plus the full data as JSON to
REPORT_DIR on the router, where the OpenClaw Morning Digest reads it:

    # <Title>
    generated: <UTC ISO8601>
    summary: <one line, at most 120 characters>

    - <detail line>   (at most MAX_LINES)

Report content never goes into git.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Iterable, List, Optional, Sequence

REPORT_DIR = os.environ.get(
    "REPORT_DIR", "/persistent_data/application/openclaw/workspace/reports/updates")
REPORT_UID = int(os.environ.get("REPORT_UID", "1000"))
REPORT_GID = int(os.environ.get("REPORT_GID", "1000"))
MAX_LINES = 15
MAX_SUMMARY = 120


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def render(title: str, summary: str, lines: Sequence[str], generated: Optional[str] = None,
           max_lines: int = MAX_LINES, more_hint: str = "") -> str:
    """Render the digest markdown. Detail lines are prefixed with '- ' and capped."""
    lines = [l if l.startswith("- ") else "- " + l for l in lines if l]
    if len(lines) > max_lines:
        extra = len(lines) - (max_lines - 1)
        lines = lines[: max_lines - 1] + [f"- … {extra} more{(' (' + more_hint + ')') if more_hint else ''}"]
    out = [f"# {title}", f"generated: {generated or utc_now()}",
           f"summary: {clip(summary, MAX_SUMMARY)}", ""]
    return "\n".join(out + lines) + "\n"


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        try:
            os.chown(tmp, REPORT_UID, REPORT_GID)
        except PermissionError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_report(name: str, title: str, summary: str, lines: Sequence[str], data: Any,
                 more_hint: str = "") -> str:
    os.makedirs(REPORT_DIR, mode=0o755, exist_ok=True)
    try:
        os.chown(REPORT_DIR, REPORT_UID, REPORT_GID)
    except PermissionError:
        pass
    generated = utc_now()
    md = render(title, summary, lines, generated=generated,
                more_hint=more_hint or f"see {name}.json")
    payload = {"generated": generated, "summary": summary, "data": data}
    _atomic_write(os.path.join(REPORT_DIR, f"{name}.json"), json.dumps(payload, indent=1, sort_keys=True) + "\n")
    _atomic_write(os.path.join(REPORT_DIR, f"{name}.md"), md)
    log(f"wrote {os.path.join(REPORT_DIR, name)}.md: {clip(summary, MAX_SUMMARY)}")
    return md


def run(cmd: List[str], timeout: int = 120, input: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command, never raising on non-zero exit (callers inspect returncode)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, stdout=e.stdout or "", stderr=f"timeout after {timeout}s")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(e))


def read_secrets() -> dict:
    """Secrets arrive as a JSON object on stdin (from run_remote.py); never logged."""
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


_NUM = re.compile(r"(\d+)")


def version_key(v: str):
    """Natural sort key: '6.8.0-50' > '6.8.0-9'; non-numeric parts compare as text."""
    parts = _NUM.split(v or "")
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""]


def newer(candidate: str, current: str) -> bool:
    nums = lambda s: [int(x) for x in _NUM.findall(s or "")]
    return nums(candidate) > nums(current)


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def join_limited(items: Iterable[str], n: int = 3, sep: str = ", ") -> str:
    items = list(items)
    s = sep.join(items[:n])
    return s + (f" +{len(items) - n}" if len(items) > n else "")
