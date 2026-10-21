"""Check types. Pure functions over plain numbers, shared by the live loop and
the backtest. No I/O here.

Every check returns a Result with a severity of ok | moderate | severe.
"""
import math
import statistics
from dataclasses import dataclass, asdict

RANK = {"ok": 0, "moderate": 1, "severe": 2}
Z_CAP = 99.0
MAD_TO_SIGMA = 1.4826


@dataclass
class Result:
    check: str
    severity: str = "ok"
    score: float = 0.0
    value: float | None = None
    baseline: float | None = None
    # ok | anomaly | no_data | insufficient_history | skipped
    status: str = "ok"
    detail: str = ""

    @property
    def anomalous(self):
        return self.severity != "ok"

    def to_dict(self):
        d = asdict(self)
        for k in ("score", "value", "baseline"):
            if isinstance(d[k], float):
                d[k] = round(d[k], 4)
        return d


def worst(severities):
    best = "ok"
    for s in severities:
        if RANK[s] > RANK[best]:
            best = s
    return best


def cap(severity, max_severity):
    return severity if RANK[severity] <= RANK[max_severity] else max_severity


def grade(magnitude, moderate, severe):
    """Severity of a non-negative magnitude against two ascending limits."""
    if magnitude is None or (isinstance(magnitude, float) and math.isnan(magnitude)):
        return "ok"
    if severe is not None and magnitude >= severe:
        return "severe"
    if moderate is not None and magnitude >= moderate:
        return "moderate"
    return "ok"


def _clean(xs):
    return [float(x) for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]


def robust_z(current, history, min_scale=0.0, rel_scale=0.0):
    """Robust z-score of `current` against `history` (median / MAD).

    The scale is floored at `min_scale` and at `rel_scale * |median|`, so a
    perfectly flat history (MAD 0) does not turn every wiggle into z=inf.
    Returns (z, median, scale).
    """
    med = statistics.median(history)
    mad = statistics.median([abs(x - med) for x in history])
    scale = max(MAD_TO_SIGMA * mad, min_scale, rel_scale * abs(med))
    delta = current - med
    if scale <= 0:
        z = 0.0 if delta == 0 else math.copysign(Z_CAP, delta)
    else:
        z = delta / scale
    return max(-Z_CAP, min(Z_CAP, z)), med, scale


def directional(z, direction):
    if direction == "up":
        return max(z, 0.0)
    if direction == "down":
        return max(-z, 0.0)
    return abs(z)


def seasonal(current, history, p):
    """Current window vs the same hour-of-week over the last N weeks.

    history: values for the same window 1..N weeks ago (None where missing).
    params: min_weeks, z_moderate, z_severe, direction, min_scale, rel_scale,
            min_abs_delta.
    """
    if current is None:
        return Result("seasonal", status="no_data")
    hist = _clean(history)
    if len(hist) < p["min_weeks"]:
        return Result("seasonal", status="insufficient_history", value=current,
                      detail=f"{len(hist)}/{p['min_weeks']} weeks of history")
    z, med, scale = robust_z(current, hist, p.get("min_scale", 0.0), p.get("rel_scale", 0.0))
    mag = directional(z, p.get("direction", "both"))
    if abs(current - med) < p.get("min_abs_delta", 0.0):
        mag = 0.0
    sev = grade(mag, p["z_moderate"], p["z_severe"])
    return Result("seasonal", severity=sev, score=z, value=current, baseline=med,
                  status="anomaly" if sev != "ok" else "ok",
                  detail=f"z={z:.1f} vs median {med:.3g} (scale {scale:.3g}, {len(hist)} weeks)")


def rate(values, p):
    """Change between the last two buckets vs the typical bucket-to-bucket change.

    values: bucket values in time order (complete buckets only).
    params: min_points, z_moderate, z_severe, direction, min_scale, rel_scale,
            max_change: {moderate, severe} (absolute, optional).
    """
    vals = _clean(values)
    if len(vals) < 2:
        return Result("rate", status="no_data")
    diffs = [b - a for a, b in zip(vals, vals[1:])]
    cur, hist = diffs[-1], diffs[:-1]
    if len(hist) < p["min_points"]:
        return Result("rate", status="insufficient_history", value=cur)
    z, med, _ = robust_z(cur, hist, p.get("min_scale", 0.0), p.get("rel_scale", 0.0))
    direction = p.get("direction", "both")
    sev = grade(directional(z, direction), p["z_moderate"], p["z_severe"])
    mc = p.get("max_change") or {}
    if mc:
        mag = {"up": max(cur, 0.0), "down": max(-cur, 0.0)}.get(direction, abs(cur))
        sev = worst([sev, grade(mag, mc.get("moderate"), mc.get("severe"))])
    return Result("rate", severity=sev, score=z, value=cur, baseline=med,
                  status="anomaly" if sev != "ok" else "ok",
                  detail=f"change {cur:.3g} per bucket, typical {med:.3g} (z={z:.1f})")


def stale(age_seconds, p):
    """No data (or a device's own last-seen timestamp) older than expected.

    age_seconds: seconds since the newest point, or None when nothing was
    found in the lookback (treated as at least the lookback).
    params: max_age: {moderate, severe} in seconds, lookback (seconds).
    """
    if age_seconds is None:
        age_seconds = float(p["lookback"])
        detail = "no data in lookback"
    else:
        detail = ""
    lim = p["max_age"]
    sev = grade(age_seconds, lim.get("moderate"), lim.get("severe"))
    expected = lim.get("moderate") or lim.get("severe")
    hours = age_seconds / 3600.0
    return Result("stale", severity=sev, score=age_seconds / expected if expected else 0.0,
                  value=hours, baseline=expected / 3600.0 if expected else None,
                  status="anomaly" if sev != "ok" else "ok",
                  detail=detail or f"last seen {_fmt_age(age_seconds)} ago")


def drift(points, p):
    """Least-squares slope (units per day) over a trailing window.

    points: [(epoch_seconds, value)] in time order, typically daily buckets.
    params: min_points, slope: {moderate, severe}, direction (default up),
            min_total_change (optional).
    """
    pts = [(t, float(v)) for t, v in points if v is not None]
    if len(pts) < p["min_points"]:
        return Result("drift", status="insufficient_history",
                      detail=f"{len(pts)}/{p['min_points']} points")
    t0 = pts[0][0]
    xs = [(t - t0) / 86400.0 for t, _ in pts]
    ys = [v for _, v in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = 0.0 if sxx == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    total = ys[-1] - ys[0]
    direction = p.get("direction", "up")
    mag = directional(slope, direction)
    if abs(total) < p.get("min_total_change", 0.0):
        mag = 0.0
    lim = p["slope"]
    sev = grade(mag, lim.get("moderate"), lim.get("severe"))
    ref = lim.get("moderate") or lim.get("severe")
    return Result("drift", severity=sev, score=slope / ref if ref else slope, value=slope,
                  baseline=ys[0], status="anomaly" if sev != "ok" else "ok",
                  detail=f"slope {slope:.3g}/day over {xs[-1]:.0f}d, {ys[0]:.3g} -> {ys[-1]:.3g}")


def threshold(value, p):
    """Simple bounds. params: above / below: {moderate, severe}."""
    if value is None:
        return Result("threshold", status="no_data")
    sev, limit = "ok", None
    above, below = p.get("above") or {}, p.get("below") or {}
    for level in ("moderate", "severe"):
        if above.get(level) is not None and value >= above[level]:
            sev, limit = level, above[level]
        if below.get(level) is not None and value <= below[level]:
            sev, limit = level, below[level]
    if limit is None:
        limit = above.get("moderate", below.get("moderate"))
    return Result("threshold", severity=sev, score=(value - limit) if limit is not None else 0.0,
                  value=value, baseline=limit, status="anomaly" if sev != "ok" else "ok",
                  detail=f"value {value:.4g} vs limit {limit}" if limit is not None else "")


def _fmt_age(seconds):
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.0f}m"
