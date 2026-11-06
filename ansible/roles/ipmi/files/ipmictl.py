#!/usr/bin/env python3
"""Keep the router's Supermicro BMC settings as code (in-band IPMI only).

Talks to the local BMC through the kernel IPMI driver (``ipmitool -I open``,
/dev/ipmi0). It never uses the network interface of the BMC and needs no BMC
credentials. Desired state: /etc/ipmictl/desired.yaml, rendered by
ansible/roles/ipmi from ``ipmi_desired`` in the role defaults. See docs/IPMI.md.

Subcommands
  export    print the live settings in the desired-state format (read-only)
  check     compare the live settings with the desired state; exit 2 on drift
  plan      show the ipmitool commands that ``apply --execute`` would run
  apply     the same as plan. Writes only with --execute; refuses under CI
  metrics   write BMC sensor, chassis and SEL metrics (InfluxDB line
            protocol) for Telegraf's file input; logs new SEL entries
  validate  offline: the desired-state file parses and every key is known
  audit     print a read-only inventory of the BMC for a human (not stored)

Only settings marked applyable below are ever written: power restore policy,
fan mode, SOL parameters, the SEL enable bit and sensor thresholds. LAN,
channel access and cipher settings are check-only. Every write goes through
``Ipmi.write``, which refuses anything outside that list (no chassis power or
boot commands, no BMC reset, no SEL clear, no user, LAN or channel changes).

Exit codes: 0 ok / no drift, 1 error, 2 drift (check, apply leftovers),
3 refused by a guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time

DEFAULT_DESIRED = "/etc/ipmictl/desired.yaml"
DEFAULT_METRICS = "/var/lib/ipmictl/bmc.influx"
DEFAULT_STATE = "/var/lib/ipmictl/sel-state.json"
SCHEMA = 1

# --------------------------------------------------------------------------
# ipmitool access
# --------------------------------------------------------------------------

# Read commands (argument prefixes) the tool may send. Everything else is a
# write and goes through Ipmi.write.
READ_PREFIXES = (
    ("lan", "print"),
    ("sol", "info"),
    ("sol", "payload", "status"),
    ("chassis", "status"),
    ("raw", "0x30", "0x45", "0x00"),       # Supermicro: get fan mode
    ("raw", "0x06", "0x41"),               # Get Channel Access
    ("sensor",),
    ("sel", "info"),
    ("sel", "time", "get"),
    ("sel", "elist"),
    ("mc", "info"),
    ("mc", "getenables"),
    ("mc", "watchdog", "get"),
    ("user", "list"),
    ("user", "summary"),
    ("channel", "info"),
    ("channel", "getciphers"),
    ("pef", "status"),
)

# The only writes. Anything not matching one of these is refused, whatever
# called it.
WRITE_PREFIXES = (
    ("chassis", "policy"),                 # power restore policy (not power control)
    ("raw", "0x30", "0x45", "0x01"),       # Supermicro: set fan mode
    ("sol", "set"),
    ("sensor", "thresh"),
    ("mc", "setenables"),
)

FORBIDDEN_WORDS = {
    "power", "reset", "cycle", "soft", "bootdev", "bootparam", "clear",
    "lan", "user", "channel", "setaccess", "password", "identify",
}

POLICY_ARGS = {"always-on", "always-off", "previous"}


class IpmiError(RuntimeError):
    pass


class Refused(RuntimeError):
    pass


def _matches(args, prefixes):
    return any(tuple(args[: len(p)]) == p for p in prefixes)


class Ipmi:
    """Runs ``ipmitool -I open``. ``runner`` is replaceable for tests."""

    def __init__(self, runner=None, timeout=60):
        self.runner = runner or self._run
        self.timeout = timeout
        self.writes = []

    def _run(self, args):
        try:
            p = subprocess.run(
                ["ipmitool", "-I", "open"] + list(args),
                capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError:
            raise IpmiError("ipmitool is not installed")
        except subprocess.TimeoutExpired:
            raise IpmiError("ipmitool %s: timed out" % " ".join(args))
        if p.returncode != 0:
            raise IpmiError("ipmitool %s: %s" % (" ".join(args), (p.stderr or p.stdout).strip()[:300]))
        return p.stdout

    def read(self, *args):
        args = [str(a) for a in args]
        if not _matches(args, READ_PREFIXES):
            raise Refused("not a known read command: ipmitool %s" % " ".join(args))
        return self.runner(args)

    def write(self, *args):
        args = [str(a) for a in args]
        check_write_allowed(args)
        self.writes.append(args)
        return self.runner(args)


def check_write_allowed(args):
    if not _matches(args, WRITE_PREFIXES):
        raise Refused("write refused (not on the allow list): ipmitool %s" % " ".join(args))
    if args[:2] == ["chassis", "policy"]:
        if len(args) != 3 or args[2] not in POLICY_ARGS:
            raise Refused("write refused: chassis policy takes exactly one of %s" % sorted(POLICY_ARGS))
        return
    if args[0] == "raw":
        if len(args) != 5 or args[4] not in FAN_MODE_CODES.values():
            raise Refused("write refused: fan mode raw command with an unknown mode")
        return
    bad = FORBIDDEN_WORDS.intersection(a.lower() for a in args[1:])
    if args[0] == "mc":
        if len(args) != 3 or args[2] not in ("system_event_log=on", "system_event_log=off"):
            raise Refused("write refused: mc setenables only for system_event_log")
        return
    if bad:
        raise Refused("write refused (forbidden word %s): ipmitool %s" % (sorted(bad), " ".join(args)))


# --------------------------------------------------------------------------
# Parsers (pure functions; unit tested)
# --------------------------------------------------------------------------


def parse_kv(text):
    """``Key : value`` lines. Continuation lines (empty key) are skipped."""
    out = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if k and k not in out:
            out[k] = v.strip()
    return out


def parse_hex_bytes(text):
    return [int(b, 16) for b in text.split()]


FAN_MODE_CODES = {"standard": "0x00", "full": "0x01", "optimal": "0x02", "heavyio": "0x04"}
FAN_MODE_NAMES = {int(v, 16): k for k, v in FAN_MODE_CODES.items()}

ACCESS_MODES = {0: "disabled", 1: "pre-boot only", 2: "always available", 3: "shared"}
PRIV_LEVELS = {0: "reserved", 1: "callback", 2: "user", 3: "operator", 4: "administrator", 5: "oem"}


def parse_channel_access(text):
    b = parse_hex_bytes(text)
    if len(b) < 2:
        raise IpmiError("short Get Channel Access response: %r" % text)
    return {
        "access_mode": ACCESS_MODES.get(b[0] & 0x07, "unknown"),
        "alerting": "disabled" if b[0] & 0x20 else "enabled",
        "per_message_auth": "disabled" if b[0] & 0x10 else "enabled",
        "user_level_auth": "disabled" if b[0] & 0x08 else "enabled",
        "privilege_limit": PRIV_LEVELS.get(b[1] & 0x0F, "unknown"),
    }


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


SENSOR_STATUS = {"ok": 0, "nc": 1, "cr": 2, "nr": 3}


def sensor_status_code(raw):
    raw = raw.strip().lower()
    if raw in ("na", ""):
        return None
    if raw.startswith("0x"):
        return None  # discrete: state bits, see discrete_state
    for suffix, code in (("nr", 3), ("cr", 2), ("nc", 1)):
        if raw.endswith(suffix):
            return code
    return SENSOR_STATUS.get(raw, 0)


def sensor_type(name, unit):
    unit = unit.lower()
    lname = name.lower()
    if "degrees" in unit:
        return "temperature"
    if unit == "rpm":
        return "fan"
    if unit == "volts":
        return "voltage"
    if re.match(r"ps\d* status", lname) or "power supply" in lname:
        return "psu"
    if "intru" in lname:
        return "intrusion"
    return "discrete"


def discrete_state(raw):
    """``0x0100`` -> state bits (offsets 0-14). ipmitool prints byte 3 then 4."""
    raw = raw.strip().lower()
    if not raw.startswith("0x") or len(raw) < 6:
        return None
    try:
        return int(raw[2:4], 16) | (int(raw[4:6], 16) << 8)
    except ValueError:
        return None


def parse_sensors(text):
    """``ipmitool sensor`` -> list of dicts (sensors without a reading skipped)."""
    out = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 4:
            continue
        name, value, unit, status = cols[:4]
        if value.lower() == "na":
            continue
        thr = cols[4:10] + ["na"] * (10 - len(cols))
        s = {
            "name": name,
            "unit": unit,
            "type": sensor_type(name, unit),
            "thresholds": {
                k: _num(v) for k, v in zip(("lnr", "lcr", "lnc", "unc", "ucr", "unr"), thr[:6])
            },
        }
        if unit.lower() == "discrete":
            s["state"] = discrete_state(status)
            if s["state"] is None:
                s["state"] = int(_num(value) or 0)
            s["value"] = None
            s["status"] = discrete_status(s["type"], s["state"])
        else:
            s["value"] = _num(value)
            s["status"] = sensor_status_code(status)
        out.append(s)
    return out


# Power supply sensor offsets (IPMI 2.0 table 42-3): 0 presence, 1 failure,
# 2 predictive failure, 3 AC lost, 4 AC lost or out of range, 5 AC out of
# range but present, 6 configuration error.
PSU_FAULT_BITS = 0x7E


def discrete_status(stype, state):
    if stype == "psu":
        if not state & 0x01 or state & PSU_FAULT_BITS:
            return 2
        return 0
    return 0


def parse_sel_info(text):
    kv = parse_kv(text)
    entries = int(kv.get("Entries", "0") or 0)
    used = kv.get("Percent Used", "0%").rstrip("%").strip()
    units = kv.get("# of Alloc Units", "")
    return {
        "entries": entries,
        "capacity": int(units) if units.isdigit() else None,
        "used_pct": float(used) if _num(used) is not None else None,
        "overflow": kv.get("Overflow", "false").lower() == "true",
        "last_add": kv.get("Last Add Time", ""),
        "last_del": kv.get("Last Del Time", ""),
    }


def parse_sel_elist(text):
    out = []
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 6:
            continue
        out.append({
            "id": cols[0],
            "date": cols[1],
            "time": cols[2],
            "sensor": cols[3],
            "event": cols[4],
            "direction": cols[5],
            "detail": " | ".join(cols[6:]),
        })
    return out


_CRIT = re.compile(
    r"critical going|non-recoverable|uncorrectable|failure detected|predictive failure|"
    r"ac lost|power off|thermal trip|ierr|machine check|fatal|bus error|power unit failure",
    re.I,
)
_WARN = re.compile(r"intru|watchdog|non-critical going|presence detected.*deasserted", re.I)
_ECC = re.compile(r"correctable ecc", re.I)


def classify_sel(entry):
    """critical | ecc | warning | info. Deasserted events are info."""
    text = "%s %s" % (entry.get("sensor", ""), entry.get("event", ""))
    if entry.get("direction", "").lower() != "asserted":
        return "info"
    if re.search(r"uncorrectable", text, re.I):
        return "critical"
    if _ECC.search(text):
        return "ecc"
    if re.search(r"non-critical going", text, re.I):
        return "warning"
    if _CRIT.search(text):
        return "critical"
    if _WARN.search(text):
        return "warning"
    return "info"


def sel_key(entry):
    return "%s|%s|%s" % (entry["id"], entry["date"], entry["time"])


def sel_clock_set(text):
    """``sel time get``: a pre-init time (``S+ ...``) means the clock was never set."""
    t = text.strip()
    if not t or t.startswith("S+") or "pre-init" in t.lower():
        return False
    m = re.match(r"(\d\d)/(\d\d)/(\d{4})", t)
    return bool(m) and 2000 <= int(m.group(3)) < 2100


# --------------------------------------------------------------------------
# Live state
# --------------------------------------------------------------------------


def read_live(ipmi, channel=1, sensors=True):
    lan = parse_kv(ipmi.read("lan", "print", channel))
    sol = parse_kv(ipmi.read("sol", "info", channel))
    chassis = parse_kv(ipmi.read("chassis", "status"))
    fan_raw = parse_hex_bytes(ipmi.read("raw", "0x30", "0x45", "0x00"))
    access = parse_channel_access(ipmi.read("raw", "0x06", "0x41", "0x%02x" % channel, "0x40"))
    enables = parse_kv(ipmi.read("mc", "getenables"))

    suites = [s.strip() for s in lan.get("RMCP+ Cipher Suites", "").split(",") if s.strip()]
    privmax = lan.get("Cipher Suite Priv Max", "")
    suite0 = "0" in suites and (not privmax or privmax[0] != "X")

    def ms(v):
        n = _num(v)
        return int(n) if n is not None else v

    live = {
        "schema": SCHEMA,
        "chassis": {"power_restore_policy": chassis.get("Power Restore Policy", "")},
        "fan": {"mode": FAN_MODE_NAMES.get(fan_raw[0] if fan_raw else -1, "unknown")},
        "sol": {
            "channel": channel,
            "enabled": sol.get("Enabled", "").lower() == "true",
            "privilege_level": sol.get("Privilege Level", "").lower(),
            "volatile_bit_rate": sol.get("Volatile Bit Rate (kbps)", ""),
            "non_volatile_bit_rate": sol.get("Non-Volatile Bit Rate (kbps)", ""),
            "character_accumulate_level_ms": ms(sol.get("Character Accumulate Level (ms)")),
            "character_send_threshold": ms(sol.get("Character Send Threshold")),
            "retry_count": ms(sol.get("Retry Count")),
            "retry_interval_ms": ms(sol.get("Retry Interval (ms)")),
        },
        "sel": {"logging_enabled": enables.get("System Event Logging", "").lower() == "enabled"},
        "lan": {
            "channel": channel,
            "ip_source": "dhcp" if "dhcp" in lan.get("IP Address Source", "").lower() else
                         ("static" if "static" in lan.get("IP Address Source", "").lower() else
                          lan.get("IP Address Source", "").lower()),
            "ip_address": lan.get("IP Address", ""),
            "netmask": lan.get("Subnet Mask", ""),
            "default_gateway": lan.get("Default Gateway IP", ""),
            "vlan": lan.get("802.1q VLAN ID", "").lower(),
            "cipher_suite_0": "enabled" if suite0 else "disabled",
        },
        "channel_access": {
            "channel": channel,
            "access_mode": access["access_mode"],
            "privilege_limit": access["privilege_limit"],
        },
    }
    if sensors:
        live["sensor_thresholds"] = {
            s["name"]: {
                "lower": [s["thresholds"][k] for k in ("lnr", "lcr", "lnc")],
                "upper": [s["thresholds"][k] for k in ("unc", "ucr", "unr")],
            }
            for s in parse_sensors(ipmi.read("sensor"))
            if s["unit"].lower() != "discrete"
        }
    return live


# --------------------------------------------------------------------------
# Desired state
# --------------------------------------------------------------------------

# section -> key -> applyable?
KNOWN = {
    "chassis": {"power_restore_policy": True},
    "fan": {"mode": True},
    "sol": {
        "channel": False, "enabled": True, "privilege_level": True,
        "volatile_bit_rate": True, "non_volatile_bit_rate": True,
        "character_accumulate_level_ms": True, "character_send_threshold": True,
        "retry_count": True, "retry_interval_ms": True,
    },
    "sel": {"logging_enabled": True},
    "lan": {
        "channel": False, "ip_source": False, "ip_address": False, "netmask": False,
        "default_gateway": False, "vlan": False, "cipher_suite_0": False,
    },
    "channel_access": {"channel": False, "access_mode": False, "privilege_limit": False},
}

CHECK_ONLY_REASON = {
    "lan": "LAN settings are never changed in-band by this tool (could cut off the BMC)",
    "channel_access": "channel access is never changed by this tool",
}


def load_yaml(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is required (apt install python3-yaml)")
    with open(path) as f:
        return yaml.safe_load(f)


def validate_desired(d):
    errors = []
    if not isinstance(d, dict):
        return ["desired state is not a mapping"]
    if d.get("schema") != SCHEMA:
        errors.append("schema must be %d" % SCHEMA)
    for section, body in d.items():
        if section == "schema":
            continue
        if section == "sensor_thresholds":
            if not isinstance(body, dict):
                errors.append("sensor_thresholds must be a mapping")
                continue
            for name, t in body.items():
                if not isinstance(t, dict) or not set(t) <= {"lower", "upper"} or not t:
                    errors.append("sensor_thresholds.%s: needs lower and/or upper" % name)
                    continue
                for side, vals in t.items():
                    if (not isinstance(vals, list) or len(vals) != 3
                            or any(not isinstance(v, (int, float)) for v in vals)):
                        errors.append("sensor_thresholds.%s.%s: three numbers" % (name, side))
                    elif side == "lower" and not vals[0] <= vals[1] <= vals[2]:
                        errors.append("sensor_thresholds.%s.lower: must be lnr <= lcr <= lnc" % name)
                    elif side == "upper" and not vals[0] <= vals[1] <= vals[2]:
                        errors.append("sensor_thresholds.%s.upper: must be unc <= ucr <= unr" % name)
            continue
        if section not in KNOWN:
            errors.append("unknown section: %s" % section)
            continue
        if not isinstance(body, dict):
            errors.append("%s must be a mapping" % section)
            continue
        for k in body:
            if k not in KNOWN[section]:
                errors.append("unknown key: %s.%s" % (section, k))
    pol = d.get("chassis", {}).get("power_restore_policy")
    if pol is not None and pol not in POLICY_ARGS:
        errors.append("chassis.power_restore_policy must be one of %s" % sorted(POLICY_ARGS))
    mode = d.get("fan", {}).get("mode")
    if mode is not None and mode not in FAN_MODE_CODES:
        errors.append("fan.mode must be one of %s" % sorted(FAN_MODE_CODES))
    if d.get("lan", {}).get("cipher_suite_0", "disabled") != "disabled":
        errors.append("lan.cipher_suite_0 must be 'disabled'")
    return errors


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    n = _num(s)
    return n if n is not None else s.lower()


def diff(desired, live):
    """-> list of dicts: section, key, want, have, applyable."""
    out = []
    for section, body in desired.items():
        if section == "schema":
            continue
        if section == "sensor_thresholds":
            have_all = live.get("sensor_thresholds", {})
            for name, t in body.items():
                have = have_all.get(name)
                for side in ("lower", "upper"):
                    if side not in t:
                        continue
                    if have is None:
                        out.append(dict(section=section, key="%s.%s" % (name, side), want=t[side],
                                        have="(sensor not found)", applyable=False))
                        continue
                    hv = have[side]
                    if any(h is None or abs(float(w) - h) > 0.5 for w, h in zip(t[side], hv)):
                        out.append(dict(section=section, key="%s.%s" % (name, side), want=t[side],
                                        have=hv, applyable=True))
            continue
        for k, want in body.items():
            have = live.get(section, {}).get(k)
            if _norm(want) != _norm(have):
                out.append(dict(section=section, key=k, want=want, have=have,
                                applyable=KNOWN.get(section, {}).get(k, False)))
    return out


def _fmt_bit_rate(v):
    n = _num(v)
    return ("%g" % n) if n is not None else str(v)


def commands_for(d):
    """ipmitool argument lists that bring one drift item to the desired value."""
    s, k, w = d["section"], d["key"], d["want"]
    if not d["applyable"]:
        return []
    if s == "chassis" and k == "power_restore_policy":
        return [["chassis", "policy", w]]
    if s == "fan" and k == "mode":
        return [["raw", "0x30", "0x45", "0x01", FAN_MODE_CODES[w]]]
    if s == "sel" and k == "logging_enabled":
        return [["mc", "setenables", "system_event_log=%s" % ("on" if w else "off")]]
    if s == "sol":
        ch = "1"
        table = {
            "enabled": ("enabled", lambda v: "true" if v else "false"),
            "privilege_level": ("privilege-level", lambda v: str(v).lower()),
            "volatile_bit_rate": ("volatile-bit-rate", _fmt_bit_rate),
            "non_volatile_bit_rate": ("non-volatile-bit-rate", _fmt_bit_rate),
            # ipmitool takes 5 ms units here and 10 ms units for the retry interval
            "character_accumulate_level_ms": ("character-accumulate-level", lambda v: str(int(v) // 5)),
            "character_send_threshold": ("character-send-threshold", lambda v: str(int(v))),
            "retry_count": ("retry-count", lambda v: str(int(v))),
            "retry_interval_ms": ("retry-interval", lambda v: str(int(v) // 10)),
        }
        param, conv = table[k]
        return [["sol", "set", param, conv(w), ch]]
    if s == "sensor_thresholds":
        name, side = k.rsplit(".", 1)
        return [["sensor", "thresh", name, side] + ["%g" % float(x) for x in w]]
    return []


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def _show(v):
    return json.dumps(v) if not isinstance(v, str) else v


def render_text(items):
    lines = []
    for d in items:
        tag = "" if d["applyable"] else "  [check-only]"
        lines.append("DRIFT  %s.%s: want %s, have %s%s" % (d["section"], d["key"], _show(d["want"]),
                                                          _show(d["have"]), tag))
    return "\n".join(lines)


def render_markdown(items, host):
    if not items:
        return "No drift: the BMC on `%s` matches the desired state.\n" % host
    out = [
        "The BMC on `%s` differs from the desired state "
        "(`ansible/roles/ipmi/defaults/main.yml`, `ipmi_desired`).\n" % host,
        "| Setting | Desired | Live | |",
        "|---|---|---|---|",
    ]
    for d in items:
        out.append("| `%s.%s` | `%s` | `%s` | %s |" % (
            d["section"], d["key"], _show(d["want"]), _show(d["have"]),
            "applyable" if d["applyable"] else "check-only"))
    out.append("")
    out.append("Nothing was changed. To fix: update the desired state in the repo if the "
               "live value is intended, or on the router run `ipmictl plan` and then "
               "`ipmictl apply --execute` (applyable rows only). See docs/IPMI.md.")
    return "\n".join(out) + "\n"


def dump_yaml(obj):
    try:
        import yaml
        return yaml.safe_dump(obj, sort_keys=False, default_flow_style=None)
    except ImportError:
        return json.dumps(obj, indent=2) + "\n"


# --------------------------------------------------------------------------
# Metrics (InfluxDB line protocol for Telegraf [[inputs.file]])
# --------------------------------------------------------------------------


def _tag(v):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(v)).strip("_") or "unknown"


def _field(k, v):
    if isinstance(v, bool):
        return "%s=%di" % (k, int(v))
    if isinstance(v, int):
        return "%s=%di" % (k, v)
    if isinstance(v, float):
        return "%s=%s" % (k, repr(v))
    return '%s="%s"' % (k, str(v).replace("\\", "\\\\").replace('"', '\\"'))


def line(measurement, tags, fields, ts_ns):
    t = "".join(",%s=%s" % (k, _tag(v)) for k, v in sorted(tags.items()))
    f = ",".join(_field(k, v) for k, v in fields.items() if v is not None)
    return "%s%s %s %d" % (measurement, t, f, ts_ns)


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def atomic_write(path, data, mode=0o644):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


CATEGORIES = ("critical", "ecc", "warning", "info")


def sel_update(state, info, read_list):
    """New SEL entries since ``state``.

    ``read_list`` is called only when the SEL changed (count, last add or
    last delete time), so an unchanged SEL costs one ``sel info``. The first
    run records a baseline and reports nothing as new. A cleared SEL (fewer
    entries or a new delete time) makes every current entry new.
    -> (new_state, new_entries)
    """
    totals = {c: 0 for c in CATEGORIES}
    if state:
        totals.update(state.get("totals", {}))
    sig = [info["entries"], info["last_add"], info["last_del"]]
    if state and state.get("sig") == sig:
        return dict(state, totals=totals), []
    entries = read_list()
    keys = [sel_key(e) for e in entries]
    if not state:
        return {"sig": sig, "seen": keys, "totals": totals}, []
    cleared = info["entries"] < state["sig"][0] or info["last_del"] != state["sig"][2]
    seen = set() if cleared else set(state.get("seen", []))
    new = [e for e, k in zip(entries, keys) if k not in seen]
    for e in new:
        e["category"] = classify_sel(e)
        totals[e["category"]] += 1
    return {"sig": sig, "seen": keys, "totals": totals}, new


def collect_metrics(ipmi, state, desired=None, now=None):
    """-> (lines, new_state, new_sel_entries)"""
    now = now or time.time()
    ts = int(now * 1e9)
    lines = []

    sensors = parse_sensors(ipmi.read("sensor"))
    for s in sensors:
        tags = {"sensor": s["name"], "type": s["type"]}
        if s["unit"].lower() == "discrete":
            fields = {"state": s["state"], "status": s["status"]}
            if s["type"] == "psu":
                fields["present"] = int(bool(s["state"] & 0x01))
                fields["fault"] = int(bool(s["state"] & PSU_FAULT_BITS))
        else:
            fields = {"value": s["value"], "status": s["status"]}
            if s["type"] == "fan":
                fields["lower_critical"] = s["thresholds"]["lcr"]
            if s["type"] == "temperature":
                fields["upper_critical"] = s["thresholds"]["ucr"]
        lines.append(line("bmc_sensor", tags, fields, ts))

    ch = parse_kv(ipmi.read("chassis", "status"))

    def flag(k):
        return int(ch.get(k, "false").strip().lower() in ("true", "active"))

    lines.append(line("bmc_chassis", {}, {
        "power_on": int(ch.get("System Power", "").lower() == "on"),
        "power_restore_policy": ch.get("Power Restore Policy", ""),
        "power_overload": flag("Power Overload"),
        "main_power_fault": flag("Main Power Fault"),
        "power_control_fault": flag("Power Control Fault"),
        "cooling_fault": flag("Cooling/Fan Fault"),
        "drive_fault": flag("Drive Fault"),
        "intrusion": flag("Chassis Intrusion"),
    }, ts))

    info = parse_sel_info(ipmi.read("sel", "info"))
    enables = parse_kv(ipmi.read("mc", "getenables"))
    clock = sel_clock_set(ipmi.read("sel", "time", "get"))
    new_state, new = sel_update(state, info, lambda: parse_sel_elist(ipmi.read("sel", "elist")))
    counts = {c: 0 for c in CATEGORIES}
    for e in new:
        counts[e["category"]] += 1
    fields = {
        "entries": info["entries"],
        "capacity": info["capacity"],
        "used_pct": info["used_pct"],
        "overflow": int(info["overflow"]),
        "logging_enabled": int(enables.get("System Event Logging", "").lower() == "enabled"),
        "clock_set": int(clock),
        "collected_at": int(now),
    }
    for c in CATEGORIES:
        fields["new_%s" % c] = counts[c]
        fields["%s_total" % c] = int(new_state["totals"][c])
    lines.append(line("bmc_sel", {}, fields, ts))
    for i, e in enumerate(new):
        # one point per new entry; +i ns keeps several in one run apart
        lines.append(line("bmc_sel_event", {"category": e["category"], "sensor": e["sensor"]},
                          {"event": e["event"], "direction": e["direction"],
                           "record": "%s %s %s %s" % (e["id"], e["date"], e["time"], e["detail"])},
                          ts + i))

    if desired is not None:
        try:
            items = diff(desired, read_live(ipmi, sensors=bool(desired.get("sensor_thresholds"))))
            lines.append(line("bmc_config", {}, {"check_ok": 1, "drift_items": len(items)}, ts))
        except (IpmiError, Refused) as exc:
            print("config check failed: %s" % exc, file=sys.stderr)
            lines.append(line("bmc_config", {}, {"check_ok": 0}, ts))
    return lines, new_state, new


# --------------------------------------------------------------------------
# Audit (read-only, printed only; never written to the repo)
# --------------------------------------------------------------------------


def audit(ipmi):
    sections = [
        ("mc info", ("mc", "info")),
        ("lan print 1", ("lan", "print", "1")),
        ("user summary 1", ("user", "summary", "1")),
        ("user list 1", ("user", "list", "1")),
        ("channel info 1", ("channel", "info", "1")),
        ("channel access 1 (raw Get Channel Access, non-volatile)", ("raw", "0x06", "0x41", "0x01", "0x40")),
        ("channel getciphers ipmi 1", ("channel", "getciphers", "ipmi", "1")),
        ("sol info 1", ("sol", "info", "1")),
        ("chassis status", ("chassis", "status")),
        ("fan mode (raw 0x30 0x45 0x00)", ("raw", "0x30", "0x45", "0x00")),
        ("mc getenables", ("mc", "getenables")),
        ("mc watchdog get", ("mc", "watchdog", "get")),
        ("sel info", ("sel", "info")),
        ("sel time get", ("sel", "time", "get")),
        ("sensor", ("sensor",)),
    ]
    for title, args in sections:
        print("=== %s" % title)
        try:
            print(ipmi.read(*args).rstrip())
        except (IpmiError, Refused) as exc:
            print("(failed: %s)" % exc)
        print()
    try:
        entries = parse_sel_elist(ipmi.read("sel", "elist"))
    except (IpmiError, Refused) as exc:
        print("=== SEL summary\n(failed: %s)" % exc)
        return
    counts = {}
    for e in entries:
        k = (classify_sel(e), e["sensor"], e["event"])
        counts[k] = counts.get(k, 0) + 1
    print("=== SEL summary (%d entries; category, count, sensor, event)" % len(entries))
    for (cat, sensor, event), n in sorted(counts.items(), key=lambda x: (CATEGORIES.index(x[0][0]), -x[1])):
        print("%-8s %5d  %s | %s" % (cat, n, sensor, event))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _load_desired(path):
    d = load_yaml(path)
    errs = validate_desired(d)
    if errs:
        raise SystemExit("invalid desired state %s:\n  %s" % (path, "\n  ".join(errs)))
    return d


def cmd_export(a, ipmi):
    sys.stdout.write(dump_yaml(read_live(ipmi)))
    return 0


def cmd_check(a, ipmi):
    desired = _load_desired(a.desired)
    items = diff(desired, read_live(ipmi, sensors=bool(desired.get("sensor_thresholds"))))
    if a.markdown:
        sys.stdout.write(render_markdown(items, os.uname().nodename))
    elif items:
        print(render_text(items))
    if not items and not a.markdown:
        print("ok: BMC matches %s" % a.desired)
    return 2 if items else 0


def cmd_plan(a, ipmi, execute=False):
    desired = _load_desired(a.desired)
    items = diff(desired, read_live(ipmi, sensors=bool(desired.get("sensor_thresholds"))))
    if not items:
        print("ok: no drift, nothing to do")
        return 0
    leftovers = 0
    for d in items:
        cmds = commands_for(d)
        if not cmds:
            leftovers += 1
            reason = CHECK_ONLY_REASON.get(d["section"], "check-only")
            print("SKIP   %s.%s (want %s, have %s): %s" % (d["section"], d["key"], _show(d["want"]),
                                                       _show(d["have"]), reason))
            continue
        for c in cmds:
            check_write_allowed(c)
            if execute:
                print("RUN    ipmitool %s" % " ".join(c))
                ipmi.write(*c)
            else:
                print("WOULD  ipmitool %s" % " ".join(c))
    if execute:
        after = diff(desired, read_live(ipmi, sensors=bool(desired.get("sensor_thresholds"))))
        if after:
            print(render_text(after))
        return 2 if after else 0
    if not execute:
        print("(plan only: nothing was sent; `ipmictl apply --execute` runs the WOULD lines)")
    return 2 if leftovers else 0


def cmd_apply(a, ipmi):
    if not a.execute:
        return cmd_plan(a, ipmi)
    if os.environ.get("CI"):
        print("REFUSED: apply --execute never runs in CI.")
        return 3
    if os.geteuid() != 0:
        print("REFUSED: run as root (in-band IPMI needs /dev/ipmi0).")
        return 3
    return cmd_plan(a, ipmi, execute=True)


def cmd_metrics(a, ipmi):
    state = load_state(a.state)
    desired = None
    if a.desired and os.path.exists(a.desired):
        desired = _load_desired(a.desired)
    lines, new_state, new = collect_metrics(ipmi, state, desired)
    for e in new:
        prio = {"critical": "<2>", "ecc": "<4>", "warning": "<4>"}.get(e["category"], "<6>")
        print("%snew SEL entry [%s] %s | %s | %s | %s %s %s" % (
            prio, e["category"], e["sensor"], e["event"], e["direction"], e["id"], e["date"], e["time"]))
    if a.out == "-":
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        atomic_write(a.out, "\n".join(lines) + "\n", 0o644)
    atomic_write(a.state, json.dumps(new_state) + "\n", 0o600)
    if state is None:
        print("SEL baseline recorded (%d entries); later runs report new entries" % len(new_state["seen"]))
    return 0


def cmd_validate(a, _ipmi):
    d = load_yaml(a.desired)
    errs = validate_desired(d)
    for e in errs:
        print("ERROR  %s" % e)
    if not errs:
        print("ok: %s" % a.desired)
    return 1 if errs else 0


def cmd_audit(a, ipmi):
    audit(ipmi)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ipmictl", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "plan", "apply", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--desired", default=DEFAULT_DESIRED)
        if name == "check":
            p.add_argument("--markdown", action="store_true", help="report for a GitLab issue")
        if name == "apply":
            p.add_argument("--execute", action="store_true", help="actually send the writes")
    sub.add_parser("export")
    sub.add_parser("audit")
    m = sub.add_parser("metrics")
    m.add_argument("--out", default=DEFAULT_METRICS, help="'-' for stdout")
    m.add_argument("--state", default=DEFAULT_STATE)
    m.add_argument("--desired", default=DEFAULT_DESIRED)
    a = ap.parse_args(argv)
    handler = {
        "export": cmd_export, "check": cmd_check, "plan": cmd_plan, "apply": cmd_apply,
        "metrics": cmd_metrics, "validate": cmd_validate, "audit": cmd_audit,
    }[a.cmd]
    ipmi = Ipmi()
    try:
        return handler(a, ipmi)
    except Refused as exc:
        print("REFUSED: %s" % exc, file=sys.stderr)
        return 3
    except IpmiError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
