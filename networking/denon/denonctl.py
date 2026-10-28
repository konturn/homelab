#!/usr/bin/env python3
"""Read the Denon AVR-X6700H configuration and keep it as code.

The desired state is ``denon-x6700h.yaml`` in this directory: the settings
the receiver reports over its telnet control port (TCP 23) and the HTTP
``goform/AppCommand.xml`` interface, normalised. Python stdlib only.

Subcommands
  export   read the live settings and write them to the YAML file (or stdout)
  drift    compare the live settings with the YAML file; exit 2 on drift
  check    offline: the YAML parses and every value maps to a known setting
  plan     show the telnet commands that would bring the receiver to the YAML
  apply    the same as plan (dry run). Sends only with --send AND the
           environment variable DENON_APPLY_ENABLE=1; refuses under CI

Everything except ``apply --send`` is read-only: it sends nothing but
queries ending in ``?`` (telnet) and ``Get*`` commands (HTTP). See
docs/denon.md.

Exit codes: 0 ok / no differences / skipped (receiver in standby),
1 error, 2 drift (drift) or pending changes (plan --detailed-exitcode),
3 refused by a guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DESIRED = os.path.join(HERE, "denon-x6700h.yaml")
DEFAULT_HOST = "denon.lab.nkontur.com"
TELNET_PORT = 23
HTTP_PORT = 8080
SCHEMA = 1

# --------------------------------------------------------------------------
# Value encodings
# --------------------------------------------------------------------------
# Denon levels are "50" = 0 dB, one unit per dB, a trailing 5 on a
# three-digit value is a half step: "565" = +6.5 dB, "465" = -3.5 dB.


def level_to_db(raw: str) -> str:
    raw = raw.strip()
    if not raw.isdigit() or len(raw) not in (2, 3):
        raise ValueError("not a Denon level: %r" % raw)
    v = int(raw[:2]) + (0.5 if len(raw) == 3 and raw[2] == "5" else 0.0)
    return "%+.1f" % (v - 50) if v != 50 else "0.0"


def db_to_level(db: str) -> str:
    v = float(db) + 50
    whole = int(v)
    if abs(v - whole - 0.5) < 1e-6:
        return "%02d5" % whole
    if abs(v - whole) > 1e-6:
        raise ValueError("level must be a multiple of 0.5 dB: %r" % db)
    return "%02d" % whole


RAW = (lambda s: s.strip(), lambda s: s)
DB = (level_to_db, db_to_level)

# --------------------------------------------------------------------------
# Query table
# --------------------------------------------------------------------------
# Each telnet query has extractors. A single extractor reads one line that
# starts with ``prefix``; the set command is ``prefix + value``. A multi
# extractor reads ``prefix<KEY> <value>`` lines up to ``prefix END`` (or
# ``prefixEND``) into a mapping; the set command is ``prefix + KEY + " " +
# value``. Paths starting with "per_input" are stored under the input that
# is selected at export time (Denon keeps them per input source).
#
# protected: speaker/HDMI setup values written by Audyssey calibration or
#            the setup wizard. apply refuses them without --allow-setup.
# unverified: the set form is inferred from the query form and has not been
#            tested on this firmware. apply marks them in the plan.


@dataclass(frozen=True)
class Ext:
    prefix: str
    path: tuple
    multi: bool = False
    codec: tuple = RAW
    protected: bool = False
    unverified: bool = False
    drift: bool = True  # False: exported, but ignored by drift
    apply: bool = True  # False: exported, never sent by apply
    join: bool = False  # runtime only: all matching lines, joined with ","


@dataclass(frozen=True)
class Query:
    cmd: str
    exts: tuple


def S(prefix, *path, **kw):
    return Ext(prefix, path, **kw)


def M(prefix, *path, **kw):
    return Ext(prefix, path, multi=True, **kw)


PI = "per_input"

QUERIES: tuple = (
    # device information (not drift-checked, never applied)
    Query("NSFRN ?", (S("NSFRN ", "device", "friendly_name", drift=False, apply=False),)),
    Query("SSINFFRM ?", (M("SSINFFRM", "device", "firmware", drift=False, apply=False),)),
    # sources: rename and use/delete (SOURCE DELETE in the setup menu)
    Query("SSFUN ?", (M("SSFUN", "sources", "names", unverified=True),)),
    Query("SSSOD ?", (M("SSSOD", "sources", "use", unverified=True),)),
    # speakers (setup values, mostly written by Audyssey)
    Query("SSSPC ?", (M("SSSPC", "speakers", "config", protected=True, unverified=True),)),
    Query("SSCFR ?", (M("SSCFR", "speakers", "crossover", protected=True, unverified=True),)),
    Query("SSLEV ?", (M("SSLEV", "speakers", "levels_db", codec=DB, protected=True, unverified=True),)),
    Query("SSSDE ?", (M("SSSDE", "speakers", "distances_raw", protected=True, unverified=True),)),
    Query("SSSWM ?", (S("SSSWM ", "speakers", "subwoofer_mode", protected=True, unverified=True),)),
    Query("SSLFL ?", (S("SSLFL ", "speakers", "lfe_lowpass_hz", protected=True, unverified=True),)),
    Query("SSPAA ?", (M("SSPAA", "speakers", "amp_assign", protected=True, unverified=True),)),
    Query("SPPR ?", (S("SPPR ", "speakers", "preset", protected=True),)),
    # HDMI setup
    Query("SSHOS ?", (M("SSHOS", "hdmi", protected=True, unverified=True),)),
    # video
    Query("VSASP ?", (S("VSASP", "video", "aspect"),)),
    Query("VSMONI ?", (S("VSMONI", "video", "monitor_out"),)),
    Query("VSSC ?", (S("VSSC", "video", "resolution"),)),
    Query("VSSCH ?", (S("VSSCH", "video", "resolution_hdmi"),)),
    Query("VSAUDIO ?", (S("VSAUDIO ", "video", "hdmi_audio_out"),)),
    Query("VSVPM ?", (S("VSVPM", "video", "processing_mode"),)),
    # system
    Query("ECO?", (S("ECO", "system", "eco"),)),
    Query("DIM ?", (S("DIM ", "system", "dimmer"),)),
    Query("STBY?", (S("STBY", "system", "auto_standby"),)),
    Query("SSLOC ?", (S("SSLOC ", "system", "setup_lock", protected=True, unverified=True),)),
    Query("BTTX ?", (S("BTTX ", "system", "bluetooth_tx"),)),
    # zones 2 and 3 (power, source and volume are runtime, see RUNTIME)
    Query("Z2CS?", (S("Z2CS", "zone2", "channel_setting"),)),
    Query("Z2CV?", (M("Z2CV", "zone2", "levels_db", codec=DB),)),
    Query("Z2HPF?", (S("Z2HPF", "zone2", "hpf"),)),
    Query("Z2PSBAS ?", (S("Z2PSBAS ", "zone2", "bass_db", codec=DB),)),
    Query("Z2PSTRE ?", (S("Z2PSTRE ", "zone2", "treble_db", codec=DB),)),
    Query("Z2STBY?", (S("Z2STBY", "zone2", "auto_standby"),)),
    Query("Z3CS?", (S("Z3CS", "zone3", "channel_setting"),)),
    Query("Z3CV?", (M("Z3CV", "zone3", "levels_db", codec=DB),)),
    Query("Z3HPF?", (S("Z3HPF", "zone3", "hpf"),)),
    Query("Z3PSBAS ?", (S("Z3PSBAS ", "zone3", "bass_db", codec=DB),)),
    Query("Z3PSTRE ?", (S("Z3PSTRE ", "zone3", "treble_db", codec=DB),)),
    Query("Z3STBY?", (S("Z3STBY", "zone3", "auto_standby"),)),
    # per input source (main zone)
    Query("MS?", (S("MS", PI, "sound_mode", drift=False),)),
    Query("SD?", (S("SD", PI, "input_mode"),)),
    Query("PSMULTEQ: ?", (S("PSMULTEQ:", PI, "audyssey", "multeq"),)),
    Query("PSDYNEQ ?", (S("PSDYNEQ ", PI, "audyssey", "dynamic_eq"),)),
    Query("PSREFLEV ?", (S("PSREFLEV ", PI, "audyssey", "reference_level_offset"),)),
    Query("PSDYNVOL ?", (S("PSDYNVOL ", PI, "audyssey", "dynamic_volume"),)),
    Query("PSLFC ?", (S("PSLFC ", PI, "audyssey", "lfc"),)),
    Query("PSCNTAMT ?", (S("PSCNTAMT ", PI, "audyssey", "containment_amount"),)),
    Query("PSGEQ ?", (S("PSGEQ ", PI, "graphic_eq"),)),
    Query("PSTONE CTRL ?", (S("PSTONE CTRL ", PI, "tone", "control"),)),
    Query("PSBAS ?", (S("PSBAS ", PI, "tone", "bass_db", codec=DB),)),
    Query("PSTRE ?", (S("PSTRE ", PI, "tone", "treble_db", codec=DB),)),
    Query("CV?", (M("CV", PI, "levels", "channels_db", codec=DB),)),
    Query("PSSWL ?", (
        S("PSSWL ", PI, "levels", "subwoofer_db", codec=DB),
        S("PSSWL2 ", PI, "levels", "subwoofer2_db", codec=DB),
    )),
    Query("PSSWR ?", (S("PSSWR ", PI, "levels", "subwoofer_on"),)),
    Query("PSLFE ?", (S("PSLFE ", PI, "levels", "lfe_attenuation"),)),
    Query("PSDIC ?", (S("PSDIC ", PI, "levels", "dialog_enhancer"),)),
    Query("PSCINEMA EQ. ?", (S("PSCINEMA EQ.", PI, "surround", "cinema_eq"),)),
    Query("PSDRC ?", (S("PSDRC ", PI, "surround", "dynamic_compression"),)),
    Query("PSDEH ?", (S("PSDEH ", PI, "surround", "dialog_enhancer_level"),)),
    Query("PSBSC ?", (S("PSBSC ", PI, "surround", "bass_sync"),)),
    Query("PSDEL ?", (S("PSDEL ", PI, "surround", "delay"),)),
    Query("PSDELAY ?", (S("PSDELAY ", PI, "surround", "audio_delay_ms"),)),
    Query("PSEFF ?", (S("PSEFF ", PI, "surround", "effect"),)),
    Query("PSNEURAL ?", (S("PSNEURAL ", PI, "surround", "neural_x"),)),
    Query("PSAUROPR ?", (S("PSAUROPR ", PI, "surround", "auro_preset"),)),
    Query("PSAUROST ?", (S("PSAUROST ", PI, "surround", "auro_strength"),)),
    Query("PSAUROMODE ?", (S("PSAUROMODE ", PI, "surround", "auro_mode"),)),
    Query("PSSP: ?", (S("PSSP:", PI, "surround", "speaker_output"),)),
    Query("PSLOM ?", (S("PSLOM ", PI, "surround", "loudness_management"),)),
    Query("PSHEQ ?", (S("PSHEQ ", PI, "surround", "headphone_eq"),)),
    Query("PSSPV ?", (S("PSSPV ", PI, "surround", "speaker_virtualizer"),)),
    Query("PSRSTR ?", (S("PSRSTR ", PI, "surround", "audio_restorer"),)),
)

# Runtime state: read for context (and `export --runtime`), never written
# to the YAML file, never compared, never applied.
RUNTIME: tuple = (
    Query("PW?", (S("PW", "power"),)),
    Query("ZM?", (S("ZM", "main_zone"),)),
    Query("SI?", (S("SI", "input"),)),
    Query("MV?", (S("MVMAX ", "volume_max"), S("MV", "volume"))),
    Query("MU?", (S("MU", "mute"),)),
    Query("SLP?", (S("SLP", "sleep"),)),
    Query("MSQUICK ?", (S("MSQUICK", "quick_select"),)),
    Query("TR?", (S("TR1 ", "trigger1"), S("TR2 ", "trigger2"))),
    Query("Z2?", (S("Z2", "zone2", join=True),)),
    Query("Z3?", (S("Z3", "zone3", join=True),)),
)

# Anything apply builds must start with one of the table's prefixes and none
# of these (power, input, volume, menu, system/reset, tuner, network).
FORBIDDEN_PREFIXES = (
    "PW", "ZM", "SI", "MV", "MU", "MN", "SY", "RST", "TF", "TM", "TP", "NS",
    "SSTTR", "SLP", "Z2ON", "Z2OFF", "Z3ON", "Z3OFF", "Z2MU", "Z3MU",
    "MSQUICK", "Z2QUICK", "Z3QUICK", "PV", "UG",
)

# --------------------------------------------------------------------------
# Parsing telnet responses
# --------------------------------------------------------------------------


def _set_path(d: dict, path: tuple, value):
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = value


def _get_path(d: dict, path: tuple):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


def parse_multi(prefix: str, lines: list, codec=RAW) -> dict | None:
    """``prefix<KEY> <value>`` lines -> {KEY: value}. Repeated keys are
    joined with ",". A line with no key (``SSCFR IDV``) is stored as
    ``_``. Returns None when no line matched."""
    out: dict = {}
    seen = False
    for line in lines:
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):]
        if rest.strip() == "END":
            seen = True
            continue
        if " " in rest:
            key, value = rest.split(" ", 1)
        else:  # no value separator: whole rest is the key, e.g. "CVEND"
            continue
        key = key or "_"
        value = codec[0](value) if key != "_" else value.strip()
        seen = True
        out[key] = value if key not in out else out[key] + "," + value
    return out if seen else None


def parse_single(prefix: str, lines: list, all_prefixes: tuple, codec=RAW):
    """Last line starting with ``prefix`` and not with a longer prefix of
    the same query (``PSSWL `` vs ``PSSWL2 ``, ``MVMAX`` vs ``MV``)."""
    value = None
    longer = [p for p in all_prefixes if p != prefix and p.startswith(prefix)]
    for line in lines:
        if line.startswith(prefix) and not any(line.startswith(p) for p in longer):
            if prefix in ("MS",) and line.startswith(("MSQUICK", "MSSMART")):
                continue
            value = codec[0](line[len(prefix):])
    return value


def apply_query(result: dict, q: Query, lines: list) -> list:
    """Store a query's values in ``result``; return the paths not read."""
    missing = []
    prefixes = tuple(e.prefix for e in q.exts)
    for e in q.exts:
        try:
            if e.multi:
                v = parse_multi(e.prefix, lines, e.codec)
            elif e.join:
                v = ",".join(l[len(e.prefix):].strip() for l in lines if l.startswith(e.prefix)) or None
            else:
                v = parse_single(e.prefix, lines, prefixes, e.codec)
        except ValueError:  # unexpected encoding: keep the raw values
            v = parse_multi(e.prefix, lines) if e.multi else parse_single(e.prefix, lines, prefixes)
        if v is None or v == {}:
            missing.append(e.path)
        else:
            _set_path(result, e.path, v)
    return missing


# --------------------------------------------------------------------------
# Live access
# --------------------------------------------------------------------------


class Telnet:
    """One short telnet session. The receiver accepts more than one client
    on this firmware, but keep sessions short so Home Assistant is not held
    off on firmware that allows only one."""

    def __init__(self, host: str, port: int = TELNET_PORT, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(0.2)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _read(self, done, wait: float, idle: float = 0.4) -> list:
        buf = b""
        end = time.time() + wait
        last = time.time()
        while time.time() < end:
            try:
                d = self.sock.recv(8192)
                if not d:
                    break
                buf += d
                last = time.time()
                if done(buf):
                    break
            except socket.timeout:
                if buf and time.time() - last > idle:
                    break
        return [l for l in buf.decode("ascii", "replace").split("\r") if l.strip()]

    def query(self, q: Query) -> list:
        cmd = q.cmd
        if not cmd.endswith("?") or cmd.startswith("MN"):
            raise RuntimeError("refusing non-query command %r" % cmd)
        self.sock.sendall((cmd + "\r").encode("ascii"))
        multi = any(e.multi for e in q.exts)
        ends = [(e.prefix + " END", e.prefix + "END") for e in q.exts if e.multi]

        def done(buf):
            text = buf.decode("ascii", "replace")
            if multi:
                return all(any(x + "\r" in text for x in pair) for pair in ends)
            return False

        return self._read(done, wait=4.0 if multi else 1.0)

    def send(self, cmd: str) -> list:
        self.sock.sendall((cmd + "\r").encode("ascii"))
        return self._read(lambda b: False, wait=0.8)


def http_appcommand(host: str, cmds: list, port: int = HTTP_PORT, timeout: float = 10) -> ET.Element:
    """POST Get* commands to goform/AppCommand.xml. The firmware only parses
    the request when each element is on its own line."""
    for c in cmds:
        if not c.startswith("Get"):
            raise RuntimeError("refusing non-Get AppCommand %r" % c)
    body = '<?xml version="1.0" encoding="utf-8"?>\n<tx>\n%s\n</tx>\n' % "\n".join(
        '<cmd id="1">%s</cmd>' % c for c in cmds)
    req = urllib.request.Request(
        "http://%s:%d/goform/AppCommand.xml" % (host, port),
        data=body.encode(), headers={"Content-Type": "text/xml"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return ET.fromstring(r.read())


WEB_CMDS = ["GetRenameSource", "GetDeletedSource", "GetZoneName", "GetQuickSelectName"]


def parse_web(rx: ET.Element) -> dict:
    """GetRenameSource, GetDeletedSource, GetZoneName, GetQuickSelectName ->
    {sources: {name: {rename, use}}, zone_names: {...}, quick_select: {...}}.
    The firmware repeats some source entries; duplicates are dropped."""
    out: dict = {}
    src: dict = {}
    for lst in rx.iter("functionrename"):
        for item in lst.iter("list"):
            name = (item.findtext("name") or "").strip()
            if name:
                src.setdefault(name, {})["rename"] = (item.findtext("rename") or "").strip()
    for lst in rx.iter("functiondelete"):
        for item in lst.iter("list"):
            name = (item.findtext("name") or "").strip()
            if name:
                use = (item.findtext("use") or "").strip()
                src.setdefault(name, {})["use"] = {"1": "USE", "0": "DEL"}.get(use, use)
    if src:
        out["sources"] = src
    for cmd in rx.findall("cmd"):
        zones = {c.tag: (c.text or "").strip() for c in cmd if re.match(r"^zone\d$", c.tag)}
        if zones and not any(c.tag.startswith("zone") and c.text and c.text.strip() in ("ON", "OFF")
                             for c in cmd):
            out["zone_names"] = zones
        qs = {}
        for c in cmd:
            m = re.match(r"^(Name|Source)(\d)$", c.tag)
            if m:
                qs.setdefault(m.group(2), {})[m.group(1).lower()] = (c.text or "").strip()
        if qs:
            out["quick_select"] = qs
    return out


@dataclass
class Snapshot:
    settings: dict
    runtime: dict
    input: str | None
    missing: list
    standby: bool


def read_live(host: str, with_web: bool = True, log=print) -> Snapshot:
    runtime: dict = {}
    settings: dict = {}
    missing: list = []
    with Telnet(host) as t:
        for q in RUNTIME:
            apply_query(runtime, q, t.query(q))
        standby = runtime.get("power") == "STANDBY"
        zone_on = runtime.get("main_zone") == "ON"
        inp = runtime.get("input")
        if not standby:
            for q in QUERIES:
                if q.exts[0].path[0] == PI and not zone_on:
                    continue
                missing += apply_query(settings, q, t.query(q))
            # the input must not change while per-input values are read
            end: dict = {}
            apply_query(end, RUNTIME[2], t.query(RUNTIME[2]))
            if end.get("input") != inp:
                raise RuntimeError("input changed during export (%s -> %s); try again"
                                   % (inp, end.get("input")))
    if PI in settings:
        settings[PI] = {inp: settings[PI]} if inp else {}
    if with_web and not standby:
        try:
            web = parse_web(http_appcommand(host, WEB_CMDS))
            if web:
                settings["web"] = web
            else:
                missing.append(("web",))
        except Exception as e:  # noqa: BLE001 - web stack is optional
            log("WARN: HTTP AppCommand failed: %s" % e, file=sys.stderr)
            missing.append(("web",))
    return Snapshot(settings, runtime, inp, missing, standby)


# --------------------------------------------------------------------------
# Minimal YAML (nested mappings of strings; output also loads with PyYAML)
# --------------------------------------------------------------------------

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")

HEADER = """\
# Denon AVR-X6700H settings, exported by denonctl.py (see docs/denon.md).
# Read-only snapshot of what the receiver reports over telnet and HTTP.
# per_input holds the values Denon keeps per input source; an input is only
# captured while it is selected (export never switches inputs).
# Not captured: Audyssey room-correction filters, anything not listed here.
"""


def _key(k: str) -> str:
    return k if _PLAIN_KEY.match(k) and k not in ("true", "false", "null", "yes", "no", "on", "off") else json.dumps(k)


def dump_yaml(d: dict, indent: int = 0) -> str:
    out = []
    for k in sorted(d, key=_sort_key):
        v = d[k]
        pad = "  " * indent
        if isinstance(v, dict):
            out.append("%s%s:" % (pad, _key(k)))
            out.append(dump_yaml(v, indent + 1) if v else pad + "  {}")
        else:
            out.append("%s%s: %s" % (pad, _key(k), json.dumps(str(v))))
    return "\n".join(x for x in out if x != "")


TOP_ORDER = ["schema", "device", "sources", "web", "speakers", "hdmi",
             "video", "system", "zone2", "zone3", "per_input"]


def _sort_key(k):
    return (TOP_ORDER.index(k), "") if k in TOP_ORDER else (len(TOP_ORDER), k)


def load_yaml(text: str) -> dict:
    root: dict = {}
    stack = [(-1, root)]
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        body = line.strip()
        m = re.match(r'^("(?:[^"\\]|\\.)*"|[^:]+):(?:\s+(.*))?$', body)
        if not m:
            raise ValueError("line %d: cannot parse %r" % (n, line))
        key = json.loads(m.group(1)) if m.group(1).startswith('"') else m.group(1)
        val = m.group(2)
        while stack and stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]
        if val is None:
            parent[key] = {}
            stack.append((ind, parent[key]))
        elif val == "{}":
            parent[key] = {}
        elif val.startswith('"'):
            parent[key] = json.loads(val)
        else:
            parent[key] = val
    return root


def read_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return load_yaml(f.read())


def write_file(path: str, d: dict):
    text = HEADER + dump_yaml(d) + "\n"
    if path == "-":
        sys.stdout.write(text)
        return
    with open(path, "w") as f:
        f.write(text)


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


def flatten(d: dict, prefix: tuple = ()) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, prefix + (k,)))
        else:
            out[prefix + (k,)] = v
    return out


def ext_for(path: tuple):
    """The extractor (and multi key) that produced a flattened path."""
    p = path
    if p and p[0] == PI:
        p = (PI,) + p[2:]
    for q in QUERIES:
        for e in q.exts:
            if e.multi and len(p) == len(e.path) + 1 and p[:-1] == e.path:
                return e, p[-1]
            if not e.multi and p == e.path:
                return e, None
    return None, None


@dataclass
class Diff:
    changed: list   # (path, desired, live)
    added: list     # (path, live): live but not in the file
    unread: list    # (path, desired): in the file but not read live
    info: list      # free text


def compare(desired: dict, live: Snapshot) -> Diff:
    """Compare a live snapshot with the file. Only the selected input's
    per_input entry is compared; device info, sound_mode and runtime state
    are ignored."""
    want = {k: v for k, v in desired.items() if k not in ("schema", "device", PI)}
    have = {k: v for k, v in live.settings.items() if k not in ("device", PI)}
    info = []
    if live.input and live.settings.get(PI, {}).get(live.input) is not None:
        if live.input in desired.get(PI, {}):
            want[PI] = {live.input: desired[PI][live.input]}
            have[PI] = {live.input: live.settings[PI][live.input]}
        else:
            info.append("input %s is not in the snapshot yet (run export to add it)" % live.input)
    elif live.settings and not live.standby:
        info.append("main zone is off: per-input settings not compared")
    fw, fh = flatten(want), flatten(have)
    d = Diff([], [], [], info)
    for p in sorted(set(fw) | set(fh)):
        e, _ = ext_for(p)
        if e is not None and not e.drift:
            continue
        if p in fw and p in fh:
            if fw[p] != fh[p]:
                d.changed.append((p, fw[p], fh[p]))
        elif p in fh:
            d.added.append((p, fh[p]))
        else:
            d.unread.append((p, fw[p]))
    return d


def merge_export(old: dict, live: Snapshot) -> tuple:
    """New file contents: the live settings, plus per_input entries for
    inputs not selected now, plus values that could not be read this time
    (kept from the old file, reported as warnings)."""
    new = {"schema": str(SCHEMA)}
    new.update(json.loads(json.dumps(live.settings)))
    kept = []
    fo, fn = flatten({k: v for k, v in old.items() if k != PI}), flatten(new)
    for p, v in fo.items():
        if p not in fn and p[0] != "schema":
            _set_path(new, p, v)
            kept.append(p)
    pi = dict(old.get(PI, {}))
    pi.update(new.get(PI, {}))
    if pi:
        new[PI] = pi
    return new, kept


# --------------------------------------------------------------------------
# Plan / apply
# --------------------------------------------------------------------------


@dataclass
class Step:
    path: tuple
    desired: str
    live: str
    commands: list
    protected: bool
    unverified: bool
    skip: str | None = None


def build_commands(path: tuple, desired: str) -> tuple:
    e, key = ext_for(path)
    if e is None or not e.apply:
        return None, None
    raw = e.codec[1](desired) if key != "_" else desired
    if e.multi:
        if key == "_":
            cmds = [e.prefix + " " + v for v in raw.split(",")]
        else:
            cmds = [e.prefix + key + " " + v for v in raw.split(",")]
    else:
        cmds = [e.prefix + raw]
    for c in cmds:
        if c.startswith(FORBIDDEN_PREFIXES) or c.endswith("?") or "\r" in c or "\n" in c:
            raise RuntimeError("refusing to build command %r" % c)
    return e, cmds


def plan(desired: dict, live: Snapshot) -> list:
    steps = []
    d = compare(desired, live)
    for p, want, have in d.changed:
        e, cmds = build_commands(p, want)
        if e is None:
            steps.append(Step(p, want, have, [], False, False,
                              skip="no set command (read over HTTP only, or never applied)"))
            continue
        steps.append(Step(p, want, have, cmds, e.protected, e.unverified))
    for inp in sorted(desired.get(PI, {})):
        if inp != live.input:
            steps.append(Step((PI, inp), "", "", [], False, False,
                              skip="input %s is not selected; apply never switches inputs" % inp))
    return steps


def fmt_path(p: tuple) -> str:
    return ".".join(p)


def print_plan(steps: list, allow_setup: bool) -> int:
    n = 0
    for s in steps:
        if s.skip:
            print("SKIP     %s: %s" % (fmt_path(s.path), s.skip))
            continue
        tag = []
        if s.protected:
            tag.append("protected" if not allow_setup else "protected, allowed")
        if s.unverified:
            tag.append("unverified set form")
        print("CHANGE   %s: %r -> %r%s" % (fmt_path(s.path), s.live, s.desired,
                                          " [%s]" % ", ".join(tag) if tag else ""))
        for c in s.commands:
            print("    send %r" % c)
        n += 1
    if not n:
        print("No differences for the selected input and global settings.")
    return n


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _report(d: Diff) -> int:
    for line in d.info:
        print("INFO     " + line)
    for p, want, have in d.changed:
        print("DRIFT    %s: file %r, live %r" % (fmt_path(p), want, have))
    for p, have in d.added:
        print("NEW      %s: live %r (not in the file)" % (fmt_path(p), have))
    for p, want in d.unread:
        print("UNREAD   %s: file %r, not reported live" % (fmt_path(p), want))
    return len(d.changed) + len(d.added)


def cmd_export(a) -> int:
    live = read_live(a.host, with_web=not a.no_web)
    if live.standby:
        print("Receiver is in standby; settings are not reported. Nothing written.", file=sys.stderr)
        return 1
    if a.runtime:
        print("# runtime state (not written to the file)", file=sys.stderr)
        print(dump_yaml(live.runtime), file=sys.stderr)
    old = read_file(a.file) if a.output != "-" or os.path.exists(a.file) else {}
    new, kept = merge_export(old, live)
    for p in kept:
        print("WARN: %s not reported live, kept the previous value" % fmt_path(p), file=sys.stderr)
    write_file(a.output or a.file, new)
    print("Exported input %s (%d values)." % (live.input, len(flatten(new))), file=sys.stderr)
    return 0


def cmd_drift(a) -> int:
    desired = read_file(a.file)
    if not desired:
        print("ERROR: %s is missing or empty" % a.file)
        return 1
    live = read_live(a.host, with_web=not a.no_web)
    if live.standby:
        print("SKIPPED  receiver is in standby (settings are not reported). No drift check.")
        return 0
    print("Live: power %s, main zone %s, input %s" % (
        live.runtime.get("power"), live.runtime.get("main_zone"), live.input))
    n = _report(compare(desired, live))
    if n:
        print("\n%d difference(s) between the receiver and %s." % (n, os.path.basename(a.file)))
        print("If the change was intended, run `denonctl.py export` and commit the file.")
        return 2
    print("No drift.")
    return 0


def cmd_plan(a, send: bool = False) -> int:
    desired = read_file(a.file)
    if not desired:
        print("ERROR: %s is missing or empty" % a.file)
        return 1
    live = read_live(a.host, with_web=False)
    if live.standby:
        print("Receiver is in standby; nothing to plan.")
        return 1
    print("Live input: %s (%s)" % (live.input, "send requested" if send else "dry run"))
    steps = plan(desired, live)
    n = print_plan(steps, a.allow_setup)
    if not send:
        if n:
            print("\nDry run: nothing was sent. `apply --send` sends the commands above.")
        return 2 if (n and getattr(a, "detailed_exitcode", False)) else 0
    if os.environ.get("CI"):
        print("REFUSED: apply --send never runs in CI.")
        return 3
    if os.environ.get("DENON_APPLY_ENABLE") != "1":
        print("REFUSED: sending is disabled. Review the plan above, then rerun with "
              "DENON_APPLY_ENABLE=1 and --send.")
        return 3
    todo = [s for s in steps if not s.skip]
    blocked = [s for s in todo if s.protected and not a.allow_setup]
    if blocked:
        print("REFUSED: %d protected change(s) need --allow-setup." % len(blocked))
        return 3
    if not todo:
        return 0
    with Telnet(a.host) as t:
        now: dict = {}
        apply_query(now, RUNTIME[2], t.query(RUNTIME[2]))
        if now.get("input") != live.input:
            print("REFUSED: input changed to %s since the plan; nothing sent." % now.get("input"))
            return 3
        for s in todo:
            for c in s.commands:
                print("sent %r -> %s" % (c, " | ".join(t.send(c))))
                time.sleep(0.3)
    after = compare(desired, read_live(a.host, with_web=False))
    rem = [c for c in after.changed if ext_for(c[0])[0] and ext_for(c[0])[0].apply]
    if rem:
        print("\nStill different after apply:")
        _report(Diff(rem, [], [], []))
        return 1
    print("Applied and verified.")
    return 0


def cmd_check(a) -> int:
    """Offline validation of the desired file (used by CI)."""
    desired = read_file(a.file)
    errors = []
    if desired.get("schema") != str(SCHEMA):
        errors.append("schema is %r, want %r" % (desired.get("schema"), str(SCHEMA)))
    flat = flatten(desired)
    for p, v in flat.items():
        if p[0] in ("schema", "web"):
            continue
        e, key = ext_for(p)
        if e is None:
            errors.append("%s: not a known setting" % fmt_path(p))
            continue
        if e.apply:
            try:
                build_commands(p, v)
            except (ValueError, RuntimeError) as ex:
                errors.append("%s: %s" % (fmt_path(p), ex))
    for line in errors:
        print("ERROR    " + line)
    if not errors:
        print("%s: %d values, %d input(s) captured (%s)." % (
            os.path.basename(a.file), len(flat), len(desired.get(PI, {})),
            ", ".join(sorted(desired.get(PI, {}))) or "none"))
    return 1 if errors or not flat else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("DENON_HOST", DEFAULT_HOST))
    ap.add_argument("--file", default=DEFAULT_DESIRED, help="desired-state YAML")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("-o", "--output", help="write here instead of --file ('-' = stdout)")
    e.add_argument("--runtime", action="store_true", help="also print runtime state to stderr")
    e.add_argument("--no-web", action="store_true", help="skip the HTTP AppCommand interface")
    sub.add_parser("check")
    d = sub.add_parser("drift")
    d.add_argument("--no-web", action="store_true")
    p = sub.add_parser("plan")
    p.add_argument("--detailed-exitcode", action="store_true")
    p.add_argument("--allow-setup", action="store_true")
    x = sub.add_parser("apply")
    x.add_argument("--send", action="store_true", help="actually send (default: dry run)")
    x.add_argument("--allow-setup", action="store_true",
                   help="allow speaker/HDMI setup values (overwrites calibration results)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            return cmd_export(a)
        if a.cmd == "check":
            return cmd_check(a)
        if a.cmd == "drift":
            return cmd_drift(a)
        if a.cmd == "plan":
            return cmd_plan(a)
        return cmd_plan(a, send=a.send)
    except (OSError, RuntimeError, ValueError) as ex:
        print("ERROR: %s" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
