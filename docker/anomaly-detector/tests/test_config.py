"""The repo's config.yml and suppressions.yml, config validation, Flux builders."""
import datetime as dt
import os
import unittest

import yaml

from detector import flux
from detector.config import ConfigError, duration, load_config, parse_config, parse_suppressions
from detector.influx import line, parse_csv, parse_time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sig(**kw):
    s = {"name": "x_signal", "description": "d", "owner": "o", "category": "infrastructure",
         "source": {"bucket": "metrics", "measurement": "m", "field": "f"},
         "checks": [{"type": "threshold", "above": {"moderate": 1}}]}
    s.update(kw)
    return {"signals": [s]}


class RepoFilesTest(unittest.TestCase):
    def test_config_parses(self):
        cfg = load_config(os.path.join(HERE, "config.yml"))
        self.assertGreaterEqual(len(cfg.signals), 15)
        cats = {s.category for s in cfg.signals}
        self.assertTrue({"gone_quiet", "physical", "infrastructure", "pattern"} <= cats)
        for s in cfg.signals:
            for c in s.checks:
                if c.type == "seasonal":
                    self.assertGreaterEqual(c.params["min_weeks"], 3, s.name)
                self.assertGreaterEqual(c.interval, 300, f"{s.name}: interval below the cycle")

    def test_config_queries_build(self):
        cfg = load_config(os.path.join(HERE, "config.yml"))
        for s in cfg.signals:
            q = flux.window_query(s.source, s.group_by, 1000000, 1003600)
            self.assertIn("range(start: ", q)
            for item in cfg.context:
                flux.context_query(item, 1000000, 1003600)

    def test_suppressions_parse_strict(self):
        with open(os.path.join(HERE, "suppressions.yml")) as f:
            doc = yaml.safe_load(f)
        sups = parse_suppressions(doc, dt.date.today(), strict=True)
        cfg = load_config(os.path.join(HERE, "config.yml"))
        names = {s.name for s in cfg.signals}
        for s in sups:
            self.assertTrue(any(__import__("fnmatch").fnmatchcase(n, s.signal) for n in names),
                            f"suppression for unknown signal {s.signal}")


class ValidationTest(unittest.TestCase):
    def test_minimal(self):
        cfg = parse_config(sig())
        c = cfg.signals[0].checks[0]
        self.assertEqual((c.interval, c.for_cycles), (300, 2))

    def test_errors(self):
        bad = [
            sig(name="Bad-Name"),
            sig(description=""),
            sig(source={"bucket": "metrics", "measurement": "m"}),
            sig(source={"flux": "from(bucket: \"x\")"}),
            sig(checks=[]),
            sig(checks=[{"type": "nope"}]),
            sig(checks=[{"type": "stale"}]),
            sig(checks=[{"type": "drift"}]),
            sig(checks=[{"type": "threshold"}]),
            sig(checks=[{"type": "seasonal", "weeks": 2, "min_weeks": 3}]),
            sig(checks=[{"type": "threshold", "above": {"moderate": 1}, "when_hours": [1]}]),
            sig(checks=[{"type": "threshold", "above": {"moderate": 1}},
                        {"type": "threshold", "below": {"moderate": 1}}]),
            sig(max_severity="critical"),
            {"signals": [sig()["signals"][0], sig()["signals"][0]]},
        ]
        for doc in bad:
            with self.assertRaises(ConfigError, msg=str(doc)):
                parse_config(doc)

    def test_durations(self):
        self.assertEqual([duration(x) for x in ("30s", "15m", "2h", "7d", "1w", 90)],
                         [30, 900, 7200, 604800, 604800, 90])
        with self.assertRaises(ConfigError):
            duration("15 minutes")

    def test_suppression_rules(self):
        today = dt.date(2026, 10, 21)
        ok = {"suppressions": [{"signal": "x_*", "reason": "r", "until": "2026-11-01"}]}
        s = parse_suppressions(ok, today, strict=True)[0]
        self.assertTrue(s.matches("x_signal", "threshold", "anything"))
        self.assertTrue(s.active(today))
        self.assertFalse(s.active(dt.date(2026, 11, 2)))
        for bad in ({"signal": "x", "until": "2026-11-01"},
                    {"signal": "x", "reason": "r"},
                    {"signal": "x", "reason": "r", "until": "soon"},
                    {"signal": "x", "reason": "r", "until": "2030-01-01"}):
            with self.assertRaises(ConfigError, msg=str(bad)):
                parse_suppressions({"suppressions": [bad]}, today, strict=True)


class FluxTest(unittest.TestCase):
    src = {"bucket": "homeassistant", "measurement": "%", "field": "value",
           "tags": {"entity_id": "a", "host": "/^r/"}}

    def test_window_query(self):
        q = flux.window_query(self.src, ["entity_id"], 0, 3600, "mean")
        self.assertIn('range(start: 1970-01-01T00:00:00Z, stop: 1970-01-01T01:00:00Z)', q)
        self.assertIn('r["entity_id"] == "a"', q)
        self.assertIn('r["host"] =~ /^r/', q)
        self.assertIn("|> mean()", q)
        self.assertIn('group(columns: ["entity_id"])', q)

    def test_state_step_seeds_before_start(self):
        q = flux.window_query(dict(self.src, state_step="10m"), [], 86400 * 2, 86400 * 2 + 3600,
                              seed=86400)
        self.assertIn("range(start: 1970-01-02T00:00:00Z", q)
        self.assertIn("fill(usePrevious: true)", q)
        self.assertIn("r._time > 1970-01-03T00:00:00Z", q)

    def test_value_map(self):
        q = flux.series_query(dict(self.src, value_map={"heating": 1, "*": 0}), [], 0, 3600, 300)
        self.assertIn('if string(v: r._value) == "heating" then 1.0 else 0.0', q)
        self.assertNotIn("toFloat", q)

    def test_union(self):
        q = flux.union_windows(self.src, [], [(0, 3600), (604800, 608400)])
        self.assertIn("union(tables: [w0, w1])", q)
        self.assertEqual(q.count('set(key: "_k"'), 2)

    def test_raw_placeholders(self):
        raw = {"flux": 'from(bucket: "m") |> range(start: {start}, stop: {stop}) |> aggregateWindow(every: {every}, fn: {fn})'}
        q = flux.series_query(raw, [], 0, 7200, 3600, "sum")
        self.assertIn("every: 1h, fn: sum", q)


class InfluxIOTest(unittest.TestCase):
    def test_parse_csv_multiple_tables(self):
        text = (",result,table,_time,_value,host\r\n,_result,0,2026-10-21T04:00:00Z,1.5,router\r\n"
                "\r\n,result,table,_time,_value,_k\r\n,_result,0,2026-10-21T04:00:00.123456789Z,2,0\r\n")
        rows = parse_csv(text)
        self.assertEqual(rows[0], {"_time": "2026-10-21T04:00:00Z", "_value": "1.5", "host": "router"})
        self.assertEqual(rows[1]["_k"], "0")
        self.assertAlmostEqual(parse_time(rows[1]["_time"]) % 60, 0.123456, places=5)
        self.assertEqual(parse_time("2026-09-04T16:41:06+00:00"), 1788540066.0)

    def test_line_protocol(self):
        l = line("anomaly", {"signal": "a b", "series": ""}, {"score": 1.5, "n": 2, "x": None,
                                                              "bad": float("nan")}, 10.7)
        self.assertEqual(l, "anomaly,series=-,signal=a\\ b score=1.5,n=2i 10")


if __name__ == "__main__":
    unittest.main()
