"""Engine cycle with a fake Influx: debounce, suppressions, outputs."""
import datetime as dt
import json
import os
import tempfile
import unittest

from detector import mqtt
from detector.backtest import episodes
from detector.config import parse_config
from detector.engine import Engine, in_hours
from detector.flux import ts

NOW = 1792500000  # 2026-10-21T...Z


class FakeInflux:
    def __init__(self, responder):
        self.responder = responder
        self.queries = 0
        self.query_seconds = 0.0
        self.last_error = None
        self.written = []
        self.log = []

    def query(self, q):
        self.queries += 1
        self.log.append(q)
        return self.responder(q)

    def write(self, bucket, lines):
        self.written.append((bucket, list(lines)))


CFG = {
    "defaults": {"cycle": "5m"},
    "signals": [
        {"name": "load", "description": "d", "owner": "o", "category": "infrastructure",
         "source": {"bucket": "metrics", "measurement": "snmp", "field": "load"},
         "checks": [{"type": "seasonal"}]},
        {"name": "hosts", "description": "d", "owner": "o", "category": "gone_quiet",
         "source": {"bucket": "metrics", "measurement": "system", "field": "uptime"},
         "group_by": ["host"],
         "checks": [{"type": "stale", "max_age": {"moderate": "10m", "severe": "30m"}}]},
        {"name": "temp", "description": "d", "owner": "o", "category": "physical",
         "source": {"bucket": "homeassistant", "measurement": "F", "field": "value"},
         "checks": [{"type": "threshold", "below": {"severe": 45}, "for_cycles": 2}]},
    ],
}


def responder(state):
    def r(q):
        if "snmp" in q:
            vals = {"0": state.get("load", 50.0), "1": 50.0, "2": 51.0, "3": 49.0, "4": 50.0}
            return [{"_k": k, "_value": str(v), "_time": ts(NOW)} for k, v in vals.items()]
        if "system" in q:
            return [{"host": "router", "_time": ts(NOW - 60), "_value": "1"},
                    {"host": "zwave", "_time": ts(NOW - state.get("zwave_age", 60)), "_value": "1"}]
        if '"F"' in q:
            return [{"_time": ts(NOW), "_value": str(state.get("temp", 60.0))}]
        return []
    return r


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config(CFG)
        self.state = {}
        self.influx = FakeInflux(responder(self.state))
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        self.tmp.write("suppressions: []\n")
        self.tmp.close()
        self.engine = Engine(self.cfg, self.influx, self.tmp.name, clock=lambda: NOW)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def write_suppressions(self, text):
        with open(self.tmp.name, "w") as f:
            f.write(text)

    def test_quiet_house(self):
        self.engine.run_cycle(NOW, force=True)
        st = self.engine.status(NOW)
        self.assertEqual(st["anomalies"], [])
        self.assertTrue(st["healthy"])
        json.dumps(st)

    def test_anomalies_and_outputs(self):
        self.state.update(load=500.0, zwave_age=3600)
        self.engine.run_cycle(NOW, force=True)
        st = self.engine.status(NOW)
        got = {(a["signal"], a["series"]): a["severity"] for a in st["anomalies"]}
        self.assertEqual(got, {("load", ""): "severe", ("hosts", "zwave"): "severe"})
        self.assertEqual([g["series"] for g in st["gone_quiet"]], ["zwave"])
        lines = self.influx.written[-1][1]
        self.assertTrue(any(l.startswith("anomaly,") and "signal=load" in l and "severity=severe" in l
                            and "series=-" in l and "anomalous=1i" in l for l in lines))
        self.assertTrue(any(l.startswith("anomaly_detector ") for l in lines))
        summary = self.engine.signal_summary()
        msgs = dict(mqtt.state_messages(summary))
        self.assertEqual(msgs["homelab/anomaly/load/state"], "severe")
        self.assertEqual(msgs["homelab/anomaly/house/count"], "2")

    def test_debounce_for_cycles(self):
        self.state["temp"] = 40.0
        self.engine.run_cycle(NOW, force=True)
        st = self.engine.status(NOW)
        self.assertEqual(st["anomalies"], [])
        self.assertEqual([p["signal"] for p in st["pending"]], ["temp"])
        self.engine.run_cycle(NOW + 300, force=True)
        self.assertEqual([a["signal"] for a in self.engine.status(NOW)["anomalies"]], ["temp"])

    def test_suppression_active_and_expired(self):
        self.state.update(zwave_age=3600)
        self.write_suppressions(
            "suppressions:\n"
            "  - {signal: hosts, series: zwave, reason: maintenance, until: 2099-01-01}\n")
        self.engine.run_cycle(NOW, force=True)
        st = self.engine.status(NOW)
        self.assertEqual(st["anomalies"], [])
        self.assertEqual(st["suppressed"][0]["suppressed"], "maintenance")
        self.assertIn("suppressed=true", " ".join(self.influx.written[-1][1]))
        self.assertEqual(mqtt.state_messages(self.engine.signal_summary())[-2][1], "0")
        self.write_suppressions(
            "suppressions:\n"
            "  - {signal: hosts, series: zwave, reason: maintenance, until: 2020-01-01}\n")
        self.engine.run_cycle(NOW + 300, force=True)
        st = self.engine.status(NOW + 300)
        self.assertEqual(len(st["anomalies"]), 1)
        self.assertEqual(len(st["suppressions"]["expired"]), 1)

    def test_vanished_series_goes_stale(self):
        self.engine.run_cycle(NOW, force=True)
        self.influx.responder = lambda q: [] if "system" in q else responder(self.state)(q)
        self.engine.run_cycle(NOW + 300, force=True)
        hosts = {a["series"] for a in self.engine.status(NOW)["anomalies"] if a["signal"] == "hosts"}
        self.assertEqual(hosts, {"router", "zwave"})

    def test_interval_throttles_queries(self):
        self.engine.run_cycle(NOW)
        n = self.influx.queries
        self.engine.run_cycle(NOW + 300)   # seasonal (1h) not due, others are
        self.assertEqual(self.influx.queries - n, 2)
        self.assertEqual(sum("union(" in q for q in self.influx.log), 1)

    def test_query_error_is_isolated(self):
        from detector.influx import InfluxError

        def boom(q):
            if "snmp" in q:
                raise InfluxError("HTTP 500")
            return responder(self.state)(q)
        self.influx.responder = boom
        self.engine.run_cycle(NOW, force=True)
        st = self.engine.status(NOW)
        self.assertEqual([e["signal"] for e in st["errors"]], ["load"])
        self.assertTrue(any(e.check.id == "stale" for e in self.engine.entries.values()))

    def test_digest_aggregates_rows(self):
        def r(q):
            base = {"signal": "hosts", "check": "stale", "series": "zwave", "severity": "severe",
                    "suppressed": "false", "first": str((NOW - 3600) * 10**9), "last": str(NOW * 10**9)}
            return [dict(base, _field="score", n="12", maxabs="6", lastv="6"),
                    dict(base, _field="value", n="12", maxabs="1", lastv="1.0"),
                    dict(base, _field="baseline", n="12", maxabs="0.1", lastv="0.16")]
        self.influx.responder = r
        d = self.engine.digest(24, NOW)
        item = d["items"][0]
        self.assertEqual((item["signal"], item["severity"], item["evaluations"]), ("hosts", "severe", 12))
        self.assertEqual(item["approx_minutes"], 60)
        self.assertEqual(item["last_value"], 1.0)
        json.dumps(d)


class HelpersTest(unittest.TestCase):
    def test_in_hours_wraps_midnight(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        at = lambda h: dt.datetime(2026, 10, 21, h, 30, tzinfo=tz).timestamp()
        self.assertTrue(in_hours(at(2), (1, 5), tz))
        self.assertFalse(in_hours(at(5), (1, 5), tz))
        self.assertTrue(in_hours(at(23), (22, 6), tz))
        self.assertFalse(in_hours(at(12), (22, 6), tz))

    def test_episodes_debounce(self):
        tl = [(0, "ok"), (1, "moderate"), (2, "ok"), (3, "moderate"), (4, "severe"), (5, "ok")]
        self.assertEqual(episodes(tl, 1), [(1, 1, "moderate"), (3, 4, "severe")])
        self.assertEqual(episodes(tl, 2), [(4, 4, "severe")])

    def test_discovery(self):
        msgs = dict(mqtt.discovery_messages(["ups_load"]))
        cfg = json.loads(msgs["homeassistant/sensor/anomaly_detector/ups_load/config"])
        self.assertEqual(cfg["state_topic"], "homelab/anomaly/ups_load/state")
        self.assertIn("homeassistant/sensor/anomaly_detector/house/config", msgs)


if __name__ == "__main__":
    unittest.main()
