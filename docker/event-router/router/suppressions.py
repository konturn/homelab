"""Router suppressions (doc 8.3, property L6): known, expected events that
should not reach OpenClaw for a while.

suppressions.yml, reviewed by Noah (CODEOWNERS /docker/):

  suppressions:
    - id: nas-migration-smart-noise
      match: {source: grafana, rule_uid: smart-collection-failed, labels: {host: nas}}
      reason: SMART polling fails while the disks move to the new shelf
      until: 2026-12-01            # mandatory; at most a year after it was added
      added_by: noah

Every entry needs a reason and an expiry. An expired entry stops applying on
its own (L6) and is listed as expired by /summary until someone removes it;
the weekly routing review lists every active and expired entry. Page classes
(security, data at risk, UPS, ...) cannot be suppressed: CI rejects such an
entry, and the engine checks page classes first anyway (S9).
"""

import datetime as dt
import re

import yaml

from .config import ConfigError, _selector, page_rule_uids, selector_matches

MAX_DAYS = 366
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _date(v, what):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        raise ConfigError(f"{what}: until must be a date (YYYY-MM-DD)") from None


def parse(raw):
    if raw is None:
        return []
    items = (raw or {}).get("suppressions") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ConfigError("suppressions.yml: needs a `suppressions:` list")
    out, seen = [], set()
    for i, s in enumerate(items):
        where = f"suppressions[{i}]"
        if not isinstance(s, dict) or set(s) - {"id", "match", "reason", "until", "added_by"}:
            raise ConfigError(f"{where}: takes id, match, reason, until, added_by")
        sid = s.get("id")
        if not isinstance(sid, str) or not _ID.match(sid) or sid in seen:
            raise ConfigError(f"{where}: id must be a unique lowercase name")
        seen.add(sid)
        reason = s.get("reason")
        if not isinstance(reason, str) or len(reason.strip()) < 10:
            raise ConfigError(f"{where} ({sid}): a reason is mandatory (a sentence, not a word)")
        if "until" not in s:
            raise ConfigError(f"{where} ({sid}): an expiry (until) is mandatory")
        out.append({"id": sid, "match": _selector(s.get("match"), f"{where}.match"),
                    "reason": reason.strip(), "until": _date(s["until"], where),
                    "added_by": str(s.get("added_by") or "")})
    return out


def load(path):
    try:
        with open(path) as f:
            return parse(yaml.safe_load(f))
    except FileNotFoundError:
        return []


def validate(entries, cfg, today=None):
    """CI checks on top of parse(): expiry at most a year out, no page class."""
    today = today or dt.date.today()
    errs = []
    pages = page_rule_uids(cfg)
    for s in entries:
        if (s["until"] - today).days > MAX_DAYS:
            errs.append(f"suppression {s['id']}: until {s['until']} is more than a year out")
        if s["match"]["rule_uid"] in pages:
            errs.append(f"suppression {s['id']}: {s['match']['rule_uid']} is a page class and "
                        f"cannot be suppressed")
    return errs


def active(entries, now_date):
    return [s for s in entries if s["until"] >= now_date]


def match(entries, ev, now_date):
    """The first active suppression matching the event, or None. An entry
    applies through its `until` day and never after it (L6)."""
    for s in entries:
        if s["until"] >= now_date and selector_matches(s["match"], ev):
            return s
    return None


def describe(entries, now_date):
    return [{"id": s["id"], "reason": s["reason"], "until": s["until"].isoformat(),
             "added_by": s["added_by"], "expired": s["until"] < now_date,
             "match": {k: v for k, v in s["match"].items() if v}}
            for s in entries]
