"""Tests for ci/facts (run: python3 -m unittest discover -s ci/facts -p 'test_*.py')."""

import copy
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_context  # noqa: E402
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
\tspares
\t  dev25     AVAIL

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

    def test_amp_devices_match_compose(self):
        hw = facts.load_yaml(facts.HARDWARE)
        services = facts.compose_services()
        self.assertEqual(facts.check_amp_devices(hw, services), [])
        hw["amps"]["A"]["ha_port"] = "/dev/ttyUSB9"
        self.assertTrue(facts.check_amp_devices(hw, services))

    def test_amp_input_stream_must_exist(self):
        hw = facts.load_yaml(facts.HARDWARE)
        hw["amps"]["A"]["inputs"][5]["stream"] = "Nowhere"
        self.assertTrue(any("Nowhere" in e for e in facts.validate_hardware(hw)))

    def test_expected_containers(self):
        exp = drift.expected_containers()
        self.assertEqual(exp["homeassistant"], "running")
        self.assertNotIn("wordpress", exp)  # compose profile
        self.assertIn("snapclient_global", exp)
        self.assertNotIn("snapclient_office", exp)  # snapclient_disabled_zones

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


class AgentContextTest(unittest.TestCase):
    """docker/openclaw/workspace/infra/TOOLS.md (staleness: DataTest above)."""

    @classmethod
    def setUpClass(cls):
        cls.text = agent_context.render()

    def test_every_marker_replaced(self):
        self.assertNotIn("<!-- generated:", self.text)
        template = (facts.ROOT / agent_context.TEMPLATE).read_text()
        for name in agent_context.SECTIONS:
            self.assertIn(f"<!-- generated: {name} -->", template, f"section {name} unused")

    def test_size_fits_openclaw(self):
        # OpenClaw truncates a bootstrap file above 20,000 chars; its config
        # guard distrusts a file that shrinks by more than half.
        self.assertLess(len(self.text), 18000)
        self.assertGreater(len(self.text), 6000)

    def test_bot_config_matches_sweep(self):
        sys.path.insert(0, str(facts.ROOT / "ci" / "automerge"))
        import botconfig
        cfg = botconfig.config(facts.load_yaml(agent_context.POLICY))
        section = self.text.split("## Merging your own MRs", 1)[1]
        for pattern in cfg["never"] + [p for k in botconfig.KINDS for p in cfg["paths"][k]]:
            self.assertIn(f"`{pattern}`", section)
        self.assertIn(f"at most {cfg['max_per_24h']} bot-config merges", section)
        for svc in cfg["denied_services"]:
            self.assertIn(svc, section)

    def test_facts_present(self):
        for needle in ("dev12", "870 EVO", "Office (zone_16_2)", "restart zigbee2mqtt",
                       "homeassistant needs mqtt, zwave_js, influxdb, dns", "snapclient_global",
                       "6 `iot` 10.6.0.0/16"):
            self.assertIn(needle, self.text)

    def test_unknown_marker_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (agent_context.TEMPLATE,):
                (root / rel).parent.mkdir(parents=True)
                (root / rel).write_text("<!-- generated: nope -->\n")
            with self.assertRaises(facts.FactsError):
                agent_context.render(root)


class ParseTest(unittest.TestCase):
    def test_zpool_status(self):
        p = collect.parse_zpool_status(ZPOOL_STATUS)["mpool"]
        self.assertEqual(p["state"], "DEGRADED")
        self.assertEqual(p["vdevs"], {"raidz2-0": "ONLINE", "raidz2-1": "DEGRADED", "spares": ""})
        self.assertEqual(p["leaves"]["dev12"]["vdev"], "raidz2-1")
        self.assertEqual(p["leaves"]["dev12"]["state"], "FAULTED")
        self.assertEqual(p["leaves"]["dev11"]["vdev"], "raidz2-1")
        self.assertEqual(p["state"], "DEGRADED")  # the spares line does not reset it
        self.assertEqual(p["leaves"]["dev25"], {"vdev": "spares", "state": "AVAIL", "errors": ""})

    def test_crypttab_has_spare(self):
        names = [l.split()[0] for l in render.targets()["base/disk/crypttab"].splitlines() if not l.startswith("#")]
        self.assertEqual(names[-1], "dev25")

    def test_model_match(self):
        self.assertTrue(drift.model_matches("WD240KFGX", "WDC WD240KFGX-68"))
        self.assertTrue(drift.model_matches("Samsung SSD 870 EVO 1TB", "Samsung SSD 870"))
        self.assertFalse(drift.model_matches("WD240KFGX", "WDC WD201KFGX-68"))


class DriftTest(unittest.TestCase):
    def setUp(self):
        self.hw, self.deps = facts.load()
        self.live = json.loads(SNAPSHOT.read_text())
        self.expected = drift.expected_containers()

    def texts(self, live):
        rep, _ = drift.compare(self.hw, self.deps, live, self.expected)
        return rep, [t for items in rep.sections.values() for lvl, t in items if lvl == "DRIFT"]

    def test_snapshot_reports_known_drift(self):
        rep, items = self.texts(self.live)
        joined = "\n".join(items)
        # 2026-11-08: dev12 moved to phy19 and resilvered, dev25 a spare: no disk drift
        self.assertFalse([t for lvl, t in rep.sections["disks"] if lvl == "DRIFT"], rep.sections["disks"])
        self.assertIn("`Device` on port 2-1.3.2", joined)
        self.assertIn("`Device_1` on port 2-1.3.3", joined)
        self.assertIn("Office: no card named Office", joined)
        self.assertNotIn("Global", joined)
        self.assertIn("doorbell (tcp", joined)
        self.assertIn("affects amcrest2mqtt", joined)
        self.assertIn("issue #123", joined)  # known_issue shown
        # the persistent_data_r mapper name is a note, not drift
        notes = [t for lvl, t in rep.sections["disks"] if lvl == "NOTE"]
        self.assertTrue(any("persistent_data_r" in t for t in notes))

    def healthy(self):
        live = copy.deepcopy(self.live)
        pool = live["zpools"]["mpool"]
        pool["state"] = "ONLINE"
        pool["vdevs"] = {v: "ONLINE" for v in pool["vdevs"]}
        for leaf in pool["leaves"].values():
            leaf["state"] = "AVAIL" if leaf["vdev"] == "spares" else "ONLINE"
        ctrls = self.hw["usb_controllers"]
        cards = [c for c in live["sound_cards"] if not c.get("usb_port")]
        for i, z in enumerate(self.hw["audio_zones"]):
            path = render.usb_devpath(ctrls[z["controller"]], z["port"]) + f"/sound/card{10 + i}"
            cards.append({"card": f"card{10 + i}", "id": z["id"], "devpath": path, "usb_port": z["port"]})
        live["sound_cards"] = cards
        for p in live["probes"].values():
            p["ok"] = True
        live["containers"] = {n: {"state": "running" if w == "running" else "exited", "health": None,
                                  "exit_code": None if w == "running" else 0}
                              for n, w in self.expected.items()}
        for d in live["disks"].values():
            d["mappers"] = ["persistent_data" if m == "persistent_data_r" else m for m in d["mappers"]]
        return live

    def test_healthy_snapshot_has_no_drift(self):
        rep, items = self.texts(self.healthy())
        self.assertEqual(items, [])
        self.assertEqual(rep.drift_count, 0)

    def test_spare_faulted_or_misplaced(self):
        live = self.healthy()
        live["zpools"]["mpool"]["leaves"]["dev25"]["state"] = "FAULTED"
        _, items = self.texts(live)
        self.assertTrue(any("dev25, spare): FAULTED" in t for t in items), items)
        live["zpools"]["mpool"]["leaves"]["dev25"] = {"vdev": "raidz2-1", "state": "ONLINE", "errors": ""}
        _, items = self.texts(live)
        self.assertTrue(any("declared a spare, is in raidz2-1" in t for t in items), items)

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

    def test_amp_adapter_missing(self):
        live = self.healthy()
        del live["serial_by_id"][self.hw["amps"]["B"]["serial_by_id"]]
        _, items = self.texts(live)
        self.assertTrue(any(t.startswith("amp B:") for t in items), items)

    def test_container_down_and_oneshot_failed(self):
        live = self.healthy()
        live["containers"]["zigbee2mqtt"] = {"state": "exited", "exit_code": 1, "health": None}
        live["containers"]["gitlab"]["health"] = "unhealthy"
        # no one-shot service is declared today; check the rule with a made-up one
        self.expected = {**self.expected, "example-init": "oneshot"}
        live["containers"]["example-init"] = {"state": "exited", "exit_code": 2, "health": None}
        _, items = self.texts(live)
        joined = "\n".join(items)
        self.assertIn("zigbee2mqtt: exited, exit code 1", joined)
        self.assertIn("example-init: one-shot exited, exit code 2", joined)
        self.assertIn("gitlab: running, unhealthy", joined)

    def test_expected_interfaces_from_compose(self):
        ifaces = drift.expected_interfaces()
        self.assertEqual(ifaces["containers"]["homeassistant"],
                         {"internal": "int0", "iot": "iot0", "external": "ext0"})
        self.assertEqual(sorted(ifaces["ha_adapters"]), ["int0", "iot0"])
        self.assertTrue(ifaces["ha_core_network"].endswith("/homeassistant/.storage/core.network"))
        self.assertEqual(drift.interface_spec(ifaces)["interfaces"], sorted(ifaces["containers"]))

    def test_interfaces(self):
        ifaces = drift.expected_interfaces()
        live = self.healthy()
        rep, _ = drift.compare(self.hw, self.deps, live, self.expected, ifaces)
        self.assertIn("not collected", rep.sections["interfaces"][0][1])
        self.assertEqual(rep.drift_count, 0)
        live["interfaces"] = {c: ["lo"] + sorted(n.values()) for c, n in ifaces["containers"].items()}
        live["ha_adapters"] = ["iot0", "int0"]
        rep, items = drift.compare(self.hw, self.deps, live, self.expected, ifaces)
        self.assertEqual(rep.drift_count, 0, rep.sections)
        live["interfaces"]["homeassistant"] = ["eth0", "eth1", "eth2", "lo"]
        live["ha_adapters"] = ["eth1", "eth2"]
        rep, _ = drift.compare(self.hw, self.deps, live, self.expected, ifaces)
        joined = "\n".join(t for lvl, t in rep.sections["interfaces"] if lvl == "DRIFT")
        self.assertIn("homeassistant: no interface int0 for network internal", joined)
        self.assertIn("configured_adapters is ['eth1', 'eth2']", joined)
        live["interfaces"]["homeassistant"] = None
        rep, _ = drift.compare(self.hw, self.deps, live, self.expected, ifaces)
        self.assertTrue(any("not running" in t for _, t in rep.sections["interfaces"]))

    def test_render_report(self):
        rep, checked = drift.compare(self.hw, self.deps, self.healthy(), self.expected)
        text = rep.render(checked)
        self.assertIn("no drift", text)


if __name__ == "__main__":
    unittest.main()
