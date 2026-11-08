"""Tests for ci/facts/resolve.py (run: python3 -m unittest discover -s ci/facts -p 'test_*.py')."""

import copy
import json
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import drift  # noqa: E402
import facts  # noqa: E402
import resolve  # noqa: E402

SNAPSHOT = HERE / "testdata" / "router-2026-11-07.json"
HBA = "pci-0000:06:00.0-sas-exp0x5003048001134f3f-{}-lun-0"
TODAY = "2026-11-08"


class ProposeDisksTest(unittest.TestCase):
    def setUp(self):
        self.hw, self.deps = facts.load()
        self.live = json.loads(SNAPSHOT.read_text())
        self.text = (facts.ROOT / facts.HARDWARE).read_text()

    def leaves(self):
        return self.live["zpools"]["mpool"]["leaves"]

    def check_rewrite(self, new):
        out = resolve.rewrite_hardware(self.text, self.hw["disks"], new)
        hw = yaml.safe_load(out)
        self.assertEqual(hw["disks"], new)
        self.assertEqual(facts.validate_hardware(hw), [])
        # comments outside the changed entries survive
        for line in ("# LSI HBA -> backplane expander.", "# Motherboard SATA", "disk_controllers:", "audio_zones:"):
            self.assertIn(line, out)
        return out

    def test_no_change_no_proposal(self):
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        self.assertIsNone(new)
        self.assertEqual(changes, [])

    def test_replaced_disk(self):
        d = self.live["disks"][HBA.format("phy12")]
        d.update(model="WDC WD261KFGX-68", serial_suffix="AB12", mappers=["dev30"],
                 luks_uuid="11111111-2222-3333-4444-555555555555")
        self.leaves()["dev30"] = self.leaves().pop("dev1")
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        self.assertIn("slot:hba/phy12", covered)
        e = next(x for x in new if x["slot"] == "hba/phy12")
        self.assertEqual((e["model"], e["serial_suffix"], e["role"], e["luks"]["mapper"], e["zpool"]["vdev"]),
                         ("WD261KFGX", "AB12", "data", "dev30", "raidz2-0"))
        self.assertTrue(any("replacing WD240KFGX ...V9XN" in c for c in changes), changes)
        out = self.check_rewrite(new)
        self.assertIn("dev30", out)
        self.assertNotIn("V9XN", out)

    def test_moved_disk_keeps_its_entry_text(self):
        self.live["disks"][HBA.format("phy22")] = self.live["disks"].pop(HBA.format("phy20"))
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        self.assertEqual({e["slot"]: e.get("serial_suffix") for e in new if e["slot"] in ("hba/phy20", "hba/phy22")},
                         {"hba/phy20": None, "hba/phy22": "S6SN"})
        self.assertTrue(any("hba/phy20 -> hba/phy22" in c for c in changes), changes)
        out = self.check_rewrite(new)
        self.assertIn("{slot: hba/phy22, model: WD201KFGX, serial_suffix: S6SN", out)

    def test_removed_disk(self):
        self.live["disks"].pop(HBA.format("phy12"))
        self.leaves().pop("dev1")
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        e = next(x for x in new if x["slot"] == "hba/phy12")
        self.assertEqual(e["role"], "empty")
        self.check_rewrite(new)

    def test_missing_pool_member_is_hands(self):
        self.live["disks"].pop(HBA.format("phy12"))  # dev1 still in the pool
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        self.assertIsNone(new)

    def test_new_disk_outside_pool_is_hands(self):
        self.live["disks"][HBA.format("phy22")] = {"dev": "sdz", "model": "WDC WD261KFGX-68", "serial_suffix": "QQQQ",
                                                  "mappers": [], "luks_uuid": None}
        new, changes, covered, hands = resolve.propose_disks(self.hw, self.live, TODAY)
        self.assertIsNone(new)
        self.assertTrue(any(k == "slot:hba/phy22" for k, _ in hands), hands)


class SoftwareTest(unittest.TestCase):
    def test_commands(self):
        services = resolve.container_services()
        cmd, _ = resolve.remediation_command({"type": "container", "name": "zigbee2mqtt"}, services)
        self.assertTrue(cmd.endswith("up -d --no-deps zigbee2mqtt"), cmd)
        self.assertIsNone(resolve.remediation_command({"type": "container", "name": "vault"}, services)[0])
        self.assertIsNone(resolve.remediation_command({"type": "systemd", "unit": "x; rm -rf /"}, services)[0])
        self.assertEqual(resolve.remediation_command({"type": "systemd", "unit": "frigate-backup-events.timer"},
                                                     services)[0], "systemctl start frigate-backup-events.timer")

    def test_only_stopped_containers_get_a_fix(self):
        hw, deps = facts.load()
        live = json.loads(SNAPSHOT.read_text())
        live["containers"]["zigbee2mqtt"] = {"state": "exited", "exit_code": 1, "health": None}
        live["containers"]["grafana"]["health"] = "unhealthy"
        rep, _ = drift.compare(hw, deps, live, drift.expected_containers())
        acts = {i["key"]: i["action"] for i in rep.items}
        self.assertEqual(acts["container:zigbee2mqtt"], {"type": "container", "name": "zigbee2mqtt"})
        self.assertIsNone(acts["container:grafana"])


class FakeGitLab:
    def __init__(self, issues=None):
        self.calls, self.issues = [], issues or []

    def req(self, method, path, params=None, body=None, raw=False):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/issues"):
            return self.issues
        return {"iid": 99}


class LivingIssueTest(unittest.TestCase):
    ITEMS = [("disks", "slot:hba/phy13", "dev12 FAULTED"), ("audio zones", "zone:Office", "no card")]

    def test_since_is_kept(self):
        first = resolve.living_report(self.ITEMS, "", "2026-11-01", None)
        again = resolve.living_report(self.ITEMS, first, "2026-11-08", None)
        self.assertIn("dev12 FAULTED _(since 2026-11-01)_", again)

    def test_rewrite_only_on_change_and_close_when_empty(self):
        body = resolve.living_report(self.ITEMS, "", "2026-11-01", None)
        gl = FakeGitLab([{"iid": 155, "description": body, "labels": ["facts-drift", "needs-hands"]}])
        self.assertEqual(resolve.update_issue(gl, self.ITEMS, "2026-11-08", None), "#155 unchanged")
        self.assertEqual(resolve.update_issue(gl, self.ITEMS[:1], "2026-11-08", None), "#155 rewritten")
        self.assertFalse([c for c in gl.calls if c[0] == "POST" and c[1].endswith("/notes")])  # no notes per run
        self.assertEqual(resolve.update_issue(gl, [], "2026-11-08", None), "closed #155")


if __name__ == "__main__":
    unittest.main()
