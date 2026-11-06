#!/usr/bin/env python3
"""UPS early-shutdown helper (deployed as /usr/local/sbin/ups-power).

upssched (upssched.conf) runs this as the nut user with a timer name:

  onbatt-confirm   still on battery after the confirm delay: start `watch`
  onbatt-shutdown  on battery for ups_onbatt_shutdown_minutes: `trigger`
  online           back on line power: log only (the watcher exits by itself)

Other subcommands:

  watch            poll ups.status / battery.runtime; ask for a forced
                   shutdown when the runtime stays below the threshold for
                   WATCH_CONSECUTIVE polls while on battery; exit on line power
  trigger REASON   ask upsmon for a forced shutdown (upsmon -c fsd), but only
                   if two reads of ups.status, CONFIRM_GAP_SECONDS apart, both
                   say OB and neither says OL
  selftest         run the UPS battery self-test (ups-selftest.service), only
                   when the UPS is exactly OL with a full battery
  status           print what the guards see right now (read-only)

DRY_RUN=1 (or --dry-run, or the file /etc/nut/ups-power.dry-run, used for
the supervised rehearsal in docs/POWER.md) makes every subcommand log what it
would do and change nothing. The forced shutdown itself is upsmon's: it sets FSD, waits
for secondaries, writes POWERDOWNFLAG and runs SHUTDOWNCMD
(ups-orderly-shutdown). See docs/POWER.md.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import syslog
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

CONF_FILE = os.environ.get("UPS_POWER_CONF", "/etc/nut/ups-power.conf")
DRY_RUN_FILE = os.environ.get("UPS_POWER_DRY_RUN_FILE", "/etc/nut/ups-power.dry-run")
LOCK_FILE = os.environ.get("UPS_POWER_LOCK", "/run/nut/upssched/ups-power-watch.lock")
UPSC = os.environ.get("UPSC_BIN", "/usr/bin/upsc")
UPSMON = os.environ.get("UPSMON_BIN", "/sbin/upsmon")
UPSCMD = os.environ.get("UPSCMD_BIN", "/usr/bin/upscmd")
SELF = os.path.abspath(__file__)

DEFAULTS = {
    "UPS": "apc@localhost",
    "RUNTIME_SHUTDOWN_SECONDS": "600",
    "WATCH_INTERVAL_SECONDS": "10",
    "WATCH_CONSECUTIVE": "3",
    "CONFIRM_GAP_SECONDS": "5",
    "SELFTEST_MIN_CHARGE": "95",
    "SELFTEST_USER": "selftest",
    "SELFTEST_PASSWORD_FILE": "/etc/nut/ups-selftest.secret",
}


# --------------------------------------------------------------------------- pure logic

def status_tokens(status: Optional[str]) -> List[str]:
    """'OB DISCHRG' -> ['OB', 'DISCHRG']; None/'' -> []"""
    return (status or "").split()


def on_battery(status: Optional[str]) -> bool:
    """OB present and OL absent. Anything unreadable is not on battery."""
    t = status_tokens(status)
    return "OB" in t and "OL" not in t


def confirm_on_battery(first: Optional[str], second: Optional[str]) -> Tuple[bool, str]:
    """Guard for every shutdown request: both reads on battery, no FSD yet."""
    for label, s in (("first", first), ("second", second)):
        if s is None:
            return False, f"{label} ups.status read failed"
        if "OL" in status_tokens(s):
            return False, f"{label} ups.status is '{s}' (on line power)"
        if "OB" not in status_tokens(s):
            return False, f"{label} ups.status is '{s}' (not on battery)"
    if "FSD" in status_tokens(second):
        return False, "FSD already set"
    return True, f"ups.status '{first}' then '{second}'"


def parse_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(float(value)) if value is not None and value.strip() != "" else None
    except ValueError:
        return None


def watch_decision(samples: Sequence[Tuple[Optional[str], Optional[int]]], threshold: int,
                   consecutive: int) -> str:
    """Decide from the most recent (ups.status, battery.runtime) samples.

    'exit'    the newest sample says OL (line power is back)
    'trigger' the last `consecutive` samples are all on battery with a
              readable runtime below `threshold`
    'wait'    anything else (including failed reads: upsmon's own NOCOMM and
              LOWBATT handling covers a UPS that cannot be read)
    """
    if not samples:
        return "wait"
    newest_status = samples[-1][0]
    if newest_status is not None and "OL" in status_tokens(newest_status):
        return "exit"
    if consecutive < 1 or len(samples) < consecutive:
        return "wait"
    for status, runtime in samples[-consecutive:]:
        if not on_battery(status) or runtime is None or runtime >= threshold:
            return "wait"
    return "trigger"


def selftest_allowed(status: Optional[str], charge: Optional[int], min_charge: int) -> Tuple[bool, str]:
    t = status_tokens(status)
    if t != ["OL"]:
        return False, f"ups.status is '{status}', need exactly 'OL'"
    if charge is None or charge < min_charge:
        return False, f"battery.charge is {charge}, need >= {min_charge}"
    return True, "OL with a full battery"


# --------------------------------------------------------------------------- environment

def load_conf(path: str = CONF_FILE) -> Dict[str, str]:
    conf = dict(DEFAULTS)
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def dry_run(argv: Sequence[str]) -> bool:
    return (os.environ.get("DRY_RUN", "") not in ("", "0") or "--dry-run" in argv
            or os.path.exists(DRY_RUN_FILE))


def log(msg: str, level: int = syslog.LOG_NOTICE) -> None:
    syslog.openlog("ups-power", syslog.LOG_PID, syslog.LOG_DAEMON)
    syslog.syslog(level, msg)
    print(msg, file=sys.stderr)


def upsc_var(ups: str, var: str) -> Optional[str]:
    try:
        r = subprocess.run([UPSC, ups, var], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


# --------------------------------------------------------------------------- actions

def trigger(conf: Dict[str, str], reason: str, dry: bool,
            read: Callable[[str, str], Optional[str]] = upsc_var,
            sleep: Callable[[float], None] = time.sleep,
            run: Callable[[List[str]], int] = lambda cmd: subprocess.call(cmd)) -> int:
    ups = conf["UPS"]
    first = read(ups, "ups.status")
    sleep(float(conf["CONFIRM_GAP_SECONDS"]))
    second = read(ups, "ups.status")
    ok, why = confirm_on_battery(first, second)
    if not ok:
        log(f"shutdown request ({reason}) ignored: {why}")
        return 0
    cmd = [UPSMON, "-c", "fsd"]
    if dry:
        log(f"DRY_RUN: would request forced shutdown ({reason}; {why}): {' '.join(cmd)}")
        return 0
    log(f"requesting forced shutdown ({reason}; {why})", syslog.LOG_CRIT)
    return run(cmd)


def watch(conf: Dict[str, str], dry: bool,
          read: Callable[[str, str], Optional[str]] = upsc_var,
          sleep: Callable[[float], None] = time.sleep,
          max_polls: Optional[int] = None) -> int:
    ups = conf["UPS"]
    threshold = int(conf["RUNTIME_SHUTDOWN_SECONDS"])
    consecutive = int(conf["WATCH_CONSECUTIVE"])
    interval = float(conf["WATCH_INTERVAL_SECONDS"])
    samples: List[Tuple[Optional[str], Optional[int]]] = []
    log(f"runtime watch started (shutdown below {threshold} s for {consecutive} polls, every {interval:g} s)")
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        samples.append((read(ups, "ups.status"), parse_int(read(ups, "battery.runtime"))))
        samples = samples[-consecutive:]
        d = watch_decision(samples, threshold, consecutive)
        if d == "exit":
            log("runtime watch: line power is back, exiting")
            return 0
        if d == "trigger":
            return trigger(conf, f"battery.runtime {samples[-1][1]} s < {threshold} s", dry, read=read, sleep=sleep)
        sleep(interval)
    return 0


def start_watch(conf: Dict[str, str], dry: bool) -> int:
    """Start one detached watcher (upssched must not block on it)."""
    env = dict(os.environ)
    if dry:
        env["DRY_RUN"] = "1"
    subprocess.Popen([sys.executable, SELF, "watch"], env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("on battery past the confirm delay: runtime watch launched")
    return 0


def watch_locked(conf: Dict[str, str], dry: bool) -> int:
    try:
        fh = open(LOCK_FILE, "w")
    except OSError as e:
        log(f"runtime watch: cannot open lock {LOCK_FILE}: {e}; running unlocked", syslog.LOG_WARNING)
        return watch(conf, dry)
    with fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("runtime watch already running")
            return 0
        return watch(conf, dry)


def selftest(conf: Dict[str, str], dry: bool,
             read: Callable[[str, str], Optional[str]] = upsc_var,
             run: Callable[[List[str]], int] = lambda cmd: subprocess.call(cmd)) -> int:
    ups = conf["UPS"]
    status, charge = read(ups, "ups.status"), parse_int(read(ups, "battery.charge"))
    ok, why = selftest_allowed(status, charge, int(conf["SELFTEST_MIN_CHARGE"]))
    if not ok:
        log(f"self-test skipped: {why}", syslog.LOG_WARNING)
        return 1
    try:
        with open(conf["SELFTEST_PASSWORD_FILE"]) as fh:
            password = fh.read().strip()
    except OSError:
        password = ""
    if not password:
        log(f"self-test skipped: no password in {conf['SELFTEST_PASSWORD_FILE']} "
            "(set ups_selftest_enabled and the Vault field; docs/POWER.md)", syslog.LOG_WARNING)
        return 1
    cmd = [UPSCMD, "-u", conf["SELFTEST_USER"], "-p", password, ups, "test.battery.start"]
    if dry:
        log(f"DRY_RUN: would run {UPSCMD} -u {conf['SELFTEST_USER']} -p *** {ups} test.battery.start ({why})")
        return 0
    log(f"starting UPS battery self-test ({why})")
    return run(cmd)


def show_status(conf: Dict[str, str]) -> int:
    ups = conf["UPS"]
    st, rt = upsc_var(ups, "ups.status"), parse_int(upsc_var(ups, "battery.runtime"))
    print(f"ups={ups} ups.status={st!r} battery.runtime={rt}")
    print(f"on_battery={on_battery(st)} runtime_threshold={conf['RUNTIME_SHUTDOWN_SECONDS']}")
    print(f"watch decision for one sample: {watch_decision([(st, rt)], int(conf['RUNTIME_SHUTDOWN_SECONDS']), 1)}")
    return 0


def main(argv: Sequence[str]) -> int:
    args = [a for a in argv if a != "--dry-run"]
    dry = dry_run(argv)
    conf = load_conf()
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "onbatt-confirm":
        return start_watch(conf, dry)
    if cmd == "onbatt-shutdown":
        return trigger(conf, "on battery for the configured time", dry)
    if cmd == "online":
        log("UPS back on line power; early-shutdown timers cancelled")
        return 0
    if cmd == "watch":
        return watch_locked(conf, dry)
    if cmd == "trigger":
        return trigger(conf, " ".join(args[1:]) or "manual request", dry)
    if cmd == "selftest":
        return selftest(conf, dry)
    if cmd == "status":
        return show_status(conf)
    log(f"unknown command {cmd!r}; doing nothing", syslog.LOG_WARNING)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
