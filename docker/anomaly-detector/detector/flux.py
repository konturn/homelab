"""Flux query builders. Every query is bounded (explicit start/stop) and
aggregated server-side, so a cycle never pulls raw points.

A source is either structured (bucket / measurement / field / tags) or a raw
`flux` template with {start} {stop} {every} {fn} placeholders that returns
_time/_value plus the signal's group_by columns.
"""
import datetime as dt
import json

from .config import duration

AGG_FLUX = {"mean": "mean", "max": "max", "min": "min", "sum": "sum", "last": "last",
            "count": "count", "median": "median"}


def ts(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dur(seconds):
    seconds = int(seconds)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % n == 0:
            return f"{seconds // n}{unit}"
    return f"{seconds}s"


def _s(v):
    return json.dumps(str(v), ensure_ascii=False)


def _tag_pred(k, v):
    return f"r[{_s(k)}] =~ {v}" if _is_re(v) else f"r[{_s(k)}] == {_s(v)}"


def _is_re(v):
    v = str(v)
    return len(v) > 2 and v.startswith("/") and v.endswith("/")


def _pred(col, v):
    return f"r.{col} =~ {v}" if _is_re(v) else f"r.{col} == {_s(v)}"


def _filters(src):
    mpred, fpred = _pred("_measurement", src["measurement"]), _pred("_field", src["field"])
    out = [f"  |> filter(fn: (r) => {mpred} and {fpred})"]
    tags = src.get("tags") or {}
    if tags:
        out.append("  |> filter(fn: (r) => " + " and ".join(_tag_pred(k, v) for k, v in tags.items()) + ")")
    return out


def _value_stages(src):
    out = []
    vmap = src.get("value_map")
    if vmap:
        default = float(vmap.get("*", 0))
        expr = f"{default}"
        for k, v in vmap.items():
            if k != "*":
                expr = f"if string(v: r._value) == {_s(k)} then {float(v)} else {expr}"
        out.append(f"  |> map(fn: (r) => ({{r with _value: {expr}}}))")
    elif src.get("to_float", True) and src.get("agg") != "count":
        out.append("  |> toFloat()")
    if src.get("derivative"):
        out.append("  |> derivative(unit: 1s, nonNegative: true)")
    return out


def _group(group_by):
    cols = ", ".join(_s(c) for c in group_by)
    return f"  |> group(columns: [{cols}])"


def _keep(group_by, extra=()):
    cols = ", ".join(_s(c) for c in ["_time", "_value", *group_by, *extra])
    return f"  |> keep(columns: [{cols}])"


def _raw(src, start, stop, every, fn):
    return (src["flux"].replace("{start}", ts(start)).replace("{stop}", ts(stop))
            .replace("{every}", dur(every)).replace("{fn}", AGG_FLUX[fn]))


def _base(src, start, stop, group_by, step_seed=0):
    """Filtered stream, mapped to floats, grouped by group_by.

    State sources (HA entities that only write on change) set state_step: the
    series is forward-filled on a fixed grid, seeded from `step_seed` seconds
    before start, so a window with no change still has a value.
    """
    step = duration(src["state_step"]) if src.get("state_step") else 0
    seed = step_seed if step else 0
    lines = [f'from(bucket: {_s(src["bucket"])})',
             f"  |> range(start: {ts(start - seed)}, stop: {ts(stop)})"]
    lines += _filters(src)
    lines += _value_stages(src)
    if step:
        lines.append(_group(group_by))
        lines.append(f"  |> aggregateWindow(every: {dur(step)}, fn: last, createEmpty: true)")
        lines.append("  |> fill(usePrevious: true)")
        lines.append(f"  |> filter(fn: (r) => exists r._value and r._time > {ts(start)})")
    else:
        lines.append(_group(group_by))
    return lines


def window_query(src, group_by, start, stop, fn=None, seed=86400):
    """One aggregated value per group over [start, stop)."""
    fn = fn or src.get("agg", "mean")
    if "flux" in src:
        return _raw(src, start, stop, stop - start, fn)
    lines = _base(src, start, stop, group_by, seed)
    if fn in ("last",):
        lines.append('  |> sort(columns: ["_time"])')
    lines.append(f"  |> {AGG_FLUX[fn]}()")
    if fn not in ("last",):
        lines.append(f"  |> map(fn: (r) => ({{r with _time: {ts(stop)}}}))")
    lines.append(_keep(group_by))
    return "\n".join(lines)


def series_query(src, group_by, start, stop, every, fn=None, seed=86400, create_empty=False):
    """Values per group in `every` buckets over [start, stop)."""
    fn = fn or src.get("agg", "mean")
    if "flux" in src:
        return _raw(src, start, stop, every, fn)
    lines = _base(src, start, stop, group_by, seed)
    if fn in ("last",):
        lines.append('  |> sort(columns: ["_time"])')
    ce = "true" if create_empty else "false"
    lines.append(f"  |> aggregateWindow(every: {dur(every)}, fn: {AGG_FLUX[fn]}, createEmpty: {ce})")
    lines.append(_keep(group_by))
    return "\n".join(lines)


def union_windows(src, group_by, windows, fn=None, seed=86400):
    """Several [start, stop) windows in one request, tagged _k=index.

    Used by the seasonal check: the current hour plus the same hour 1..N
    weeks back, i.e. N+1 one-hour scans instead of one N-week scan.
    """
    parts, names = [], []
    for i, (start, stop) in enumerate(windows):
        q = window_query(src, group_by, start, stop, fn, seed)
        parts.append(f"w{i} = {q}\n  |> set(key: \"_k\", value: \"{i}\")")
        names.append(f"w{i}")
    return "\n".join(parts) + f"\nunion(tables: [{', '.join(names)}])\n" + _keep(group_by, ["_k"])


def last_query(src, group_by, start, stop):
    """Newest point per raw series (string values kept, for age_from: value)."""
    if "flux" in src:
        return _raw(src, start, stop, stop - start, "last")
    lines = [f'from(bucket: {_s(src["bucket"])})',
             f"  |> range(start: {ts(start)}, stop: {ts(stop)})"]
    lines += _filters(src)
    lines.append("  |> last()")
    lines.append(_keep(group_by))
    return "\n".join(lines)


def context_query(item, start, stop):
    """Context for triage: newest value (mode last) or recent changes (mode changes)."""
    src = item["source"]
    lines = [f'from(bucket: {_s(src["bucket"])})',
             f"  |> range(start: {ts(start)}, stop: {ts(stop)})"]
    lines += _filters(src)
    if item.get("mode", "last") == "last":
        lines.append("  |> last()")
    else:
        lines.append("  |> group()")
        lines.append('  |> sort(columns: ["_time"], desc: true)')
        lines.append(f"  |> limit(n: {int(item.get('limit', 20))})")
    cols = ["_time", "_value", "_measurement", *(item.get("columns") or [])]
    lines.append("  |> keep(columns: [" + ", ".join(_s(c) for c in cols) + "])")
    return "\n".join(lines)


def anomaly_digest_query(bucket, start, stop):
    """Non-ok anomaly evaluations in a range, aggregated per signal/check/series."""
    return "\n".join([
        'import "math"',
        f"from(bucket: {_s(bucket)})",
        f"  |> range(start: {ts(start)}, stop: {ts(stop)})",
        '  |> filter(fn: (r) => r._measurement == "anomaly" and r.severity != "ok")',
        '  |> filter(fn: (r) => r._field == "score" or r._field == "value" or r._field == "baseline")',
        '  |> group(columns: ["signal", "check", "series", "severity", "suppressed", "_field"])',
        "  |> reduce(identity: {n: 0, first: 0, last: 0, lastv: 0.0, maxabs: 0.0},",
        "       fn: (r, accumulator) => ({",
        "         n: accumulator.n + 1,",
        "         first: if accumulator.n == 0 then int(v: r._time) else accumulator.first,",
        "         last: int(v: r._time),",
        "         lastv: r._value,",
        "         maxabs: if math.abs(x: r._value) > accumulator.maxabs then math.abs(x: r._value) else accumulator.maxabs}))",
    ])
