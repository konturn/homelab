"""Tests for the per-service rollback hold (docs/AUTOMERGE.md).

Run: python3 -m unittest discover -s ci/automerge -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import automerge  # noqa: E402


def verdict(job, *services):
    return SimpleNamespace(deploy_jobs=[job], changes=[SimpleNamespace(service=s) for s in services])


class RollbackScope(unittest.TestCase):
    def test_marker(self):
        issue = {"title": "anything", "description": "x\n<!-- automerge-rollback job=router services=tautulli,plex -->\n"}
        self.assertEqual(automerge.rollback_scope(issue), {("router", "tautulli"), ("router", "plex")})

    def test_title_fallback(self):
        issue = {"title": "Auto-merge rollback: tautulli on router (!1006)", "description": "no marker"}
        self.assertEqual(automerge.rollback_scope(issue), {("router", "tautulli")})

    def test_title_fallback_multi(self):
        issue = {"title": "Auto-merge rollback: loki, tempo on satellite-2 (!12)"}
        self.assertEqual(automerge.rollback_scope(issue), {("satellite-2", "loki"), ("satellite-2", "tempo")})

    def test_unknown_scope(self):
        self.assertIsNone(automerge.rollback_scope({"title": "Rollback by hand", "description": ""}))


class RollbackGate(unittest.TestCase):
    holds = [({"iid": 153}, {("router", "tautulli")})]

    def test_same_service_held(self):
        self.assertIn("#153", automerge.rollback_gate(self.holds, verdict("router", "tautulli")))

    def test_other_service_passes(self):
        self.assertIsNone(automerge.rollback_gate(self.holds, verdict("router", "sonarr")))

    def test_same_service_other_host_passes(self):
        self.assertIsNone(automerge.rollback_gate(self.holds, verdict("zwave", "tautulli")))

    def test_unknown_scope_holds_everything(self):
        holds = [({"iid": 9}, None)]
        self.assertIn("#9", automerge.rollback_gate(holds, verdict("router", "sonarr")))

    def test_no_holds(self):
        self.assertIsNone(automerge.rollback_gate([], verdict("router", "tautulli")))


if __name__ == "__main__":
    unittest.main()
