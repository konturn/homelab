"""Tests for ci/facts (run: python3 -m unittest discover -s ci/facts -p 'test_*.py')."""

import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import collect  # noqa: E402
import drift  # noqa: E402
import facts  # noqa: E402
import render  # noqa: E402

SNAPSHOT = HERE / "testdata" / "router-2026-11-07.json"

ZPOOL_STATUS = """  pool: mpool
 state: DEGRADED
config:

\tNAME        STATE     READ WRITE CKSUM
\tmpool       DEGRADED     0     0     0
\t  raidz2-0  ONLINE       0     0     0
\t    dev1    ONLINE       0     0     0
\t  raidz2-1  DEGRADED     0     0     0
\t    replacing-0  DEGRADED 0  0     0
\t      dev12   FAULTED     55     0     0  too many errors
\t      dev25   ONLINE       0     0     0
\t    dev11   ONLINE       0     0     0

errors: No known data errors
"""


class DataTest(unittest.TestCase):
    def test_fact_files_are_valid(self):
        facts.load()

    def test_generated_files_are_current(self):
        for rel, text in render.targets().items():
            self.assertEqual((facts.ROOT / rel).read_text(), text,
                             f"{rel} is stale: run python3 ci/facts/render.py")

    def test_serial_suffix_limit(self):
        hw = facts.load_yaml(facts.HARDWARE)
        hw["disks"][0]["serial_suffix"] = "ABCDEFGH"
        self.assertTrue(any("longer than 4" in e for e in facts.validate_hardware(hw)))

    def test_remote_node_probe_must_be_network(self):
        deps = {"nodes": {"x": {"kind": "container", "host": "zwave", "probe": {"container": "x"}}}}
        self.assertTrue(facts.validate_dependencies(deps))

    def test_cycle_detected(self):
        deps = {"nodes": {"a": {"kind": "process", "needs": ["b"]}, "b": {"kind": "process", "needs": ["a"]}}}
        self.assertTrue(any("cycle" in e for e in facts.validate_dependencies(deps)))

    def test_vars_substituted(self):
        _hw, deps = facts.load()
        self.assertNotIn("{{", json.dumps(deps))

    def test_dependents_transitive(self):
        _hw, deps = facts.load()
        self.assertIn("homeassistant", facts.dependents(deps, "mosquitto"))
        self.assertIn("deploy", facts.dependents(deps, "gitlab"))


class RenderTest(unittest.TestCase):
    def test_usb_devpath(self):
        ctrl = {"pci_path": "pci0000:80/0000:80:02.0/0000:82:00.0"}
        self.assertEqual(render.usb_devpath(ctrl, "3-2.3.2"),
                         "/devices/pci0000:80/0000:80:02.0/0000:82:00.0/usb3/3-2/3-2.3/3-2.3.2/3-2.3.2:1.0")
        self.assertEqual(render.usb_devpath(ctrl, "3-4"),
                         "/devices/pci0000:80/0000:80:02.0/0000:82:00.0/usb3/3-4/3-4:1.0")

    def test_crypttab_order(self):
        lines = [l for l in render.targets()["base/disk/crypttab"].splitlines() if not l.startswith("#")]
        names = [l.split()[0] for l in lines]
        self.assertEqual(names[:3], ["sda5_crypt", "persistent_data", "dev1"])
        self.assertNotIn("dev22", names)
        nums = [int(n[3:]) for n in names[2:]]
        self.assertEqual(nums, sorted(nums))


class ParseTest(unittest.TestCase):
    def test_zpool_status(self):
        p = collect.parse_zpool_status(ZPOOL_STATUS)["mpool"]
        self.assertEqual(p["state"], "DEGRADED")
        self.assertEqual(p["vdevs"], {"raidz2-0": "ONLINE", "raidz2-1": "DEGRADED"})
        self.assertEqual(p["leaves"]["dev12"]["vdev"], "raidz2-1")
        self.assertEqual(p["leaves"]["dev12"]["state"], "FAULTED")
        self.assertEqual(p["leaves"]["dev11"]["vdev"], "raidz2-1")

    def test_model_match(self):
        self.assertTrue(drift.model_matches("WD240KFGX", "WDC WD240KFGX-68"))
        self.assertTrue(drift.model_matches("Samsung SSD 870 EVO 1TB", "Samsung SSD 870"))
        self.assertFalse(drift.model_matches("WD240KFGX", "WDC WD201KFGX-68"))


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.hw, self.deps = facts.load()
        self.live = json.loads(SNAPSHOT.read_text())

    def texts(self, live):
        rep, _ = drift.compare(self.hw, self.deps, live)
        return rep, [t for items in rep.sections.values() for lvl, t in items if lvl == "DRIFT"]

    def test_snapshot_reports_known_drift(self):
        rep, items = self.texts(self.live)
        joined = "\n".join(items)
        self.assertIn("hba/phy13 (dev12", joined)
        self.assertIn("FAULTED", joined)
        self.assertIn("pool mpool is DEGRADED", joined)
        self.assertIn("`Device` on port 2-1.3.2", joined)
        self.assertIn("`Device_1` on port 2-1.3.3", joined)
        self.assertIn("Office: no card named Office", joined)
        self.assertNotIn("Global", joined)
        self.assertIn("doorbell (tcp", joined)
        self.assertIn("affects amcrest2mqtt", joined)
        # the persistent_data_r mapper name is a note, not drift
        notes = [t for lvl, t in rep.sections["disks"] if lvl == "NOTE"]
        self.assertTrue(any("persistent_data_r" in t for t in notes))

    def healthy(self):
        live = copy.deepcopy(self.live)
        pool = live["zpools"]["mpool"]
        pool["state"] = "ONLINE"
        pool["vdevs"] = {v: "ONLINE" for v in pool["vdevs"]}
        for leaf in pool["leaves"].values():
            leaf["state"] = "ONLINE"
        ctrls = self.hw["usb_controllers"]
        cards = [c for c in live["sound_cards"] if not c.get("usb_port")]
        for i, z in enumerate(self.hw["audio_zones"]):
            path = render.usb_devpath(ctrls[z["controller"]], z["port"]) + f"/sound/card{10 + i}"
            cards.append({"card": f"card{10 + i}", "id": z["id"], "devpath": path, "usb_port": z["port"]})
        live["sound_cards"] = cards
        for p in live["probes"].values():
            p["ok"] = True
        for d in live["disks"].values():
            d["mappers"] = ["persistent_data" if m == "persistent_data_r" else m for m in d["mappers"]]
        return live

    def test_healthy_snapshot_has_no_drift(self):
        rep, items = self.texts(self.healthy())
        self.assertEqual(items, [])
        self.assertEqual(rep.drift_count, 0)

    def test_disk_swapped(self):
        live = self.healthy()
        path = "pci-0000:06:00.0-sas-exp0x5003048001134f3f-phy12-lun-0"
        live["disks"][path]["serial_suffix"] = "ZZZZ"
        _, items = self.texts(live)
        self.assertTrue(any(t.startswith("hba/phy12: holds") for t in items), items)

    def test_disk_missing_and_undeclared(self):
        live = self.healthy()
        path = "pci-0000:06:00.0-sas-exp0x5003048001134f3f-phy12-lun-0"
        live["disks"]["pci-0000:06:00.0-sas-exp0x5003048001134f3f-phy22-lun-0"] = live["disks"].pop(path)
        _, items = self.texts(live)
        joined = "\n".join(items)
        self.assertIn("hba/phy12: no disk", joined)
        self.assertIn("hba/phy22: declared empty", joined)

    def test_probe_down_lists_dependents(self):
        live = self.healthy()
        live["probes"]["slzb_coordinator"] = {"ok": False, "detail": "connect failed: timed out"}
        _, items = self.texts(live)
        self.assertTrue(any("slzb_coordinator" in t and "zigbee2mqtt" in t for t in items), items)

    def test_render_report(self):
        rep, checked = drift.compare(self.hw, self.deps, self.healthy())
        text = rep.render(checked)
        self.assertIn("no drift", text)


if __name__ == "__main__":
    unittest.main()
