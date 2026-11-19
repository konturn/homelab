"""Offline tests for reconcile.py: desired state, planning, adopting UI edits."""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reconcile as r  # noqa: E402

HW = """\
amps:
  A:
    model: Monoprice 10761
    serial_by_id: x
    ha_port: /dev/ttyUSB0
    inputs:
      1: {label: Global, stream: Global}
      2: {label: Guest Bathroom, stream: Guest_Bathroom}
    zones:
      11: {name: Living Room Speakers 2, entity: media_player.zone_11}
      15: {name: Dead, entity: media_player.amp_a_zone_15}
  C:
    model: Monoprice 10761
    serial_by_id: y
    ha_port: /dev/ttyUSB2
    inputs:
      1: {label: Global, stream: Global}
    zones:
      15: {name: Kitchen Speakers, entity: media_player.zone_15}
  D:
    model: Monoprice 10761
    connected: false
"""

ENT = """\
# comment kept
entities:
  media_player.zone_15: {area: kitchen}
  media_player.amp_a_zone_15: {name: Amp A Zone 15 (dead output), disabled: true}
  media_player.amp_c_zone_11: {name: Amp C Zone 11 (spare)}

amp_inputs:
  C: {2: Input 2, 3: Input 3}
"""


def repo(hw=HW, ent=ENT):
    d = Path(tempfile.mkdtemp())
    (d / "inventory").mkdir()
    (d / "docker" / "homeassistant").mkdir(parents=True)
    (d / r.HARDWARE).write_text(hw)
    (d / r.ENTITIES).write_text(ent)
    return d


def dev_entity(eid, device_id, **kw):
    return dict({"entity_id": eid, "device_id": device_id, "has_entity_name": True, "original_name": None,
                 "name": None, "area_id": None, "disabled_by": None}, **kw)


class DesiredTest(unittest.TestCase):
    def test_merges_inventory_and_overrides(self):
        ents, inputs = r.desired(repo())
        self.assertEqual(ents["media_player.zone_11"]["name"], "Living Room Speakers 2")
        self.assertEqual(ents["media_player.zone_15"], {"name": "Kitchen Speakers", "area": "kitchen",
                                                        "source": {"name": r.HARDWARE, "area": r.ENTITIES}})
        a15 = ents["media_player.amp_a_zone_15"]
        self.assertEqual((a15["name"], a15["disabled"], a15["source"]["name"]),
                         ("Amp A Zone 15 (dead output)", True, r.ENTITIES))
        self.assertEqual(inputs["/dev/ttyUSB2"], {1: "Global", 2: "Input 2", 3: "Input 3"})
        self.assertEqual(inputs["/dev/ttyUSB0"], {1: "Global", 2: "Guest Bathroom"})

    def test_amp_inputs_for_unknown_amp_fails(self):
        with self.assertRaises(ValueError):
            r.desired(repo(ent="entities: {}\namp_inputs:\n  Z: {1: x}\n"))

    def test_unquoted_comma_is_refused(self):
        with self.assertRaises(ValueError):
            r.desired(repo(ent="entities:\n  a.x: {name: A (b, c)}\n"))
        ents, _ = r.desired(repo(ent="entities:\n  a.x: {name: 'A (b, c)'}\n"))
        self.assertEqual(ents["a.x"]["name"], "A (b, c)")

    def test_seeded_file_parses(self):
        root = Path(__file__).resolve().parents[2]
        if (root / r.ENTITIES).exists():
            ents, _ = r.desired(root)
            self.assertIn("media_player.zone_15", ents)


class PlanTest(unittest.TestCase):
    AREAS = {"kitchen", "living_room"}

    def test_device_named_entity_gets_device_name_and_area(self):
        reg = {"media_player.zone_15": dev_entity("media_player.zone_15", "d1")}
        devs = {"d1": {"id": "d1", "name": "Zone 15", "name_by_user": None, "area_id": None}}
        changes, problems = r.plan({"media_player.zone_15": {"name": "Kitchen Speakers", "area": "kitchen"}},
                                   reg, devs, self.AREAS)
        self.assertEqual(problems, [])
        self.assertEqual([(c["op"], c["field"], c["to"]) for c in changes],
                         [("device", "name_by_user", "Kitchen Speakers"), ("device", "area_id", "kitchen")])

    def test_in_sync_is_empty(self):
        reg = {"media_player.zone_15": dev_entity("media_player.zone_15", "d1")}
        devs = {"d1": {"id": "d1", "name_by_user": "Kitchen Speakers", "area_id": "kitchen"}}
        self.assertEqual(r.plan({"media_player.zone_15": {"name": "Kitchen Speakers", "area": "kitchen"}},
                                reg, devs, self.AREAS), ([], []))

    def test_entity_with_own_name(self):
        reg = {"light.x": dev_entity("light.x", "d1", name="Old", has_entity_name=False, original_name="Old")}
        changes, _ = r.plan({"light.x": {"name": "New", "area": "kitchen"}}, reg, {}, self.AREAS)
        self.assertEqual([(c["op"], c["field"]) for c in changes], [("entity", "name"), ("entity", "area_id")])

    def test_disable_and_enable(self):
        reg = {"a.x": dev_entity("a.x", None, has_entity_name=False, original_name="X"),
               "a.y": dev_entity("a.y", None, has_entity_name=False, original_name="Y", disabled_by="user")}
        changes, _ = r.plan({"a.x": {"disabled": True}, "a.y": {"disabled": False}}, reg, {}, set())
        self.assertEqual([(c["entity_id"], c["to"]) for c in changes], [("a.x", "user"), ("a.y", None)])

    def test_integration_disabled_is_reported(self):
        reg = {"a.x": dev_entity("a.x", None, has_entity_name=False, original_name="X", disabled_by="integration")}
        changes, problems = r.plan({"a.x": {"disabled": False}}, reg, {}, set())
        self.assertEqual(changes, [])
        self.assertIn("disabled by integration", problems[0])

    def test_rename_only_when_new_is_free(self):
        reg = {"a.old": dev_entity("a.old", None, has_entity_name=False, original_name="O")}
        changes, problems = r.plan({"a.new": {"rename_from": "a.old"}}, reg, {}, set())
        self.assertEqual(changes, [{"op": "rename", "entity_id": "a.old", "new_entity_id": "a.new"}])
        reg["a.new"] = dev_entity("a.new", None, has_entity_name=False, original_name="N")
        changes, problems = r.plan({"a.new": {"rename_from": "a.old"}}, reg, {}, set())
        self.assertEqual(changes, [])
        self.assertIn("not renaming", problems[0])

    def test_missing_entity_and_area_are_problems(self):
        reg = {"a.x": dev_entity("a.x", None, has_entity_name=False, original_name="X")}
        changes, problems = r.plan({"a.gone": {"name": "G"}, "a.x": {"area": "attic"}}, reg, {}, {"kitchen"})
        self.assertEqual(changes, [])
        self.assertEqual(len(problems), 2)

    def test_only_listed_inputs(self):
        changes = r.plan_inputs({"/dev/ttyUSB2": {1: "Global"}},
                                {"/dev/ttyUSB2": {1: "Global", 2: "anything"}, "/dev/ttyUSB0": {1: "x"}})
        self.assertEqual(changes, [])
        changes = r.plan_inputs({"/dev/ttyUSB2": {2: "Input 2"}}, {"/dev/ttyUSB2": {2: "Kitchen"}})
        self.assertEqual(changes[0]["diff"], {2: ("Kitchen", "Input 2")})


class AdoptTest(unittest.TestCase):
    def setUp(self):
        self.root = repo()
        self.want, _ = r.desired(self.root)
        self.texts = {r.ENTITIES: ENT, r.HARDWARE: HW}

    def test_name_from_inventory_written_there(self):
        c = {"op": "device", "entity_id": "media_player.zone_15", "device_id": "d", "field": "name_by_user",
             "from": "Kitchen: Island", "to": "Kitchen Speakers"}
        new, notes = r.adopt(self.texts, self.want, [c], {})
        hw = yaml.safe_load(new[r.HARDWARE])
        self.assertEqual(hw["amps"]["C"]["zones"][15], {"name": "Kitchen: Island", "entity": "media_player.zone_15"})
        self.assertEqual(new[r.ENTITIES], ENT)
        self.assertNotIn("NOT WRITTEN", notes[0])

    def test_entities_file_fields_and_comments_kept(self):
        cs = [{"op": "entity", "entity_id": "media_player.amp_a_zone_15", "field": "disabled_by",
               "from": None, "to": "user"},
              {"op": "device", "entity_id": "media_player.zone_15", "device_id": "d", "field": "area_id",
               "from": "living_room", "to": "kitchen"}]
        new, _ = r.adopt(self.texts, self.want, cs, {})
        self.assertIn("# comment kept", new[r.ENTITIES])
        ents = yaml.safe_load(new[r.ENTITIES])["entities"]
        self.assertIs(ents["media_player.amp_a_zone_15"]["disabled"], False)
        self.assertEqual(ents["media_player.zone_15"]["area"], "living_room")

    def test_unlisted_entity_is_added(self):
        c = {"op": "entity", "entity_id": "media_player.zone_11", "field": "area_id", "from": "deck", "to": None}
        new, _ = r.adopt(self.texts, dict(self.want, **{"media_player.zone_11": {"area": None, "source": {}}}), [c], {})
        self.assertEqual(yaml.safe_load(new[r.ENTITIES])["entities"]["media_player.zone_11"], {"area": "deck"})

    def test_inputs_go_to_their_file(self):
        src = r.inputs_sources(self.root)
        c = {"op": "inputs", "port": "/dev/ttyUSB2", "diff": {1: ("Global 2", "Global"), 3: ("Bedroom", "Input 3")}}
        new, notes = r.adopt(self.texts, self.want, [c], src)
        hw = yaml.safe_load(new[r.HARDWARE])
        self.assertEqual(hw["amps"]["C"]["inputs"][1], {"label": "Global 2", "stream": "Global"})
        self.assertEqual(hw["amps"]["A"]["inputs"][1]["label"], "Global")
        self.assertEqual(yaml.safe_load(new[r.ENTITIES])["amp_inputs"]["C"][3], "Bedroom")
        self.assertFalse(any(n.startswith("NOT WRITTEN") for n in notes))

    def test_cleared_name_not_written(self):
        c = {"op": "device", "entity_id": "media_player.zone_15", "device_id": "d", "field": "name_by_user",
             "from": None, "to": "Kitchen Speakers"}
        new, notes = r.adopt(self.texts, self.want, [c], {})
        self.assertEqual(new, self.texts)
        self.assertTrue(notes[0].startswith("NOT WRITTEN"))


if __name__ == "__main__":
    unittest.main()
