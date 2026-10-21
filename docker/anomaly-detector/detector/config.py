"""config.yml / suppressions.yml loading and validation."""
import datetime as dt
import fnmatch
import re
from dataclasses import dataclass, field

import yaml

CHECK_TYPES = ("seasonal", "rate", "stale", "drift", "threshold")
SEVERITIES = ("moderate", "severe")
AGG_FNS = ("mean", "max", "min", "sum", "last", "count", "median")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
MAX_SUPPRESSION_DAYS = 366

# Built-in defaults per check type; config.yml `defaults.checks.<type>` and the
# check entry itself override them, in that order.
CHECK_DEFAULTS = {
    "seasonal": {"window": "1h", "weeks": 4, "min_weeks": 3, "z_moderate": 6.0, "z_severe": 10.0,
                 "direction": "both", "min_scale": 0.0, "rel_scale": 0.05, "min_abs_delta": 0.0,
                 "interval": "1h", "for_cycles": 1},
    "rate": {"every": "15m", "lookback": "6h", "min_points": 12, "z_moderate": 8.0, "z_severe": 15.0,
             "direction": "both", "min_scale": 0.0, "rel_scale": 0.0, "interval": "15m",
             "for_cycles": 2},
    "stale": {"lookback": "7d", "age_from": "time", "interval": "5m", "for_cycles": 1},
    "drift": {"every": "1d", "days": 14, "min_points": 7, "direction": "up", "interval": "6h",
              "for_cycles": 1, "min_total_change": 0.0},
    "threshold": {"window": "15m", "interval": "5m", "for_cycles": 2},
}

_DUR = re.compile(r"^(\d+)(s|m|h|d|w)$")
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class ConfigError(ValueError):
    pass


def duration(v):
    """'15m' -> 900. Plain numbers are seconds."""
    if isinstance(v, (int, float)):
        return int(v)
    m = _DUR.match(str(v).strip())
    if not m:
        raise ConfigError(f"bad duration {v!r}")
    return int(m.group(1)) * _UNIT[m.group(2)]


@dataclass
class Check:
    type: str
    params: dict
    id: str = ""
    max_severity: str = "severe"
    interval: int = 300
    for_cycles: int = 1
    when_hours: tuple | None = None


@dataclass
class Signal:
    name: str
    description: str
    owner: str
    category: str
    source: dict
    group_by: list
    checks: list
    labels: dict = field(default_factory=dict)
    max_severity: str = "severe"


@dataclass
class Config:
    timezone: str
    cycle_seconds: int
    signals: list
    context: list
    raw: dict

    def signal(self, name):
        for s in self.signals:
            if s.name == name:
                return s
        return None


def _merge(*dicts):
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            out[k] = v
    return out


def _validate_source(name, src):
    if not isinstance(src, dict):
        raise ConfigError(f"{name}: source must be a mapping")
    if "flux" in src:
        if "{start}" not in src["flux"] or "{stop}" not in src["flux"]:
            raise ConfigError(f"{name}: raw flux must use {{start}} and {{stop}}")
        return
    for k in ("bucket", "measurement", "field"):
        if not src.get(k):
            raise ConfigError(f"{name}: source.{k} is required")
    agg = src.get("agg", "mean")
    if agg not in AGG_FNS:
        raise ConfigError(f"{name}: source.agg must be one of {AGG_FNS}")


def _parse_check(sig_name, c, defaults):
    if not isinstance(c, dict) or c.get("type") not in CHECK_TYPES:
        raise ConfigError(f"{sig_name}: check type must be one of {CHECK_TYPES}")
    t = c["type"]
    p = _merge(CHECK_DEFAULTS[t], defaults.get(t), {k: v for k, v in c.items() if k != "type"})
    for k in ("window", "every", "lookback", "interval", "state_step"):
        if k in p:
            p[k] = duration(p[k])
    for lim_key in ("max_age",):
        if lim_key in p:
            p[lim_key] = {k: duration(v) for k, v in p[lim_key].items()}
    if t == "stale" and not (p.get("max_age") or {}).keys() & set(SEVERITIES):
        raise ConfigError(f"{sig_name}: stale needs max_age.moderate and/or max_age.severe")
    if t == "drift" and not (p.get("slope") or {}).keys() & set(SEVERITIES):
        raise ConfigError(f"{sig_name}: drift needs slope.moderate and/or slope.severe")
    if t == "threshold" and not (p.get("above") or p.get("below")):
        raise ConfigError(f"{sig_name}: threshold needs above and/or below")
    if t == "stale" and p["age_from"] not in ("time", "value"):
        raise ConfigError(f"{sig_name}: stale.age_from must be time or value")
    if t == "seasonal" and p["min_weeks"] > p["weeks"]:
        raise ConfigError(f"{sig_name}: seasonal.min_weeks > weeks")
    max_sev = p.pop("max_severity", "severe")
    if max_sev not in SEVERITIES:
        raise ConfigError(f"{sig_name}: max_severity must be moderate or severe")
    wh = p.pop("when_hours", None)
    if wh is not None:
        if not (isinstance(wh, list) and len(wh) == 2 and all(0 <= int(h) <= 24 for h in wh)):
            raise ConfigError(f"{sig_name}: when_hours must be [start_hour, end_hour]")
        wh = (int(wh[0]), int(wh[1]))
    return Check(type=t, params=p, id=c.get("id") or t, max_severity=max_sev,
                 interval=p.pop("interval"), for_cycles=int(p.pop("for_cycles")), when_hours=wh)


def parse_config(doc):
    if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
        raise ConfigError("config: `signals` list is required")
    defaults = doc.get("defaults") or {}
    check_defaults = defaults.get("checks") or {}
    signals, seen = [], set()
    for s in doc["signals"]:
        name = s.get("name", "")
        if not NAME_RE.match(name):
            raise ConfigError(f"signal name {name!r} must match {NAME_RE.pattern}")
        if name in seen:
            raise ConfigError(f"duplicate signal {name}")
        seen.add(name)
        for k in ("description", "owner", "category"):
            if not s.get(k):
                raise ConfigError(f"{name}: {k} is required")
        _validate_source(name, s.get("source"))
        checks = [_parse_check(name, c, check_defaults) for c in s.get("checks") or []]
        if not checks:
            raise ConfigError(f"{name}: at least one check is required")
        ids = [c.id for c in checks]
        if len(ids) != len(set(ids)):
            raise ConfigError(f"{name}: two checks of the same type need distinct `id`s")
        max_sev = s.get("max_severity", "severe")
        if max_sev not in SEVERITIES:
            raise ConfigError(f"{name}: max_severity must be moderate or severe")
        signals.append(Signal(name=name, description=s["description"], owner=s["owner"],
                              category=s["category"], source=s["source"],
                              group_by=list(s.get("group_by") or []), checks=checks,
                              labels={str(k): str(v) for k, v in (s.get("labels") or {}).items()},
                              max_severity=max_sev))
    return Config(timezone=defaults.get("timezone", "America/New_York"),
                  cycle_seconds=duration(defaults.get("cycle", "5m")),
                  signals=signals, context=list(doc.get("context") or []), raw=doc)


def load_config(path):
    with open(path) as f:
        return parse_config(yaml.safe_load(f))


# ---------------------------------------------------------------------------
# Suppressions
# ---------------------------------------------------------------------------
@dataclass
class Suppression:
    signal: str
    reason: str
    until: dt.date
    series: str = "*"
    check: str = "*"

    def matches(self, signal, check, series):
        return (fnmatch.fnmatchcase(signal, self.signal)
                and fnmatch.fnmatchcase(check, self.check)
                and fnmatch.fnmatchcase(series or "", self.series))

    def active(self, today):
        return today <= self.until

    def to_dict(self):
        return {"signal": self.signal, "series": self.series, "check": self.check,
                "reason": self.reason, "until": self.until.isoformat()}


def parse_suppressions(doc, today=None, strict=False):
    """strict=True (CI) also rejects expiries more than a year out."""
    items = (doc or {}).get("suppressions") or []
    out = []
    for i, s in enumerate(items):
        if not isinstance(s, dict):
            raise ConfigError(f"suppressions[{i}] must be a mapping")
        for k in ("signal", "reason", "until"):
            if not s.get(k):
                raise ConfigError(f"suppressions[{i}]: {k} is required")
        until = s["until"]
        if isinstance(until, str):
            try:
                until = dt.date.fromisoformat(until)
            except ValueError:
                raise ConfigError(f"suppressions[{i}]: until must be YYYY-MM-DD")
        if not isinstance(until, dt.date):
            raise ConfigError(f"suppressions[{i}]: until must be YYYY-MM-DD")
        if strict and today and (until - today).days > MAX_SUPPRESSION_DAYS:
            raise ConfigError(f"suppressions[{i}]: until is more than {MAX_SUPPRESSION_DAYS} days out")
        out.append(Suppression(signal=str(s["signal"]), reason=str(s["reason"]), until=until,
                               series=str(s.get("series", "*")), check=str(s.get("check", "*"))))
    return out


def load_suppressions(path, today=None):
    try:
        with open(path) as f:
            return parse_suppressions(yaml.safe_load(f), today)
    except FileNotFoundError:
        return []
