"""Live detection loop: query -> check -> debounce -> suppress -> outputs."""
import datetime as dt
import logging
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import checks, flux
from .config import load_suppressions
from .influx import InfluxError, line, parse_time, to_float

log = logging.getLogger(__name__)
WEEK = 7 * 86400


def series_key(row, group_by):
    return "/".join(str(row.get(g, "")) for g in group_by)


def in_hours(epoch, when_hours, tz):
    if not when_hours:
        return True
    h = dt.datetime.fromtimestamp(epoch, tz).hour
    start, end = when_hours
    return start <= h < end if start <= end else (h >= start or h < end)


def combine(values, fn):
    """Combine partial-bucket aggregates (a window can straddle two buckets)."""
    vals = [v for _, v in sorted(values) if v is not None]
    if not vals:
        return None
    if fn in ("max",):
        return max(vals)
    if fn in ("min",):
        return min(vals)
    if fn in ("sum", "count"):
        return sum(vals)
    if fn == "last":
        return vals[-1]
    return sum(vals) / len(vals)


class Entry:
    __slots__ = ("signal", "check", "series", "result", "severity", "suppressed", "evaluated_at",
                 "streak")

    def __init__(self, signal, check, series):
        self.signal, self.check, self.series = signal, check, series
        self.result, self.severity, self.suppressed = None, "ok", None
        self.evaluated_at, self.streak = 0.0, 0

    def to_dict(self, tz):
        sig, r = self.signal, self.result
        return {
            "signal": sig.name, "description": sig.description, "category": sig.category,
            "owner": sig.owner, "check": self.check.id, "type": self.check.type,
            "series": self.series, "label": sig.labels.get(self.series, self.series),
            "severity": self.severity, "raw_severity": r.severity, "status": r.status,
            "pending": r.severity != "ok" and self.severity == "ok",
            "score": round(r.score, 3), "value": r.value, "baseline": r.baseline,
            "detail": r.detail,
            "suppressed": self.suppressed.reason if self.suppressed else None,
            "suppressed_until": self.suppressed.until.isoformat() if self.suppressed else None,
            "evaluated_at": dt.datetime.fromtimestamp(self.evaluated_at, tz).isoformat(timespec="seconds"),
        }


class Engine:
    def __init__(self, cfg, influx, suppressions_path=None, write_bucket="metrics", clock=time.time):
        self.cfg = cfg
        self.influx = influx
        self.suppressions_path = suppressions_path
        self.write_bucket = write_bucket
        self.clock = clock
        self.tz = ZoneInfo(cfg.timezone)
        self.entries = {}          # (signal, check_id, series) -> Entry
        self.next_due = {}         # (signal, check_id) -> epoch
        self.errors = {}           # (signal, check_id) -> message
        self.suppressions = []
        self.last_cycle_at = None
        self.last_cycle_seconds = None
        self.last_write_error = None
        self.cycles = 0

    # ------------------------------------------------------------------ eval
    def evaluate(self, sig, chk, now):
        """Return {series: Result} for one check of one signal."""
        p, src, gb = chk.params, sig.source, sig.group_by
        if not in_hours(now, chk.when_hours, self.tz):
            return {s: checks.Result(chk.type, status="skipped", detail="outside when_hours")
                    for (n, c, s) in self.entries if n == sig.name and c == chk.id} or {}
        if chk.type == "seasonal":
            return self._seasonal(sig, chk, now)
        if chk.type == "threshold":
            fn = p.get("agg", src.get("agg", "mean"))
            q = flux.window_query(src, gb, now - p["window"], now, fn)
            out = defaultdict(list)
            for row in self.influx.query(q):
                out[series_key(row, gb)].append((parse_time(row.get("_time")) or now, to_float(row.get("_value"))))
            return {s: checks.threshold(combine(v, fn), p) for s, v in out.items()} or \
                ({"": checks.threshold(None, p)} if not gb else {})
        if chk.type == "rate":
            stop = (int(now) // p["every"]) * p["every"]
            rows = self.influx.query(flux.series_query(src, gb, stop - p["lookback"], stop, p["every"]))
            out = defaultdict(list)
            for row in rows:
                out[series_key(row, gb)].append((parse_time(row["_time"]), to_float(row.get("_value"))))
            return {s: checks.rate([v for _, v in sorted(vals)], p) for s, vals in out.items()}
        if chk.type == "drift":
            start = now - p["days"] * 86400
            fn = p.get("agg", src.get("agg", "mean"))
            rows = self.influx.query(flux.series_query(src, gb, start, now, p["every"], fn))
            out = defaultdict(list)
            for row in rows:
                out[series_key(row, gb)].append((parse_time(row["_time"]), to_float(row.get("_value"))))
            return {s: checks.drift(sorted(vals), p) for s, vals in out.items()}
        if chk.type == "stale":
            return self._stale(sig, chk, now)
        raise ValueError(chk.type)

    def _seasonal(self, sig, chk, now):
        p, src, gb = chk.params, sig.source, sig.group_by
        w = p["window"]
        head = (int(now) // w) * w
        windows = [(head - k * WEEK - w, head - k * WEEK) for k in range(p["weeks"] + 1)]
        fn = p.get("agg", src.get("agg", "mean"))
        vals = defaultdict(dict)
        if "flux" in src:
            for k, (a, b) in enumerate(windows):
                for row in self.influx.query(flux.window_query(src, gb, a, b, fn)):
                    vals[series_key(row, gb)][k] = to_float(row.get("_value"))
        else:
            for row in self.influx.query(flux.union_windows(src, gb, windows, fn)):
                vals[series_key(row, gb)][int(row["_k"])] = to_float(row.get("_value"))
        return {s: checks.seasonal(v.get(0), [v.get(k) for k in range(1, p["weeks"] + 1)], p)
                for s, v in vals.items()}

    def _stale(self, sig, chk, now):
        p, src, gb = chk.params, sig.source, sig.group_by
        newest = {}
        for row in self.influx.query(flux.last_query(src, gb, now - p["lookback"], now)):
            t = parse_time(row.get("_value")) if p["age_from"] == "value" else parse_time(row.get("_time"))
            if t is None:
                continue
            s = series_key(row, gb)
            newest[s] = max(newest.get(s, 0), t)
        known = {s for (n, c, s) in self.entries if n == sig.name and c == chk.id}
        if not gb:
            known.add("")
        params = dict(p, lookback=p["lookback"])
        out = {s: checks.stale(max(0.0, now - t), params) for s, t in newest.items()}
        for s in known - set(out):
            out[s] = checks.stale(None, params)
        return out

    # ----------------------------------------------------------------- cycle
    def _apply(self, sig, chk, results, now):
        for series, r in results.items():
            key = (sig.name, chk.id, series)
            e = self.entries.get(key) or Entry(sig, chk, series)
            e.signal, e.check = sig, chk
            if r.severity != "ok":
                e.streak += 1
            else:
                e.streak = 0
            sev = r.severity if e.streak >= chk.for_cycles else "ok"
            e.severity = checks.cap(checks.cap(sev, chk.max_severity), sig.max_severity)
            e.result, e.evaluated_at = r, now
            self.entries[key] = e
        # Drop series that stopped appearing (e.g. a removed container).
        ttl = max(3 * chk.interval, 7200)
        for key in [k for k, e in self.entries.items()
                    if k[0] == sig.name and k[1] == chk.id and now - e.evaluated_at > ttl]:
            del self.entries[key]

    def _suppress(self, now):
        today = dt.datetime.fromtimestamp(now, self.tz).date()
        if self.suppressions_path:
            try:
                self.suppressions = load_suppressions(self.suppressions_path, today)
            except Exception as e:  # keep the last good list
                log.error("suppressions: %s", e)
        active = [s for s in self.suppressions if s.active(today)]
        for (name, cid, series), e in self.entries.items():
            e.suppressed = next((s for s in active if s.matches(name, cid, series)), None)

    def run_cycle(self, now=None, force=False):
        now = now or self.clock()
        t0 = time.monotonic()
        valid = {(s.name, c.id) for s in self.cfg.signals for c in s.checks}
        for key in [k for k in self.entries if (k[0], k[1]) not in valid]:
            del self.entries[key]
        for sig in self.cfg.signals:
            for chk in sig.checks:
                k = (sig.name, chk.id)
                if not force and now < self.next_due.get(k, 0):
                    continue
                try:
                    results = self.evaluate(sig, chk, now)
                    self.errors.pop(k, None)
                    self._apply(sig, chk, results, now)
                    self.next_due[k] = now + chk.interval
                except (InfluxError, KeyError, ValueError) as e:
                    self.errors[k] = str(e)[:300]
                    self.next_due[k] = now + min(chk.interval, 300)
                    log.warning("%s/%s: %s", sig.name, chk.id, e)
        self._suppress(now)
        self.last_cycle_at = now
        self.last_cycle_seconds = time.monotonic() - t0
        self.cycles += 1
        try:
            self.influx.write(self.write_bucket, self.lines(now))
            self.last_write_error = None
        except InfluxError as e:
            self.last_write_error = str(e)[:300]
            log.error("write: %s", e)

    # --------------------------------------------------------------- outputs
    def lines(self, now):
        out = []
        for e in self.entries.values():
            r = e.result
            if r.status in ("skipped",):
                continue
            out.append(line("anomaly", {
                "signal": e.signal.name, "check": e.check.id, "series": e.series,
                "severity": e.severity, "suppressed": "true" if e.suppressed else "false",
                "category": e.signal.category,
            }, {"score": float(r.score), "value": None if r.value is None else float(r.value),
                "baseline": None if r.baseline is None else float(r.baseline),
                "anomalous": 1 if e.severity != "ok" else 0}, now))
        sev = self.counts()
        out.append(line("anomaly_detector", {}, {
            "severe": sev["severe"], "moderate": sev["moderate"], "suppressed": sev["suppressed"],
            "errors": len(self.errors), "series": len(self.entries),
            "cycle_seconds": float(self.last_cycle_seconds or 0.0),
            "queries": self.influx.queries}, now))
        return out

    def counts(self):
        c = {"severe": 0, "moderate": 0, "suppressed": 0}
        for e in self.entries.values():
            if e.severity == "ok":
                continue
            if e.suppressed:
                c["suppressed"] += 1
            else:
                c[e.severity] += 1
        return c

    def signal_summary(self):
        """Per signal: worst unsuppressed severity and its anomalous series."""
        out = {}
        for sig in self.cfg.signals:
            ents = [e for (n, _, _), e in self.entries.items() if n == sig.name]
            active = [e for e in ents if e.severity != "ok" and not e.suppressed]
            out[sig.name] = {
                "signal": sig, "severity": checks.worst(e.severity for e in active),
                "active": [e.to_dict(self.tz) for e in active],
                "suppressed": sum(1 for e in ents if e.severity != "ok" and e.suppressed),
                "series": len(ents),
            }
        return out

    def healthy(self, now=None):
        now = now or self.clock()
        return (self.last_cycle_at is not None
                and now - self.last_cycle_at < 3 * self.cfg.cycle_seconds
                and self.last_write_error is None)

    def status(self, now=None):
        now = now or self.clock()
        today = dt.datetime.fromtimestamp(now, self.tz).date()
        ents = [e.to_dict(self.tz) for e in self.entries.values()]
        rank = lambda d: (-checks.RANK[d["severity"]], d["signal"], d["series"])
        anomalies = sorted((d for d in ents if d["severity"] != "ok" and not d["suppressed"]), key=rank)
        return {
            "service": "anomaly-detector",
            "generated_at": dt.datetime.fromtimestamp(now, self.tz).isoformat(timespec="seconds"),
            "healthy": self.healthy(now),
            "last_cycle_at": None if self.last_cycle_at is None else
            dt.datetime.fromtimestamp(self.last_cycle_at, self.tz).isoformat(timespec="seconds"),
            "last_cycle_seconds": self.last_cycle_seconds,
            "cycles": self.cycles,
            "influx": {"queries": self.influx.queries,
                       "query_seconds": round(self.influx.query_seconds, 2),
                       "last_error": self.influx.last_error,
                       "last_write_error": self.last_write_error},
            "counts": self.counts(),
            "anomalies": anomalies,
            "gone_quiet": sorted((d for d in ents if d["type"] == "stale" and d["severity"] != "ok"), key=rank),
            "suppressed": sorted((d for d in ents if d["severity"] != "ok" and d["suppressed"]), key=rank),
            "pending": sorted((d for d in ents if d["pending"]), key=rank),
            "insufficient_history": sorted({f'{d["signal"]}/{d["check"]}' for d in ents
                                            if d["status"] == "insufficient_history"}),
            "errors": [{"signal": n, "check": c, "error": m} for (n, c), m in sorted(self.errors.items())],
            "suppressions": {
                "active": [s.to_dict() for s in self.suppressions if s.active(today)],
                "expired": [s.to_dict() for s in self.suppressions if not s.active(today)],
            },
            "signals": [{"name": s.name, "description": s.description, "owner": s.owner,
                         "category": s.category,
                         "checks": [c.id for c in s.checks]} for s in self.cfg.signals],
        }

    # --------------------------------------------------------------- digest
    def digest(self, hours=24, now=None):
        now = now or self.clock()
        rows = self.influx.query(flux.anomaly_digest_query(self.write_bucket, now - hours * 3600, now))
        agg = {}
        for r in rows:
            key = (r.get("signal"), r.get("check"), r.get("series"))
            a = agg.setdefault(key, {"signal": key[0], "check": key[1], "series": key[2],
                                     "severities": set(), "suppressed": False, "evaluations": 0,
                                     "first": None, "last": None, "max_abs_score": 0.0,
                                     "last_value": None, "last_baseline": None})
            a["severities"].add(r.get("severity"))
            a["suppressed"] = a["suppressed"] or r.get("suppressed") == "true"
            first, last = int(float(r.get("first") or 0)) / 1e9, int(float(r.get("last") or 0)) / 1e9
            f = r.get("_field")
            if f == "score":
                a["evaluations"] += int(float(r.get("n") or 0))
                a["max_abs_score"] = max(a["max_abs_score"], float(r.get("maxabs") or 0))
                a["first"] = first if a["first"] is None else min(a["first"], first)
                a["last"] = last if a["last"] is None else max(a["last"], last)
            elif f == "value":
                a["last_value"] = to_float(r.get("lastv"))
            elif f == "baseline":
                a["last_baseline"] = to_float(r.get("lastv"))
        items = []
        for a in agg.values():
            sig = self.cfg.signal(a["signal"])
            a["severity"] = checks.worst(s for s in a.pop("severities") if s in checks.RANK)
            a["description"] = sig.description if sig else ""
            a["label"] = sig.labels.get(a["series"], a["series"]) if sig else a["series"]
            a["approx_minutes"] = round(a["evaluations"] * self.cfg.cycle_seconds / 60)
            for k in ("first", "last"):
                a[k] = dt.datetime.fromtimestamp(a[k], self.tz).isoformat(timespec="minutes") if a[k] else None
            items.append(a)
        items.sort(key=lambda a: (a["suppressed"], -checks.RANK[a["severity"]], -a["evaluations"]))
        st = self.status(now)
        return {"window_hours": hours, "generated_at": st["generated_at"], "healthy": st["healthy"],
                "items": items, "active_now": st["anomalies"], "gone_quiet": st["gone_quiet"],
                "errors": st["errors"],
                "suppressions_expiring_7d": [s for s in st["suppressions"]["active"]
                                             if (dt.date.fromisoformat(s["until"])
                                                 - dt.datetime.fromtimestamp(now, self.tz).date()).days <= 7]}

    # -------------------------------------------------------------- context
    def context(self, now=None):
        now = now or self.clock()
        out = []
        for item in self.cfg.context:
            entry = {"name": item["name"], "description": item.get("description", "")}
            try:
                rows = self.influx.query(flux.context_query(item, now - _secs(item.get("lookback", "1d")), now))
                entry["values"] = [{"time": dt.datetime.fromtimestamp(parse_time(r["_time"]), self.tz)
                                    .isoformat(timespec="minutes") if parse_time(r.get("_time")) else None,
                                    "entity": r.get("entity_id") or r.get("_measurement"),
                                    "value": r.get("_value")} for r in rows]
            except InfluxError as e:
                entry["error"] = str(e)[:200]
            out.append(entry)
        return {"generated_at": dt.datetime.fromtimestamp(now, self.tz).isoformat(timespec="seconds"),
                "context": out}


def _secs(v):
    from .config import duration
    return duration(v)
