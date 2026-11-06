#!/usr/bin/env python3
"""Offline tests for apcctl.py (no card, no Vault)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import apcctl  # noqa: E402

SAMPLE = (HERE / "testdata" / "sample-config.ini").read_text()


class SanitizeTests(unittest.TestCase):
    def setUp(self):
        self.s = apcctl.sanitize(SAMPLE)

    def test_no_secret_survives(self):
        for secret in ("supersecretphrase", "anothersecret", "hunter2", "public"):
            self.assertNotIn(secret, self.s)
        self.assertIn("UserProfile1AuthPhrase=<redacted>", self.s)
        self.assertIn("SuperUserPassword=<redacted>", self.s)
        self.assertIn("DeviceUserPassword=\n", self.s)   # empty stays empty
        self.assertEqual(apcctl.unredacted_secrets(self.s), [])

    def test_volatile_and_comments_dropped(self):
        self.assertNotIn("generated on", self.s)
        self.assertNotIn("Date=11/06/2026", self.s)
        self.assertNotIn("Time=01:02:03", self.s)
        self.assertIn("NTPEnable=enabled", self.s)

    def test_settings_kept_in_order(self):
        self.assertLess(self.s.index("[NetworkTCP/IP]"), self.s.index("[UPSConfiguration]"))
        self.assertIn("ShutdownDelay=90", self.s)
        self.assertIn("UserProfile1AuthProtocol=MD5", self.s)

    def test_idempotent_and_crlf(self):
        self.assertEqual(apcctl.sanitize(self.s), self.s)
        self.assertEqual(apcctl.sanitize(SAMPLE.replace("\n", "\r\n")), self.s)

    def test_unredacted_detection(self):
        self.assertEqual(apcctl.unredacted_secrets("[X]\nFooPassword=abc\nName=x\n"), ["line 2: FooPassword"])

    def test_diff(self):
        self.assertEqual(apcctl.diff(self.s, self.s), "")
        self.assertIn("+ShutdownDelay=60", apcctl.diff(self.s, self.s.replace("ShutdownDelay=90", "ShutdownDelay=60")))

    def test_upload_payload_skips_redacted(self):
        payload, skipped = apcctl.upload_payload(self.s)
        self.assertNotIn(apcctl.REDACTED, payload)
        self.assertEqual(skipped, 5)
        self.assertNotIn("DeviceUserPassword", payload)
        self.assertIn("ShutdownDelay=90\r\n", payload)


class CommandTests(unittest.TestCase):
    def test_drift_without_credentials_is_a_quiet_skip(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("APC_", "VAULT_"))}
        with mock.patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as d:
            rep = os.path.join(d, "r.txt")
            self.assertEqual(apcctl.main(["drift", "--report", rep]), 0)
            self.assertIn("credentials pending", Path(rep).read_text())

    def test_apply_defaults_to_plan_and_refuses_in_ci(self):
        with tempfile.TemporaryDirectory() as d:
            desired = Path(d) / "apc-config.ini"
            desired.write_text(apcctl.sanitize(SAMPLE))
            with mock.patch.object(apcctl, "DESIRED", desired), \
                 mock.patch.object(apcctl, "_scp", side_effect=AssertionError("must not send")):
                self.assertEqual(apcctl.main(["apply"]), 0)
                with mock.patch.dict(os.environ, {"CI": "true"}):
                    self.assertEqual(apcctl.main(["apply", "--upload",
                                                  "--i-understand-this-reconfigures-the-ups"]), 2)
                with mock.patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(apcctl.main(["apply", "--upload"]), 2)

    def test_check_committed_state(self):
        self.assertEqual(apcctl.main(["check"]), 0)


if __name__ == "__main__":
    unittest.main()
