"""The playbook schema and the CI validator (doc 4.4, 4.5, 7.1)."""

import copy
import unittest

from router.config import ConfigError, Playbook, page_class, parse_config, validate
from router.deps import Graph

from .helpers import CONFIG, GRAPH, PLAYBOOKS, event

BASE = PLAYBOOKS[0]


def pb(**changes):
    raw = copy.deepcopy(BASE)
    raw.update(changes)
    return Playbook(raw, "test.yml")


class TestPlaybookSchema(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(pb().action, "restart_container")

    def test_rejects_unknown_keys(self):
        with self.assertRaises(ConfigError):
            pb(command="docker rm -f zigbee2mqtt")

    def test_rejects_non_allowlisted_action(self):
        for t in ("restart_systemd_unit", "shell", "exec"):
            with self.assertRaises(ConfigError, msg=t):
                pb(action={"type": t, "target": "zigbee2mqtt"})

    def test_rejects_hard_denied_actions(self):
        for t in ("power_off", "ups_command", "ha_service_call", "unlock", "router_reboot",
                  "firewall", "delete_volume", "zpool", "vault_unseal", "unban", "s2500_uplink"):
            with self.assertRaisesRegex(ConfigError, "hard-denied", msg=t):
                pb(action={"type": t, "target": "zigbee2mqtt"})

    def test_rejects_hard_denied_targets(self):
        for t in ("gitlab", "vault", "pihole", "event-router", "docker-socket-proxy"):
            with self.assertRaisesRegex(ConfigError, "hard-denied", msg=t):
                pb(action={"type": "restart_container", "target": t})

    def test_rejects_free_form_targets(self):
        for t in ("zigbee2mqtt; reboot", "../etc", "$(id)", "A B"):
            with self.assertRaises(ConfigError, msg=t):
                pb(action={"type": "restart_container", "target": t})
        with self.assertRaises(ConfigError):
            pb(action={"type": "restart_container", "target_from_label": "summary"})

    def test_verify_and_limits_mandatory(self):
        raw = copy.deepcopy(BASE)
        del raw["verify"]
        with self.assertRaises(ConfigError):
            Playbook(raw)
        raw = copy.deepcopy(BASE)
        del raw["limits"]
        with self.assertRaises(ConfigError):
            Playbook(raw)

    def test_mode_values(self):
        with self.assertRaises(ConfigError):
            pb(mode="yolo")
        self.assertEqual(Playbook({k: v for k, v in BASE.items() if k != "mode"}).mode, "shadow")

    def test_label_target_is_validated_at_runtime(self):
        p = Playbook(PLAYBOOKS[1])
        ev = event("app")
        ev.labels["container_name"] = "app; rm -rf /"
        self.assertEqual(p.target_for(ev), "")


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.cfg = parse_config(CONFIG)
        self.graph = Graph.from_dict(GRAPH)

    def test_overlapping_matches_rejected(self):
        a = pb()
        b = pb(id="other")
        errs = validate(self.cfg, [a, b], self.graph)
        self.assertTrue(any("same event" in e for e in errs))

    def test_unknown_node_and_rule(self):
        errs = validate(self.cfg, [pb(node="nope")], self.graph, rules={"x"}, services={"zigbee2mqtt"})
        self.assertTrue(any("not in dependencies.yml" in e for e in errs))
        self.assertTrue(any("not an alert rule" in e for e in errs))

    def test_target_must_be_compose_service_and_not_denylisted(self):
        errs = validate(self.cfg, [pb(action={"type": "restart_container", "target": "mosquitto"})],
                        self.graph, services={"zigbee2mqtt"})
        self.assertTrue(any("denylist" in e for e in errs))
        self.assertTrue(any("compose service" in e for e in errs))

    def test_fixed_ephemeral_target_rejected(self):
        with self.assertRaisesRegex(ConfigError, "ephemeral"):
            pb(action={"type": "restart_container", "target": "runner-x-build"})

    def test_config_requires_hard_denylist(self):
        bad = dict(CONFIG, restart_denylist=["gitlab"])
        with self.assertRaisesRegex(ConfigError, "restart_denylist"):
            parse_config(bad)

    def test_page_class(self):
        self.assertEqual(page_class(self.cfg, event("sec")), ("security", True))
        self.assertEqual(page_class(self.cfg, event("zig")), (None, False))


if __name__ == "__main__":
    unittest.main()
