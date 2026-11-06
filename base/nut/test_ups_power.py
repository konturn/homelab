#!/usr/bin/env python3
"""Offline tests for the UPS early-shutdown logic (base/nut).

Nothing here talks to a UPS or upsmon: upsc/upsmon/virsh/docker/shutdown are
stubs. Run: python3 -m unittest discover -s base/nut -p 'test_*.py' -v
"""

import importlib.util
import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ups_power", os.path.join(HERE, "ups-power.py"))
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)

CONF = dict(up.DEFAULTS, CONFIRM_GAP_SECONDS="0", WATCH_INTERVAL_SECONDS="0")


def reader(values):
    """values: {var: [v1, v2, ...]} consumed in order; last value repeats."""
    state = {k: list(v) for k, v in values.items()}

    def read(_ups, var):
        seq = state.get(var, [None])
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return read


class GuardTests(unittest.TestCase):
    def test_on_battery(self):
        self.assertTrue(up.on_battery("OB DISCHRG"))
        self.assertTrue(up.on_battery("OB LB"))
        self.assertFalse(up.on_battery("OL"))
        self.assertFalse(up.on_battery("OL CHRG"))
        self.assertFalse(up.on_battery("OL OB"))   # contradictory: never act
        self.assertFalse(up.on_battery(""))
        self.assertFalse(up.on_battery(None))

    def test_confirm_needs_two_on_battery_reads(self):
        self.assertTrue(up.confirm_on_battery("OB", "OB DISCHRG")[0])
        for a, b in (("OL", "OB"), ("OB", "OL"), ("OB", None), (None, "OB"),
                     ("OB", "OL CHRG"), ("OB", "OB FSD"), ("", "OB"), ("OB", "BYPASS")):
            self.assertFalse(up.confirm_on_battery(a, b)[0], (a, b))

    def test_parse_int(self):
        self.assertEqual(up.parse_int("1980"), 1980)
        self.assertEqual(up.parse_int("599.5"), 599)
        self.assertIsNone(up.parse_int(""))
        self.assertIsNone(up.parse_int(None))
        self.assertIsNone(up.parse_int("n/a"))


class WatchDecisionTests(unittest.TestCase):
    T, N = 600, 3

    def d(self, samples):
        return up.watch_decision(samples, self.T, self.N)

    def test_line_power_exits(self):
        self.assertEqual(self.d([("OB", 300), ("OB", 300), ("OL CHRG", 300)]), "exit")

    def test_needs_consecutive_low_samples(self):
        self.assertEqual(self.d([("OB", 300)]), "wait")
        self.assertEqual(self.d([("OB", 300), ("OB", 300)]), "wait")
        self.assertEqual(self.d([("OB", 300), ("OB", 300), ("OB", 300)]), "trigger")

    def test_one_high_or_unreadable_sample_resets(self):
        self.assertEqual(self.d([("OB", 300), ("OB", 900), ("OB", 300)]), "wait")
        self.assertEqual(self.d([("OB", 300), ("OB", None), ("OB", 300)]), "wait")
        self.assertEqual(self.d([("OB", 300), (None, 300), ("OB", 300)]), "wait")

    def test_threshold_is_strict(self):
        self.assertEqual(self.d([("OB", 600)] * 3), "wait")
        self.assertEqual(self.d([("OB", 599)] * 3), "trigger")

    def test_never_triggers_on_line_power(self):
        for st in ("OL", "OL CHRG", "OL LB", "OL OB"):
            self.assertNotEqual(self.d([(st, 10)] * 3), "trigger", st)

    def test_today_live_values_wait(self):
        # live state when this was written: OL, 1980 s runtime
        self.assertEqual(self.d([("OL", 1980)]), "exit")
        self.assertEqual(self.d([("OB", 1980)] * 3), "wait")


class TriggerTests(unittest.TestCase):
    def run_trigger(self, statuses, dry=False):
        calls = []
        rc = up.trigger(CONF, "test", dry, read=reader({"ups.status": statuses}),
                        sleep=lambda s: None, run=lambda cmd: calls.append(cmd) or 0)
        return rc, calls

    def test_online_never_calls_upsmon(self):
        for seq in (["OL"], ["OL", "OB"], ["OB", "OL"], [None], ["OB", "OB FSD"]):
            rc, calls = self.run_trigger(seq)
            self.assertEqual((rc, calls), (0, []), seq)

    def test_on_battery_requests_fsd(self):
        rc, calls = self.run_trigger(["OB DISCHRG", "OB DISCHRG"])
        self.assertEqual(calls, [[up.UPSMON, "-c", "fsd"]])

    def test_dry_run_never_calls_upsmon(self):
        rc, calls = self.run_trigger(["OB", "OB"], dry=True)
        self.assertEqual((rc, calls), (0, []))


class DryRunTests(unittest.TestCase):
    def test_dry_run_sources(self):
        with tempfile.TemporaryDirectory() as d:
            flag = os.path.join(d, "ups-power.dry-run")
            orig, up.DRY_RUN_FILE = up.DRY_RUN_FILE, flag
            env = os.environ.pop("DRY_RUN", None)
            try:
                self.assertFalse(up.dry_run([]))
                self.assertTrue(up.dry_run(["--dry-run"]))
                open(flag, "w").close()
                self.assertTrue(up.dry_run([]))
                os.unlink(flag)
                os.environ["DRY_RUN"] = "1"
                self.assertTrue(up.dry_run([]))
            finally:
                up.DRY_RUN_FILE = orig
                os.environ.pop("DRY_RUN", None)
                if env is not None:
                    os.environ["DRY_RUN"] = env


class WatchLoopTests(unittest.TestCase):
    def test_watch_triggers_after_consecutive_low_runtime(self):
        triggered = []
        orig = up.trigger
        up.trigger = lambda conf, reason, dry, read=None, sleep=None: triggered.append(reason) or 0
        try:
            read = reader({"ups.status": ["OB"] * 10, "battery.runtime": ["1200", "800", "500", "450", "400"]})
            up.watch(CONF, False, read=read, sleep=lambda s: None, max_polls=10)
        finally:
            up.trigger = orig
        self.assertEqual(len(triggered), 1)
        self.assertIn("400 s < 600 s", triggered[0])

    def test_watch_exits_on_line_power(self):
        read = reader({"ups.status": ["OB", "OB", "OL"], "battery.runtime": ["100"]})
        polls = []
        up.watch(CONF, False, read=read, sleep=lambda s: polls.append(1), max_polls=50)
        self.assertEqual(len(polls), 2)


class SelftestTests(unittest.TestCase):
    def test_selftest_guard(self):
        self.assertTrue(up.selftest_allowed("OL", 100, 95)[0])
        for st, ch in (("OL CHRG", 100), ("OB", 100), ("OL", 90), ("OL", None), (None, 100), ("OL LB", 100)):
            self.assertFalse(up.selftest_allowed(st, ch, 95)[0], (st, ch))

    def test_selftest_without_password_does_nothing(self):
        calls = []
        conf = dict(CONF, SELFTEST_PASSWORD_FILE="/nonexistent/x")
        rc = up.selftest(conf, False, read=reader({"ups.status": ["OL"], "battery.charge": ["100"]}),
                         run=lambda c: calls.append(c) or 0)
        self.assertEqual((rc, calls), (1, []))

    def test_selftest_on_battery_does_nothing(self):
        calls = []
        rc = up.selftest(CONF, False, read=reader({"ups.status": ["OB"], "battery.charge": ["100"]}),
                         run=lambda c: calls.append(c) or 0)
        self.assertEqual((rc, calls), (1, []))


class ConfigTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(HERE, name)) as fh:
            return fh.read()

    def test_upssched_only_acts_on_onbatt_and_online(self):
        lines = [l.split() for l in self.read("upssched.conf").splitlines() if l.startswith("AT ")]
        self.assertTrue(lines)
        for l in lines:
            self.assertIn(l[1], ("ONBATT", "ONLINE"), l)
            if l[1] == "ONLINE":
                self.assertIn(l[3], ("CANCEL-TIMER", "EXECUTE"), l)
            else:
                self.assertEqual(l[3], "START-TIMER", l)
        started = {l[4] for l in lines if l[3] == "START-TIMER"}
        cancelled = {l[4] for l in lines if l[3] == "CANCEL-TIMER"}
        self.assertEqual(started, cancelled, "every ONBATT timer must be cancelled on ONLINE")
        self.assertTrue(started <= {"onbatt-confirm", "onbatt-shutdown"})

    def test_upsmon_shutdown_uses_execute_and_powerdownflag(self):
        s = self.read("upsmon.conf")
        self.assertRegex(s, r'(?m)^SHUTDOWNCMD "/usr/local/sbin/ups-orderly-shutdown --execute"$')
        self.assertRegex(s, r"(?m)^POWERDOWNFLAG /etc/killpower$")
        self.assertRegex(s, r"(?m)^MONITOR \{\{ ups_nut_name \}\}@localhost 1 upsmon \{\{ vault_nut_monitor_password \}\} primary$")
        self.assertRegex(s, r"(?m)^NOTIFYCMD /etc/nut/notify.sh$")

    def test_ups_conf_uses_ip_not_name(self):
        s = self.read("ups.conf")
        self.assertRegex(s, r"(?m)^\[\{\{ ups_nut_name \}\}\]$")
        self.assertRegex(s, r"(?m)^\s+port = \{\{ ups_address \}\}$")
        self.assertNotIn("cyberpower", s)

    def test_selftest_user_is_conditional(self):
        s = self.read("upsd.users")
        self.assertRegex(s, r"\{% if ups_selftest_enabled .*%\}\s*\n(?:.*\n)*?\[selftest\]")
        self.assertNotRegex(s, r"instcmds\s*=\s*all")


class OrderlyShutdownScriptTests(unittest.TestCase):
    SCRIPT = os.path.join(HERE, "ups-orderly-shutdown.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.calls = os.path.join(self.tmp, "calls")
        self.state = os.path.join(self.tmp, "vms")

    def stub(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + f'echo "{name} $*" >> "{self.calls}"\n' + textwrap.dedent(body))
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        return p

    def run_script(self, args, runtime="1980", vms="excel-vm", vm_stops=True, extra_env=None):
        with open(self.state, "w") as fh:
            fh.write(vms)
        upsc = self.stub("upsc", f"""
            case "$2" in
              ups.status) echo OB DISCHRG ;;
              battery.charge) echo 90 ;;
              battery.runtime) echo {runtime} ;;
            esac
        """)
        stop = "true" if vm_stops else "false"
        virsh = self.stub("virsh", f"""
            case "$1" in
              list) cat "{self.state}" ;;
              shutdown) {stop} && : > "{self.state}" ;;
              destroy) : > "{self.state}" ;;
            esac
        """)
        docker = self.stub("docker", "exit 0\n")
        shut = self.stub("shutdown", "exit 0\n")
        sync = self.stub("sync", "exit 0\n")
        logger = self.stub("logger", "exit 0\n")
        sleep = self.stub("sleep", "exit 0\n")
        compose = os.path.join(self.tmp, "compose.yml")
        open(compose, "w").close()
        env = dict(os.environ, UPSC_BIN=upsc, VIRSH_BIN=virsh, DOCKER_BIN=docker, SHUTDOWN_BIN=shut,
                   SYNC_BIN=sync, LOGGER_BIN=logger, SLEEP_BIN=sleep, COMPOSE_FILE=compose,
                   LOG_FILE=os.path.join(self.tmp, "log"), VM_TIMEOUT="9", POLL_SECONDS="3")
        env.pop("DRY_RUN", None)
        env.update(extra_env or {})
        r = subprocess.run(["bash", self.SCRIPT] + args, env=env, capture_output=True, text=True, timeout=60)
        try:
            with open(self.calls) as fh:
                calls = fh.read().splitlines()
        except FileNotFoundError:
            calls = []
        return r, calls

    def mutating(self, calls):
        return [c for c in calls if re.match(r"(virsh (shutdown|destroy)|docker (compose|stop)|shutdown|sync)\b", c)]

    def test_default_is_dry_run(self):
        r, calls = self.run_script([])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.mutating(calls), [])
        self.assertIn("DRY_RUN: would run:", r.stderr)
        self.assertIn("shutdown -h +0", r.stderr)

    def test_dry_run_env_overrides_execute(self):
        r, calls = self.run_script(["--execute"], extra_env={"DRY_RUN": "1"})
        self.assertEqual(self.mutating(calls), [])

    def test_execute_order(self):
        r, calls = self.run_script(["--execute"])
        self.assertEqual(r.returncode, 0, r.stderr)
        m = self.mutating(calls)
        self.assertEqual(m[0], "virsh shutdown excel-vm")
        self.assertTrue(m[1].startswith("docker compose -p docker -f "), m)
        self.assertIn("stop -t 30", m[1])
        self.assertEqual(m[2], "sync ")
        self.assertTrue(m[3].startswith("shutdown -h +0"), m)
        self.assertEqual(len(m), 4, m)

    def test_hung_vm_is_destroyed_then_shutdown_still_runs(self):
        r, calls = self.run_script(["--execute"], vm_stops=False)
        m = self.mutating(calls)
        self.assertIn("virsh destroy excel-vm", m)
        self.assertTrue(m[-1].startswith("shutdown -h +0"), m)

    def test_low_runtime_uses_fast_timeouts(self):
        r, calls = self.run_script(["--execute"], runtime="120")
        self.assertIn("fast mode", r.stderr)
        self.assertTrue(any("stop -t 10" in c for c in self.mutating(calls)))

    def test_bad_argument_does_nothing(self):
        r, calls = self.run_script(["--now"])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.mutating(calls), [])


if __name__ == "__main__":
    unittest.main()
