"""Backtest: replay every check over the last N days and count what it would
have flagged. One-off and manual (`python -m detector backtest --days 21`).

Data is fetched as server-side aggregates in 7-day chunks (hourly buckets for
seasonal, window-sized buckets for threshold, ...), then the same check
functions as the live loop run in memory.
"""
import datetime as dt
import logging
import math
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from . import checks, flux
from .engine import WEEK, in_hours, series_key
from .influx import parse_time, to_float

CHUNK = 7 * 86400
log = logging.getLogger(__name__)


def fetch(influx, src, gb, start, stop, every, fn, create_empty=False, raw_values=False):
    """{series: {bucket_epoch: value}} over [start, stop), chunked."""
    out = defaultdict(dict)
    a, stop = (int(start) // every) * every, int(stop)
    while a < stop:
        b = min(a + max(CHUNK // every, 1) * every, stop)
        q = flux.series_query(src, gb, a, b, every, fn, create_empty=create_empty)
        for row in influx.query(q):
            t = parse_time(row.get("_time"))
            if t is None:
                continue
            v = row.get("_value") if raw_values else to_float(row.get("_value"))
            out[series_key(row, gb)][int(round(t))] = v
        a = b
    return out


def _evals_seasonal(influx, sig, chk, start, stop, tz):
    p, w = chk.params, chk.params["window"]
    fn = p.get("agg", sig.source.get("agg", "mean"))
    data = fetch(influx, sig.source, sig.group_by, start - p["weeks"] * WEEK - w, stop, w, fn)
    for s, vals in data.items():
        t = (int(start) // w + 1) * w
        while t <= stop:
            hist = [vals.get(t - k * WEEK) for k in range(1, p["weeks"] + 1)]
            yield s, t, checks.seasonal(vals.get(t), hist, p)
            t += w


def _evals_threshold(influx, sig, chk, start, stop, tz):
    p, w = chk.params, chk.params["window"]
    fn = p.get("agg", sig.source.get("agg", "mean"))
    data = fetch(influx, sig.source, sig.group_by, start, stop, w, fn)
    for s, vals in data.items():
        for t in sorted(vals):
            yield s, t, checks.threshold(vals[t], p)


def _evals_rate(influx, sig, chk, start, stop, tz):
    p, e = chk.params, chk.params["every"]
    n = p["lookback"] // e
    data = fetch(influx, sig.source, sig.group_by, start - p["lookback"], stop, e,
                 sig.source.get("agg", "mean"))
    for s, vals in data.items():
        times = sorted(vals)
        for i, t in enumerate(times):
            if t < start:
                continue
            window = [vals[x] for x in times[max(0, i - n):i + 1] if x > t - p["lookback"] - e]
            yield s, t, checks.rate(window, p)


def _evals_drift(influx, sig, chk, start, stop, tz):
    p, e = chk.params, chk.params["every"]
    fn = p.get("agg", sig.source.get("agg", "mean"))
    data = fetch(influx, sig.source, sig.group_by, start - p["days"] * 86400, stop, e, fn)
    for s, vals in data.items():
        times = sorted(vals)
        for t in times:
            if t < start:
                continue
            pts = [(x, vals[x]) for x in times if t - p["days"] * 86400 < x <= t]
            yield s, t, checks.drift(pts, p)


def _evals_stale(influx, sig, chk, start, stop, tz):
    p = chk.params
    step = max(chk.interval, 300)
    if p["age_from"] == "value":
        step = 3600
        data = fetch(influx, sig.source, sig.group_by, start - p["lookback"], stop, step, "last",
                     raw_values=True)
        for s, vals in data.items():
            times = sorted(vals)
            newest, j = None, 0
            t = (int(start) // step + 1) * step
            while t <= stop:
                while j < len(times) and times[j] <= t:
                    seen = parse_time(vals[times[j]])
                    if seen is not None:
                        newest = max(newest or 0, seen)
                    j += 1
                age = None if newest is None or t - newest > p["lookback"] else max(0.0, t - newest)
                yield s, t, checks.stale(age, p)
                t += step
        return
    data = fetch(influx, sig.source, sig.group_by, start - p["lookback"], stop, step, "count",
                 create_empty=True)
    for s, vals in data.items():
        last_seen = None
        for t in sorted(vals):
            if (vals[t] or 0) > 0:
                last_seen = t
            if t < start:
                continue
            age = None if last_seen is None or t - last_seen > p["lookback"] else float(t - last_seen)
            yield s, t, checks.stale(age, p)


EVALS = {"seasonal": _evals_seasonal, "threshold": _evals_threshold, "rate": _evals_rate,
         "drift": _evals_drift, "stale": _evals_stale}


def episodes(timeline, for_buckets):
    """timeline: [(t, severity)] sorted. Debounced runs of non-ok -> list of
    (start, end, worst severity)."""
    out, run, streak = [], None, 0
    for t, sev in timeline:
        if sev != "ok":
            streak += 1
            if streak >= for_buckets:
                if run is None:
                    run = [t, t, sev]
                run[1] = t
                run[2] = checks.worst([run[2], sev])
        else:
            streak = 0
            if run:
                out.append(tuple(run))
                run = None
    if run:
        out.append(tuple(run))
    return out


def bucket_seconds(chk):
    p = chk.params
    if chk.type in ("seasonal", "threshold"):
        return p["window"]
    if chk.type in ("rate", "drift"):
        return p["every"]
    return 3600 if p.get("age_from") == "value" else max(chk.interval, 300)


def run(cfg, influx, days, now, suppressions=(), only=None):
    tz = ZoneInfo(cfg.timezone)
    start = now - days * 86400
    today = dt.datetime.fromtimestamp(now, tz).date()
    report = []
    for sig in cfg.signals:
        if only and sig.name not in only:
            continue
        for chk in sig.checks:
            row = {"signal": sig.name, "check": chk.id, "type": chk.type, "series": 0,
                   "episodes": 0, "severe": 0, "moderate": 0, "suppressed": 0,
                   "anomalous_evals": 0, "evals": 0, "insufficient": 0, "error": None, "top": []}
            t0, q0 = time.monotonic(), influx.queries
            try:
                timelines = defaultdict(list)
                for s, t, r in EVALS[chk.type](influx, sig, chk, start, now, tz):
                    if not in_hours(t, chk.when_hours, tz):
                        r = checks.Result(chk.type, status="skipped")
                    sev = checks.cap(checks.cap(r.severity, chk.max_severity), sig.max_severity)
                    timelines[s].append((t, sev))
                    row["evals"] += 1
                    row["anomalous_evals"] += sev != "ok"
                    row["insufficient"] += r.status == "insufficient_history"
                bsec = bucket_seconds(chk)
                fb = max(1, math.ceil(chk.for_cycles * chk.interval / bsec)) if chk.for_cycles > 1 else 1
                per_series = []
                for s, tl in timelines.items():
                    eps = episodes(sorted(tl), fb)
                    sup = next((x for x in suppressions if x.active(today) and x.matches(sig.name, chk.id, s)), None)
                    if sup:
                        row["suppressed"] += len(eps)
                        continue
                    row["episodes"] += len(eps)
                    row["severe"] += sum(1 for e in eps if e[2] == "severe")
                    row["moderate"] += sum(1 for e in eps if e[2] == "moderate")
                    if eps:
                        per_series.append((len(eps), sig.labels.get(s, s)))
                row["series"] = len(timelines)
                row["top"] = [f"{name or '-'}:{n}" for n, name in sorted(per_series, reverse=True)[:5]]
            except Exception as e:  # report and continue with the next check
                row["error"] = str(e)[:200]
            row["seconds"] = round(time.monotonic() - t0, 1)
            row["queries"] = influx.queries - q0
            log.info("%s/%s: %d episodes (%d severe), %d series, %d queries, %.1fs%s", sig.name, chk.id,
                     row["episodes"], row["severe"], row["series"], row["queries"], row["seconds"],
                     f" ERROR {row['error']}" if row["error"] else f" top {row['top']}")
            row["per_day"] = round(row["episodes"] / days, 2)
            report.append(row)
    return report


def format_report(report, days):
    lines = [f"Backtest over {days} days (episodes = debounced anomaly runs; suppressed excluded)",
             f"{'signal':28} {'check':10} {'series':>6} {'episodes':>8} {'/day':>6} {'severe':>6} "
             f"{'moderate':>8} {'supp':>5}  top series"]
    total = 0
    for r in report:
        if r["error"]:
            lines.append(f"{r['signal']:28} {r['check']:10} ERROR {r['error']}")
            continue
        total += r["episodes"]
        lines.append(f"{r['signal']:28} {r['check']:10} {r['series']:>6} {r['episodes']:>8} "
                     f"{r['per_day']:>6} {r['severe']:>6} {r['moderate']:>8} {r['suppressed']:>5}  "
                     f"{' '.join(r['top'])}")
    lines.append(f"TOTAL episodes {total} = {total / days:.2f}/day")
    return "\n".join(lines)
