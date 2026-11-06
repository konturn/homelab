"""Offline tests for drift_issue.py. Run:
python3 -m unittest discover -s ci/ipmi -p 'test_*.py' -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drift_issue  # noqa: E402


class FakeGl:
    def __init__(self, issues=None, notes=None):
        self.issues = issues or []
        self.notes = notes or []
        self.calls = []

    def req(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/issues"):
            return self.issues
        if method == "GET" and path.endswith("/notes"):
            return self.notes
        if method == "POST" and path.endswith("/issues"):
            return {"iid": 7}
        return {}


REPORT = "| `fan.mode` | `full` | `optimal` | applyable |\n"


class Tests(unittest.TestCase):
    def writes(self, gl):
        return [(m, p) for m, p, _ in gl.calls if m != "GET"]

    def test_no_drift_no_issue(self):
        gl = FakeGl()
        self.assertEqual(drift_issue.run(gl, 0, "", "j"), 0)
        self.assertEqual(self.writes(gl), [])

    def test_no_drift_closes(self):
        gl = FakeGl(issues=[{"iid": 3}])
        drift_issue.run(gl, 0, "", "j")
        self.assertEqual(self.writes(gl), [("POST", "projects/4/issues/3/notes"), ("PUT", "projects/4/issues/3")])

    def test_drift_opens(self):
        gl = FakeGl()
        drift_issue.run(gl, 2, REPORT, "j")
        (m, p, body), = [c for c in gl.calls if c[0] == "POST"]
        self.assertEqual(p, "projects/4/issues")
        self.assertEqual(body["labels"], "ipmi-drift")
        self.assertIn(drift_issue.MARKER, body["description"])

    def test_drift_same_report_no_note(self):
        gl = FakeGl(issues=[{"iid": 3, "description": drift_issue._body(REPORT, "old-job")}])
        drift_issue.run(gl, 2, REPORT, "new-job")
        self.assertEqual(self.writes(gl), [])

    def test_drift_changed_report_notes(self):
        gl = FakeGl(issues=[{"iid": 3, "description": drift_issue._body(REPORT, "old")}])
        drift_issue.run(gl, 2, REPORT + "| `sol.enabled` | `true` | `false` | applyable |\n", "new")
        self.assertEqual(self.writes(gl), [("POST", "projects/4/issues/3/notes")])

    def test_check_error(self):
        self.assertEqual(drift_issue.main(["--rc", "1", "--report", "/nonexistent"]), 1)


if __name__ == "__main__":
    unittest.main()
