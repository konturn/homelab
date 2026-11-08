"""Intake normalisation (4.3, 7.1) and the kill switch (7.3)."""

import os
import tempfile
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from router import modes
from router.events import BadPayload, from_grafana
from router.util import parse_duration

PAYLOAD = {"alerts": [{
    "status": "firing", "fingerprint": "382bc5973490818a",
    "labels": {"alertname": "Zigbee Radio Silence (no device messages)", "severity": "warning",
               "__alert_rule_uid__": "zigbee-radio-silence", "grafana_folder": "General Alerting"},
    "annotations": {"summary": "zigbee2mqtt has received no Zigbee messages"},
    "generatorURL": "https://grafana.lab.nkontur.com/alerting/grafana/zigbee-radio-silence/view",
    "startsAt": "2026-11-07T22:00:00Z"}]}


class TestEvents(unittest.TestCase):
    def test_grafana_payload(self):
        (ev,) = from_grafana(PAYLOAD)
        self.assertEqual(ev.fp, "grafana:382bc5973490818a")
        self.assertEqual(ev.rule_uid, "zigbee-radio-silence")
        self.assertNotIn("__alert_rule_uid__", ev.labels)
        self.assertEqual(ev.kind, "alert")

    def test_rule_uid_from_generator_url(self):
        p = {"alerts": [dict(PAYLOAD["alerts"][0], labels={"alertname": "x"})]}
        self.assertEqual(from_grafana(p)[0].rule_uid, "zigbee-radio-silence")

    def test_datasource_errors_are_their_own_kind(self):
        a = dict(PAYLOAD["alerts"][0], labels={"alertname": "DatasourceError", "__alert_rule_uid__": "zpool-unhealthy"})
        self.assertEqual(from_grafana({"alerts": [a]})[0].kind, "datasource_error")

    def test_bad_payloads(self):
        for p in (None, [], {"alerts": "x"}, "x"):
            with self.assertRaises(BadPayload):
                from_grafana(p)
        bad = dict(PAYLOAD["alerts"][0], fingerprint="../../x")
        self.assertEqual(from_grafana({"alerts": [bad]}), [])

    @given(st.recursive(st.none() | st.booleans() | st.integers() | st.text(max_size=50),
                        lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=10), c, max_size=4),
                        max_leaves=20))
    @settings(max_examples=300, deadline=None)
    def test_never_crashes_on_junk(self, junk):
        try:
            from_grafana({"alerts": junk if isinstance(junk, list) else [junk]})
        except BadPayload:
            pass


class TestModes(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(modes.effective_mode("active"), "active")
        self.assertEqual(modes.effective_mode("active", host="shadow"), "shadow")
        self.assertEqual(modes.effective_mode("shadow", host="active"), "active")   # host wins
        self.assertEqual(modes.effective_mode("active", runtime="notify-only"), "notify-only")
        self.assertEqual(modes.effective_mode("shadow", host="active", runtime="shadow"), "shadow")
        self.assertEqual(modes.effective_mode("bogus"), "notify-only")
        self.assertEqual(modes.effective_mode("active", runtime="active"), "notify-only")

    @given(st.sampled_from(modes.MODES), st.one_of(st.none(), st.sampled_from(modes.MODES)),
           st.one_of(st.none(), st.sampled_from(modes.RUNTIME_MODES)))
    def test_runtime_only_lowers(self, repo, host, runtime):
        without = modes.effective_mode(repo, host)
        with_rt = modes.effective_mode(repo, host, runtime)
        self.assertLessEqual(modes.MODES.index(with_rt), modes.MODES.index(without))

    def test_override_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mode")
            self.assertIsNone(modes.read_host_override(p))
            with open(p, "w") as f:
                f.write("garbage")
            self.assertEqual(modes.read_host_override(p), "notify-only")
            with open(p, "w") as f:
                f.write("shadow\n")
            self.assertEqual(modes.read_host_override(p), "shadow")

    def test_playbook_mode(self):
        self.assertEqual(modes.playbook_mode("active", "active"), "active")
        self.assertEqual(modes.playbook_mode("active", "shadow"), "shadow")
        self.assertEqual(modes.playbook_mode("shadow", "active"), "shadow")
        self.assertEqual(modes.playbook_mode("notify-only", "active"), "notify-only")

    def test_durations(self):
        self.assertEqual(parse_duration("6h"), 21600)
        for bad in ("6", "1w", "-1m", "", True):
            with self.assertRaises(ValueError):
                parse_duration(bad)


if __name__ == "__main__":
    unittest.main()
