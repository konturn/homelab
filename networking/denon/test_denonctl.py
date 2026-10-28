"""Offline tests for denonctl.py (no receiver needed)."""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import denonctl as d  # noqa: E402


def q(cmd):
    return next(x for x in d.QUERIES + d.RUNTIME if x.cmd == cmd)


class Levels(unittest.TestCase):
    def test_roundtrip(self):
        for raw, db in [("50", "0.0"), ("565", "+6.5"), ("54", "+4.0"),
                        ("465", "-3.5"), ("38", "-12.0"), ("62", "+12.0")]:
            self.assertEqual(d.level_to_db(raw), db)
            self.assertEqual(d.db_to_level(db), raw)

    def test_bad(self):
        with self.assertRaises(ValueError):
            d.level_to_db("ON")
        with self.assertRaises(ValueError):
            d.db_to_level("0.3")


class Parse(unittest.TestCase):
    def test_multi_with_events_and_padding(self):
        lines = ["SSFUNDVD DVD              ", "SSFUNTV TV Audio         ",
                 "SSFUNSAT/CBL Videocore        ", "SYSMI Stereo", "SSFUN END"]
        r = {}
        self.assertEqual(d.apply_query(r, q("SSFUN ?"), lines), [])
        self.assertEqual(r["sources"]["names"],
                         {"DVD": "DVD", "TV": "TV Audio", "SAT/CBL": "Videocore"})

    def test_multi_repeated_and_keyless(self):
        r = {}
        d.apply_query(r, q("SSSPC ?"), ["SSSPCSBK SMA", "SSSPCSBK 2SP", "SSSPCFRO SMA"])
        self.assertEqual(r["speakers"]["config"], {"SBK": "SMA,2SP", "FRO": "SMA"})
        r = {}
        d.apply_query(r, q("SSCFR ?"), ["SSCFRFRO 080", "SSCFR IDV", "SSCFRALL 250", "SSCFR END"])
        self.assertEqual(r["speakers"]["crossover"], {"FRO": "080", "_": "IDV", "ALL": "250"})

    def test_cv_decodes_and_ignores_trailer(self):
        r = {}
        d.apply_query(r, q("CV?"), ["CVFL 50", "CVSW 565", "CVEND", "MVMAX 90"])
        self.assertEqual(r["per_input"]["levels"]["channels_db"], {"FL": "0.0", "SW": "+6.5"})

    def test_cv_standby_is_missing(self):
        r = {}
        self.assertEqual(d.apply_query(r, q("CV?"), ["CVEND"]),
                         [("per_input", "levels", "channels_db")])

    def test_single_longer_prefix(self):
        r = {}
        d.apply_query(r, q("PSSWL ?"), ["PSSWL 51", "PSSWL2 50"])
        self.assertEqual(r["per_input"]["levels"], {"subwoofer_db": "+1.0", "subwoofer2_db": "0.0"})
        r = {}
        d.apply_query(r, q("MV?"), ["MV105", "MVMAX 90"])
        self.assertEqual(r, {"volume": "105", "volume_max": "90"})

    def test_sound_mode_skips_other_ms(self):
        r = {}
        d.apply_query(r, q("MS?"), ["MSMCH STEREO", "PSDRC OFF", "MSQUICK0"])
        self.assertEqual(r["per_input"]["sound_mode"], "MCH STEREO")

    def test_runtime_join(self):
        r = {}
        d.apply_query(r, q("Z2?"), ["Z2OFF", "Z2GAME", "Z241", "SVOFF"])
        self.assertEqual(r["zone2"], "OFF,GAME,41")

    def test_web_dedup_zone_and_quick_select(self):
        rx = ET.fromstring(
            "<rx><cmd><functionrename>"
            "<list><name>DVD</name><rename>DVD   </rename></list>"
            "<list><name>DVD</name><rename>DVD   </rename></list>"
            "<list><name>AUX2</name><rename></rename></list>"
            "</functionrename></cmd><cmd><functiondelete>"
            "<list><name>DVD</name><use>1</use></list>"
            "<list><name>AUX2</name><use>0</use></list>"
            "</functiondelete></cmd>"
            "<cmd><zone1>MAIN ZONE </zone1></cmd>"
            "<cmd><Name1>Quick Select 1  </Name1><Source1>Videocore  </Source1></cmd></rx>")
        self.assertEqual(d.parse_web(rx), {
            "sources": {"DVD": {"rename": "DVD", "use": "USE"},
                        "AUX2": {"rename": "", "use": "DEL"}},
            "zone_names": {"zone1": "MAIN ZONE"},
            "quick_select": {"1": {"name": "Quick Select 1", "source": "Videocore"}}})

    def test_web_power_status_is_not_a_zone_name(self):
        rx = ET.fromstring("<rx><cmd><zone1>ON</zone1></cmd></rx>")
        self.assertEqual(d.parse_web(rx), {})


SAMPLE = {
    "schema": "1",
    "device": {"friendly_name": "denon"},
    "sources": {"names": {"8K": "NintendoSwitch", "SAT/CBL": "Videocore"},
                "use": {"8K": "USE"}},
    "speakers": {"levels_db": {"FL": "+6.5"}},
    "system": {"eco": "AUTO"},
    "per_input": {
        "8K": {"sound_mode": "MCH STEREO",
               "audyssey": {"multeq": "FLAT", "dynamic_eq": "OFF"},
               "tone": {"bass_db": "0.0"}},
        "MPLAY": {"audyssey": {"multeq": "OFF"}},
    },
}


def live(settings, inp="8K", standby=False):
    return d.Snapshot(settings, {"power": "ON", "main_zone": "ON", "input": inp}, inp, [], standby)


class Yaml(unittest.TestCase):
    def test_roundtrip(self):
        text = d.HEADER + d.dump_yaml(SAMPLE) + "\n"
        self.assertEqual(d.load_yaml(text), SAMPLE)
        self.assertLess(text.index("device:"), text.index("per_input:"))

    def test_pyyaml_compatible(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        self.assertEqual(yaml.safe_load(d.dump_yaml(SAMPLE)), SAMPLE)


class Compare(unittest.TestCase):
    def live_same(self):
        s = {k: v for k, v in SAMPLE.items() if k not in ("schema", "per_input")}
        s = d.json.loads(d.json.dumps(s))
        s["per_input"] = {"8K": d.json.loads(d.json.dumps(SAMPLE["per_input"]["8K"]))}
        return s

    def test_no_drift(self):
        r = d.compare(SAMPLE, live(self.live_same()))
        self.assertEqual((r.changed, r.added, r.unread), ([], [], []))

    def test_drift_ignores_other_inputs_sound_mode_and_device(self):
        s = self.live_same()
        s["per_input"]["8K"]["sound_mode"] = "DOLBY ATMOS"
        s["device"]["friendly_name"] = "x"
        s["per_input"]["8K"]["audyssey"]["dynamic_eq"] = "ON"
        r = d.compare(SAMPLE, live(s))
        self.assertEqual(r.changed, [(("per_input", "8K", "audyssey", "dynamic_eq"), "OFF", "ON")])

    def test_unread_is_not_drift(self):
        s = self.live_same()
        del s["system"]
        r = d.compare(SAMPLE, live(s))
        self.assertEqual(r.changed, [])
        self.assertEqual(r.unread, [(("system", "eco"), "AUTO")])

    def test_new_input_is_info(self):
        s = self.live_same()
        s["per_input"] = {"BD": {"audyssey": {"multeq": "AUDYSSEY"}}}
        r = d.compare(SAMPLE, live(s, inp="BD"))
        self.assertEqual(r.changed, [])
        self.assertTrue(any("BD" in i for i in r.info))

    def test_merge_keeps_other_inputs_and_unread(self):
        s = self.live_same()
        del s["system"]
        s["per_input"]["8K"]["audyssey"]["dynamic_eq"] = "ON"
        new, kept = d.merge_export(SAMPLE, live(s))
        self.assertEqual(new["per_input"]["MPLAY"], SAMPLE["per_input"]["MPLAY"])
        self.assertEqual(new["per_input"]["8K"]["audyssey"]["dynamic_eq"], "ON")
        self.assertEqual(new["system"], {"eco": "AUTO"})
        self.assertEqual(kept, [("system", "eco")])


class Plan(unittest.TestCase):
    def test_commands(self):
        s = d.json.loads(d.json.dumps({k: v for k, v in SAMPLE.items() if k != "schema"}))
        s["per_input"] = {"8K": s["per_input"]["8K"]}
        s["per_input"]["8K"]["audyssey"]["dynamic_eq"] = "ON"
        s["per_input"]["8K"]["tone"]["bass_db"] = "+2.0"
        s["sources"]["names"]["SAT/CBL"] = "Cable"
        s["speakers"]["levels_db"]["FL"] = "0.0"
        steps = {d.fmt_path(x.path): x for x in d.plan(SAMPLE, live(s))}
        self.assertEqual(steps["per_input.8K.audyssey.dynamic_eq"].commands, ["PSDYNEQ OFF"])
        self.assertEqual(steps["per_input.8K.tone.bass_db"].commands, ["PSBAS 50"])
        self.assertEqual(steps["sources.names.SAT/CBL"].commands, ["SSFUNSAT/CBL Videocore"])
        lv = steps["speakers.levels_db.FL"]
        self.assertEqual(lv.commands, ["SSLEVFL 565"])
        self.assertTrue(lv.protected)
        self.assertIn("apply never switches inputs", steps["per_input.MPLAY"].skip)

    def test_multi_repeated_and_keyless_commands(self):
        e, cmds = d.build_commands(("speakers", "config", "SBK"), "SMA,2SP")
        self.assertEqual(cmds, ["SSSPCSBK SMA", "SSSPCSBK 2SP"])
        e, cmds = d.build_commands(("speakers", "crossover", "_"), "IDV")
        self.assertEqual(cmds, ["SSCFR IDV"])

    def test_device_never_applied(self):
        self.assertEqual(d.build_commands(("device", "friendly_name"), "x"), (None, None))

    def test_every_buildable_prefix_is_safe(self):
        for qq in d.QUERIES:
            self.assertTrue(qq.cmd.endswith("?"), qq.cmd)
            self.assertFalse(qq.cmd.startswith("MN"), qq.cmd)
            for e in qq.exts:
                if e.apply:
                    self.assertFalse(e.prefix.startswith(d.FORBIDDEN_PREFIXES), e.prefix)

    def test_apply_send_disabled_without_env(self):
        os.environ.pop("CI", None)
        os.environ.pop("DENON_APPLY_ENABLE", None)
        orig = d.read_live
        d.read_live = lambda *a, **k: live({"system": {"eco": "ON"}})
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(d.dump_yaml({"schema": "1", "system": {"eco": "AUTO"}}))
        orig_t = d.Telnet
        d.Telnet = None  # any connection attempt would raise
        try:
            self.assertEqual(d.main(["--file", f.name, "apply", "--send"]), 3)
        finally:
            d.read_live, d.Telnet = orig, orig_t
            os.unlink(f.name)

    def test_http_only_difference_is_skipped(self):
        s = {"web": {"zone_names": {"zone1": "MAIN ZONE"}}}
        steps = d.plan({"schema": "1", "web": {"zone_names": {"zone1": "X"}}}, live(s))
        self.assertEqual(len(steps), 1)
        self.assertIn("no set command", steps[0].skip)

    def test_apply_send_refused_in_ci(self):
        os.environ["CI"] = "true"
        try:
            orig = d.read_live
            d.read_live = lambda *a, **k: live({"system": {"eco": "ON"}})
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write(d.dump_yaml({"schema": "1", "system": {"eco": "AUTO"}}))
            rc = d.main(["--file", f.name, "apply", "--send"])
            self.assertEqual(rc, 3)
        finally:
            d.read_live = orig
            del os.environ["CI"]
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
