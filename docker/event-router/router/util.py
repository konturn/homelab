"""Small helpers shared by the router modules."""

import re
import time

_DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value):
    """'30m' -> 1800. Integers are seconds. Anything else raises ValueError."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"negative duration: {value}")
        return value
    m = _DURATION.match(str(value).strip())
    if not m:
        raise ValueError(f"not a duration (want e.g. 30s, 10m, 6h, 1d): {value!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


class Clock:
    """Wall clock; tests substitute a FakeClock."""

    def now(self):
        return time.time()


class FakeClock(Clock):
    def __init__(self, t=1_800_000_000.0):
        self.t = float(t)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds
