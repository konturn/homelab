#!/usr/bin/env python3
"""One firmware rollout per night, shared by the NETGEAR, Aruba Instant and S2500 jobs.

Every firmware job that can reboot network gear runs on the protected runner,
whose /cache is the same host directory for all jobs. This module keeps two
things there (FIRMWARE_NIGHT_DIR, default /cache/firmware-night):

- ``lock``: an flock held for the whole rollout. A second rollout that starts
  while one is running does nothing (the switches and the Wi-Fi are never
  rebooted at the same time, and only one job binds UDP 69 on the router).
- ``last.json``: which rollout changed devices on which night. A *scheduled*
  rollout does nothing on a night when another family already upgraded (or
  tried to upgrade) something, so a bad night is never made worse by a second
  reboot of different gear. Started by hand, a rollout only waits for the lock.

A night is the local calendar date: the windows (04:45-06:00) do not cross
midnight.
"""
from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path
from typing import Optional

DEFAULT_DIR = "/cache/firmware-night"


class Busy(Exception):
    """Another rollout holds the lock, or already used tonight."""


def night(now: Optional[time.struct_time] = None) -> str:
    return time.strftime("%Y-%m-%d", now or time.localtime())


def blocking_use(last: Optional[dict], family: str, tonight: str) -> Optional[str]:
    """Why a scheduled rollout of <family> must not run tonight, or None."""
    if not last or last.get("night") != tonight or last.get("family") == family:
        return None
    return (f"{last.get('family')} firmware already changed devices tonight ({tonight}, "
            f"{last.get('what') or 'rollout'}); one rollout per night")


class NightLock:
    """Context manager: ``with NightLock("aruba-iap", scheduled=True) as lk: ...; lk.used("upgraded")``."""

    def __init__(self, family: str, scheduled: bool, directory: Optional[str] = None, log=print):
        self.family, self.scheduled, self.log = family, scheduled, log
        self.dir = Path(directory or os.environ.get("FIRMWARE_NIGHT_DIR") or DEFAULT_DIR)
        self._fh = None

    @property
    def last_path(self) -> Path:
        return self.dir / "last.json"

    def read_last(self) -> Optional[dict]:
        try:
            return json.loads(self.last_path.read_text())
        except (OSError, ValueError):
            return None

    def __enter__(self):
        import fcntl
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.dir / "lock", "a+")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._fh.close()
            self._fh = None
            if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                holder = ""
                try:
                    holder = (self.dir / "holder").read_text().strip()
                except OSError:
                    pass
                raise Busy(f"another firmware rollout is running ({holder or 'unknown job'})") from None
            raise
        if self.scheduled:
            why = blocking_use(self.read_last(), self.family, night())
            if why:
                self._release()
                raise Busy(why)
        try:
            (self.dir / "holder").write_text(f"{self.family} {os.environ.get('CI_JOB_URL', os.getpid())}\n")
        except OSError:
            pass
        return self

    def used(self, what: str) -> None:
        """Record that this rollout changed (or tried to change) devices tonight."""
        rec = {"family": self.family, "night": night(), "what": what,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "job": os.environ.get("CI_JOB_URL")}
        tmp = self.last_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec) + "\n")
        tmp.replace(self.last_path)

    def _release(self):
        if self._fh:
            import fcntl
            try:
                (self.dir / "holder").write_text("")
            except OSError:
                pass
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __exit__(self, *exc):
        self._release()
        return False
