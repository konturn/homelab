#!/usr/bin/env python3
"""Desired-state management for NETGEAR smart switches driven through their web UI API.

Subcommands
  check            offline: schema-check desired-state files and their snapshots
  export           write the sanitized running configuration of the live switch
  plan             changes needed to reach the desired state (live, or --offline vs the snapshot)
  apply            write only the differences, then re-read and verify
  drift            compare the live switch with the desired state and the committed snapshot
  generate         write a desired-state file from the live switch
  render-snapshot  update the managed lines of a snapshot from the desired state (offline)
  firmware status  running / stored image versions against the desired firmware
  firmware upgrade upload the desired firmware, reboot into it, verify, check drift

Managed settings: VLANs (id, name), per-port VLAN membership (untagged/tagged),
port PVID and per-port PoE (enabled, priority, power limit). Everything else in
the running configuration is compared against the committed snapshot only.

Credentials: NETGEAR_HOST / NETGEAR_USERNAME / NETGEAR_PASSWORD from the
environment, otherwise the desired-state file's vault_path is read from Vault
(VAULT_ADDR plus VAULT_TOKEN, or VAULT_ID_TOKEN for a JWT login with
VAULT_JWT_ROLE, default "ci-deploy").
"""
from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

try:
    import yaml  # PyYAML (installed with ansible, see requirements.txt)
except ImportError:  # pragma: no cover
    yaml = None

HERE = Path(__file__).resolve().parent

PRIORITY_TO_UI = {"high": "1", "medium": "2", "low": "3"}
UI_TO_PRIORITY = {v: k for k, v in PRIORITY_TO_UI.items()}
MODES = ("untagged", "tagged")
TAGGING_TO_UI = {"untagged": "1", "tagged": "2"}
UI_TO_TAGGING = {v: k for k, v in TAGGING_TO_UI.items()}
# The exported configuration names the three PoE priorities differently from the UI.
PRIORITY_TO_CLI = {"high": "critical", "medium": "high", "low": "low"}
CLI_TO_PRIORITY = {v: k for k, v in PRIORITY_TO_CLI.items()}

# running-config interface prefixes -> short names used by the web UI
IFACE_PREFIXES = {
    "fastethernet": "fa",
    "gigabitethernet": "gi",
    "twopointfivegigabitethernet": "tw",
    "fivegigabitethernet": "hx",
    "tengigabitethernet": "te",
}

# Op order used by plan/apply: add before remove so a port is never left
# without its VLANs half way through a change.
OP_ORDER = ["vlan_add", "vlan_rename", "member_set", "pvid", "member_remove", "poe", "vlan_remove"]


class NgError(Exception):
    """Fatal error with a message that is safe to print."""


class GuardError(NgError):
    """A change was refused by the management lockout guard."""


# =============================================================================
# Small helpers
# =============================================================================

def parse_ranges(text: str) -> List[int]:
    out: List[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))


def format_ranges(values: Iterable[int]) -> str:
    vals = sorted(set(values))
    parts: List[str] = []
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[j + 1] == vals[j] + 1:
            j += 1
        parts.append(str(vals[i]) if i == j else f"{vals[i]}-{vals[j]}")
        i = j + 1
    return ",".join(parts)


def port_sort_key(name: str):
    m = re.match(r"([a-zA-Z]+)(\d+)$", name)
    return (int(m.group(2)), name) if m else (10**6, name)


def short_iface(name: str) -> Optional[str]:
    m = re.match(r"([A-Za-z]+)(\d+)$", name)
    if not m:
        return None
    pfx = IFACE_PREFIXES.get(m.group(1).lower())
    return f"{pfx}{m.group(2)}" if pfx else None


# =============================================================================
# Sanitizing the exported running configuration
# =============================================================================

_DROP_LINE_RES = [
    re.compile(r"^\s*username\s+\S+.*\bpassword\b", re.I),
    re.compile(r"^\s*snmp-server\s+engineid\b", re.I),
    re.compile(r"^\s*snmp-server\s+community\b", re.I),
    re.compile(r"^\s*snmp-server\s+user\b.*\b(auth|priv)\b", re.I),
    re.compile(r"^\s*(radius|tacacs)-server\b.*\bkey\b", re.I),
    re.compile(r"\b(password|passphrase|secret|key-string)\b", re.I),
]
_CERT_BLOCK_START = re.compile(r"^\s*(encrypted\s+)?crypto\s+(certificate|key)\b.*\bimport\b", re.I)
_LEFTOVER_RES = [
    re.compile(r"-----(BEGIN|END)\b"),
    re.compile(r"PRIVATE KEY", re.I),
    re.compile(r"\bengineid\b", re.I),
    re.compile(r"\bpassword\b", re.I),
    re.compile(r"\bcrypto\s+(certificate|key)\b", re.I),
]


def sanitize(raw: str) -> str:
    """Strip the device header, credentials, key/cert blocks and the SNMP engine ID."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # The download starts with a binary-ish header line before "config-file-header".
    for i, line in enumerate(lines[:5]):
        if line.strip() == "config-file-header":
            lines = lines[i:]
            break
    else:
        if lines and (lines[0].startswith("0x") or any(ord(c) < 9 for c in lines[0])):
            lines = lines[1:]
    out: List[str] = []
    in_block = False
    in_pem = False
    for line in lines:
        s = line.rstrip()
        if in_block:
            if s.strip() == ".":
                in_block = False
            continue
        if _CERT_BLOCK_START.search(s):
            in_block = True
            continue
        if in_pem:
            if s.lstrip().startswith("-----END"):
                in_pem = False
            continue
        if s.lstrip().startswith("-----BEGIN"):
            in_pem = True
            continue
        if any(r.search(s) for r in _DROP_LINE_RES):
            continue
        if any(ord(c) < 9 for c in s):
            continue
        out.append(s)
    while out and out[-1] == "":
        out.pop()
    text = "\n".join(out) + "\n"
    assert_sanitized(text)
    return text


def assert_sanitized(text: str) -> None:
    for r in _LEFTOVER_RES:
        m = r.search(text)
        if m:
            line_no = text[: m.start()].count("\n") + 1
            raise NgError(f"sanitized export still contains sensitive-looking content at line {line_no}; refusing to continue")


# =============================================================================
# Parsing a (sanitized) running configuration into the managed model
# =============================================================================

def _empty_port() -> dict:
    return {"pvid": 1, "untagged": set(), "tagged": set(), "poe": None, "_poe_lines": []}


def parse_running_config(text: str, poe_defaults: Optional[dict] = None) -> Tuple[dict, List[Tuple[str, str]]]:
    """Return (state, unmanaged) where unmanaged is a list of (context, line).

    state = {"vlans": {id: name}, "ports": {short: {"pvid", "untagged", "tagged", "poe"}}}
    PoE is reported only when poe_defaults is given (the export lists
    non-default PoE settings only).
    """
    vlans: Dict[int, str] = {1: ""}
    ports: Dict[str, dict] = {}
    unmanaged: List[Tuple[str, str]] = []
    ctx = ""          # "" global, "vlan-db", "vlan:N", "port:xx", "if:<name>"
    for raw in text.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if s == "!":
            ctx = ""
            continue
        if s == "exit":
            ctx = ""
            continue
        if s == "vlan database":
            ctx = "vlan-db"
            continue
        m = re.match(r"^interface\s+vlan\s+(\d+)$", s, re.I)
        if m:
            ctx = f"vlan:{int(m.group(1))}"
            vlans.setdefault(int(m.group(1)), "")
            continue
        m = re.match(r"^interface\s+(\S+)$", s, re.I)
        if m and not line.startswith(" "):
            short = short_iface(m.group(1))
            if short:
                ctx = f"port:{short}"
                ports.setdefault(short, _empty_port())
            else:
                ctx = f"if:{m.group(1)}"
            continue
        if ctx == "vlan-db":
            m = re.match(r"^vlan\s+([\d,\-]+)$", s)
            if m:
                for v in parse_ranges(m.group(1)):
                    vlans.setdefault(v, "")
                continue
            unmanaged.append((ctx, s))
            continue
        if ctx.startswith("vlan:"):
            m = re.match(r"^name\s+(.*)$", s)
            if m:
                vlans[int(ctx[5:])] = m.group(1).strip()
                continue
            unmanaged.append((ctx, s))
            continue
        if ctx.startswith("port:"):
            p = ports[ctx[5:]]
            m = re.match(r"^switchport\s+general\s+allowed\s+vlan\s+add\s+([\d,\-]+)\s+(untagged|tagged)$", s)
            if m:
                for v in parse_ranges(m.group(1)):
                    p["untagged"].discard(v)
                    p["tagged"].discard(v)
                    p[m.group(2)].add(v)
                continue
            m = re.match(r"^switchport\s+general\s+allowed\s+vlan\s+remove\s+([\d,\-]+)$", s)
            if m:
                for v in parse_ranges(m.group(1)):
                    p["untagged"].discard(v)
                    p["tagged"].discard(v)
                continue
            m = re.match(r"^switchport\s+general\s+pvid\s+(\d+)$", s)
            if m:
                p["pvid"] = int(m.group(1))
                continue
            if re.match(r"^switchport\s+(mode|access|trunk)\b", s):
                raise NgError(f"{ctx[5:]}: unsupported switchport configuration '{s}' (only general mode is managed)")
            if re.match(r"^power\s+inline\b", s):
                p["_poe_lines"].append(s)
                continue
            unmanaged.append((ctx, s))
            continue
        unmanaged.append((ctx, s))
    for name, p in ports.items():
        lines = p.pop("_poe_lines")
        if poe_defaults is not None:
            p["poe"] = _poe_from_lines(name, lines, poe_defaults.get(name))
        elif lines:
            p["poe_lines"] = lines
    state = {"vlans": vlans, "ports": ports}
    return normalize_state(state), unmanaged


def _poe_from_lines(port: str, lines: List[str], default: Optional[dict]) -> Optional[dict]:
    if default is None:
        if lines:
            raise NgError(f"{port}: PoE settings in the export for a port without PoE defaults")
        return None
    poe = dict(default)
    for s in lines:
        m = re.match(r"^power\s+inline\s+(never|auto)$", s)
        if m:
            poe["enabled"] = m.group(1) == "auto"
            continue
        m = re.match(r"^power\s+inline\s+priority\s+(low|high|critical)$", s)
        if m:
            poe["priority"] = CLI_TO_PRIORITY.get(m.group(1), m.group(1))
            continue
        m = re.match(r"^power\s+inline\s+limit\s+(\d+)$", s)
        if m:
            poe["limit_mw"] = int(m.group(1))
            continue
        raise NgError(f"{port}: unrecognised PoE line '{s}'")
    return poe


def normalize_state(state: dict) -> dict:
    ports = {}
    for name in sorted(state["ports"], key=port_sort_key):
        p = state["ports"][name]
        np_ = {"pvid": int(p["pvid"]),
               "untagged": sorted(set(int(v) for v in p.get("untagged", []))),
               "tagged": sorted(set(int(v) for v in p.get("tagged", []))),
               "poe": None}
        if p.get("poe") is not None:
            poe = p["poe"]
            np_["poe"] = {"enabled": bool(poe["enabled"]), "priority": str(poe["priority"]),
                          "limit_mw": None if poe.get("limit_mw") is None else int(poe["limit_mw"])}
        if p.get("poe_lines"):
            np_["poe_lines"] = list(p["poe_lines"])
        ports[name] = np_
    vlans = {int(k): ("" if v is None else str(v)) for k, v in sorted(state["vlans"].items())}
    return {"vlans": vlans, "ports": ports}


# =============================================================================
# Desired-state files
# =============================================================================

def _load_yaml(path: Path):
    if yaml is None:
        raise NgError("PyYAML is required to read desired-state files")
    with open(path) as fh:
        return yaml.safe_load(fh)


def validate_desired(data, origin: str = "desired state") -> dict:
    """Schema-check a desired-state document; return the normalized form."""
    errs: List[str] = []

    def err(msg):
        errs.append(f"{origin}: {msg}")

    if not isinstance(data, dict):
        raise NgError(f"{origin}: top level must be a mapping")
    allowed_top = {"name", "model", "vault_path", "management", "vlans", "ports", "poe_defaults_offline", "firmware"}
    for k in data:
        if k not in allowed_top:
            err(f"unknown key '{k}'")
    for k in ("name", "vault_path"):
        if not isinstance(data.get(k), str) or not data.get(k):
            err(f"'{k}' must be a non-empty string")
    vl = data.get("vlans")
    vlans: Dict[int, str] = {}
    if not isinstance(vl, dict) or not vl:
        err("'vlans' must be a non-empty mapping of VLAN id -> name")
    else:
        for k, v in vl.items():
            try:
                vid = int(k)
            except (TypeError, ValueError):
                err(f"VLAN id '{k}' is not a number")
                continue
            if not 1 <= vid <= 4093:
                err(f"VLAN id {vid} out of range 1-4093")
            if v is None:
                v = ""
            if not isinstance(v, str) or len(v) > 32:
                err(f"VLAN {vid}: name must be a string of at most 32 characters")
            vlans[vid] = v or ""
        if 1 not in vlans:
            err("VLAN 1 (default VLAN) must be listed")
    mg = data.get("management")
    mvlan = None
    protected: List[str] = []
    uplink = None
    if not isinstance(mg, dict):
        err("'management' must be a mapping with vlan, uplink and protected_ports")
    else:
        for k in mg:
            if k not in ("vlan", "uplink", "protected_ports"):
                err(f"management: unknown key '{k}'")
        mvlan = mg.get("vlan")
        if not isinstance(mvlan, int) or mvlan not in vlans:
            err("management.vlan must be one of the listed VLANs")
        uplink = mg.get("uplink")
        protected = mg.get("protected_ports") or []
        if not isinstance(protected, list) or not all(isinstance(p, str) for p in protected) or not protected:
            err("management.protected_ports must be a non-empty list of port names")
            protected = []
        if not isinstance(uplink, str) or uplink not in protected:
            err("management.uplink must be a port listed in management.protected_ports")
    pt = data.get("ports")
    ports: Dict[str, dict] = {}
    if not isinstance(pt, dict) or not pt:
        err("'ports' must be a non-empty mapping")
    else:
        for name, p in pt.items():
            name = str(name)
            if not re.match(r"^[a-z]{2}\d+$", name):
                err(f"port '{name}': name must look like gi1 / tw5 / hx7 / te10")
                continue
            if not isinstance(p, dict):
                err(f"port {name}: must be a mapping")
                continue
            for k in p:
                if k not in ("pvid", "untagged", "tagged", "poe"):
                    err(f"port {name}: unknown key '{k}'")
            pvid = p.get("pvid")
            unt = p.get("untagged") or []
            tag = p.get("tagged") or []
            if not isinstance(pvid, int) or pvid not in vlans:
                err(f"port {name}: pvid must be one of the listed VLANs")
            for lbl, lst in (("untagged", unt), ("tagged", tag)):
                if not isinstance(lst, list) or not all(isinstance(v, int) for v in lst):
                    err(f"port {name}: {lbl} must be a list of VLAN ids")
                    continue
                for v in lst:
                    if v not in vlans:
                        err(f"port {name}: {lbl} VLAN {v} is not listed under vlans")
            if isinstance(unt, list) and isinstance(tag, list):
                both = set(unt) & set(tag)
                if both:
                    err(f"port {name}: VLAN(s) {format_ranges(both)} both untagged and tagged")
            poe = p.get("poe")
            npoe = None
            if poe is not None:
                if not isinstance(poe, dict):
                    err(f"port {name}: poe must be a mapping")
                else:
                    for k in poe:
                        if k not in ("enabled", "priority", "limit_mw"):
                            err(f"port {name}: poe: unknown key '{k}'")
                    if not isinstance(poe.get("enabled"), bool):
                        err(f"port {name}: poe.enabled must be true/false")
                    if poe.get("priority") not in PRIORITY_TO_UI:
                        err(f"port {name}: poe.priority must be one of {sorted(PRIORITY_TO_UI)}")
                    lim = poe.get("limit_mw")
                    if lim is not None and (not isinstance(lim, int) or not 0 <= lim <= 99000):
                        err(f"port {name}: poe.limit_mw must be an integer in mW")
                    npoe = {"enabled": poe.get("enabled"), "priority": poe.get("priority"), "limit_mw": lim}
            ports[name] = {"pvid": pvid, "untagged": unt if isinstance(unt, list) else [],
                           "tagged": tag if isinstance(tag, list) else [], "poe": npoe}
        for p in protected:
            if p not in ports:
                err(f"management.protected_ports: '{p}' is not a listed port")
        if isinstance(mvlan, int):
            for p in protected:
                if p in ports and mvlan not in ports[p]["untagged"] + ports[p]["tagged"]:
                    err(f"protected port {p} does not carry management VLAN {mvlan}")
    pdo = data.get("poe_defaults_offline")
    if pdo is not None:
        if not isinstance(pdo, dict) or set(pdo) - {"enabled", "priority", "limit_mw"}:
            err("poe_defaults_offline must be a mapping with enabled, priority, limit_mw")
    firmware = validate_firmware(data.get("firmware"), err)
    if errs:
        raise NgError("\n".join(errs))
    state = normalize_state({"vlans": vlans, "ports": ports})
    return {
        "name": data["name"],
        "model": data.get("model", ""),
        "vault_path": data["vault_path"],
        "management": {"vlan": mvlan, "uplink": uplink, "protected_ports": sorted(set(protected), key=port_sort_key)},
        "poe_defaults_offline": pdo,
        "firmware": firmware,
        "state": state,
    }


_FW_VERSION_RE = re.compile(r"^\d+(\.\d+){3}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_firmware(fw, err) -> Optional[dict]:
    """Schema-check the optional firmware block; report problems through err()."""
    if fw is None:
        return None
    if not isinstance(fw, dict):
        err("firmware must be a mapping with version, url, sha256, image, image_sha256")
        return None
    keys = ("version", "url", "sha256", "image", "image_sha256")
    for k in fw:
        if k not in keys:
            err(f"firmware: unknown key '{k}'")
    ok = True
    if not isinstance(fw.get("version"), str) or not _FW_VERSION_RE.match(fw["version"]):
        err("firmware.version must be a quoted version string like \"6.7.0.52\"")
        ok = False
    if not isinstance(fw.get("url"), str) or not fw["url"].startswith("https://") or not fw["url"].endswith(".zip"):
        err("firmware.url must be an https:// URL of the vendor's .zip")
        ok = False
    for k in ("sha256", "image_sha256"):
        if not isinstance(fw.get(k), str) or not _SHA256_RE.match(fw[k]):
            err(f"firmware.{k} must be a lowercase hex SHA-256")
            ok = False
    img = fw.get("image")
    if not isinstance(img, str) or "/" in img or not img.endswith(".bin"):
        err("firmware.image must be the .bin file name inside the zip")
        ok = False
    elif isinstance(fw.get("version"), str) and fw["version"] not in img:
        err("firmware.image name does not contain firmware.version")
        ok = False
    return {k: fw[k] for k in keys} if ok else None


def load_desired(path: Path) -> dict:
    d = validate_desired(_load_yaml(path), origin=str(path.name))
    d["path"] = path
    return d


def snapshot_path(desired_path: Path) -> Path:
    return desired_path.with_name(desired_path.stem + ".running-config.txt")


def offline_poe_defaults(desired: dict) -> Optional[Dict[str, dict]]:
    """PoE defaults per port for reading PoE from an export (non-defaults only are listed)."""
    pdo = desired.get("poe_defaults_offline")
    if not pdo:
        return None
    return {name: dict(pdo) for name, p in desired["state"]["ports"].items() if p["poe"] is not None}


def render_desired_yaml(name: str, model: str, vault_path: str, mgmt_vlan: int, uplink: str,
                        protected: List[str], state: dict, poe_defaults: Optional[dict]) -> str:
    out = [
        f"# Desired state for {name} ({model}).",
        "# Applied by CI from main with networking/switches/netgear/ngswitch.py; see README.md.",
        f"name: {name}",
        f"model: {model}",
        f"vault_path: {vault_path}",
        "management:",
        f"  vlan: {mgmt_vlan}",
        f"  uplink: {uplink}",
        "  # Ports carrying the management VLAN; the tool never changes their",
        "  # management VLAN membership or PVID.",
        "  protected_ports: [" + ", ".join(protected) + "]",
    ]
    if poe_defaults:
        out.append("# PoE values the switch omits from its exported configuration.")
        out.append("poe_defaults_offline: {enabled: %s, priority: %s, limit_mw: %s}" % (
            "true" if poe_defaults["enabled"] else "false", poe_defaults["priority"], poe_defaults["limit_mw"]))
    out.append("vlans:")
    for vid, vname in state["vlans"].items():
        out.append(f"  {vid}: {json.dumps(vname)}")
    out.append("ports:")
    for pname, p in state["ports"].items():
        parts = [f"pvid: {p['pvid']}",
                 "untagged: [" + ", ".join(str(v) for v in p["untagged"]) + "]",
                 "tagged: [" + ", ".join(str(v) for v in p["tagged"]) + "]"]
        if p["poe"] is not None:
            poe = p["poe"]
            lim = "" if poe["limit_mw"] is None else f", limit_mw: {poe['limit_mw']}"
            parts.append("poe: {enabled: %s, priority: %s%s}" % ("true" if poe["enabled"] else "false", poe["priority"], lim))
        out.append(f"  {pname}: {{" + ", ".join(parts) + "}")
    return "\n".join(out) + "\n"


# =============================================================================
# Planning
# =============================================================================

def _members(p: dict) -> Dict[int, str]:
    m = {v: "untagged" for v in p["untagged"]}
    m.update({v: "tagged" for v in p["tagged"]})
    return m


def describe(c: dict) -> str:
    op = c["op"]
    if op == "vlan_add":
        return f"VLAN {c['vlan']}: create (name {c['name']!r})"
    if op == "vlan_rename":
        return f"VLAN {c['vlan']}: rename {c['old']!r} -> {c['name']!r}"
    if op == "vlan_remove":
        return f"VLAN {c['vlan']}: delete"
    if op == "member_set":
        old = c.get("old") or "not a member"
        return f"{c['port']}: VLAN {c['vlan']} {old} -> {c['mode']}"
    if op == "member_remove":
        return f"{c['port']}: VLAN {c['vlan']} {c['old']} -> not a member"
    if op == "pvid":
        return f"{c['port']}: PVID {c['old']} -> {c['pvid']}"
    if op == "poe":
        o, n = c["old"], c["new"]
        diffs = [f"{k} {o.get(k)} -> {n.get(k)}" for k in ("enabled", "priority", "limit_mw") if o.get(k) != n.get(k)]
        return f"{c['port']}: PoE " + ", ".join(diffs)
    return json.dumps(c)


def plan(current: dict, desired_state: dict) -> Tuple[List[dict], List[str]]:
    """Return (changes, notes). Raises NgError when the two cannot be compared."""
    changes: List[dict] = []
    notes: List[str] = []
    cv, dv = current["vlans"], desired_state["vlans"]
    for vid, name in dv.items():
        if vid not in cv:
            changes.append({"op": "vlan_add", "vlan": vid, "name": name})
        elif vid != 1 and cv[vid] != name:
            changes.append({"op": "vlan_rename", "vlan": vid, "name": name, "old": cv[vid]})
    for vid in cv:
        if vid not in dv and vid != 1:
            changes.append({"op": "vlan_remove", "vlan": vid})
    cp, dp = current["ports"], desired_state["ports"]
    unknown = sorted(set(dp) - set(cp), key=port_sort_key)
    if unknown:
        raise NgError("ports in the desired state that the switch does not have: " + ", ".join(unknown))
    missing = sorted(set(cp) - set(dp), key=port_sort_key)
    if missing:
        raise NgError("switch ports missing from the desired state: " + ", ".join(missing))
    for name in sorted(dp, key=port_sort_key):
        d, c = dp[name], cp[name]
        dm, cm = _members(d), _members(c)
        for vid in sorted(dm):
            if cm.get(vid) != dm[vid]:
                changes.append({"op": "member_set", "port": name, "vlan": vid, "mode": dm[vid], "old": cm.get(vid)})
        for vid in sorted(cm):
            if vid not in dm:
                changes.append({"op": "member_remove", "port": name, "vlan": vid, "old": cm[vid]})
        if d["pvid"] != c["pvid"]:
            changes.append({"op": "pvid", "port": name, "pvid": d["pvid"], "old": c["pvid"]})
        if d["poe"] is not None:
            if c["poe"] is None:
                notes.append(f"{name}: PoE not compared (not available in this source)")
                continue
            new = dict(d["poe"])
            old = dict(c["poe"])
            if new.get("limit_mw") is None or old.get("limit_mw") is None:
                new["limit_mw"] = old["limit_mw"] = None
            if new != old:
                changes.append({"op": "poe", "port": name, "new": dict(d["poe"]), "old": dict(c["poe"])})
    changes.sort(key=lambda ch: OP_ORDER.index(ch["op"]))
    return changes, notes


def check_guard(changes: List[dict], desired: dict, current: dict, mgmt_ifaces: Optional[Iterable[int]] = None) -> List[str]:
    """Return refusal reasons (empty = allowed). Protects the management path."""
    reasons: List[str] = []
    mg = desired["management"]["vlan"]
    protected = set(desired["management"]["protected_ports"])
    uplink = desired["management"]["uplink"]
    if uplink not in protected:
        reasons.append(f"uplink {uplink} is not a protected port")
    carriers = {name for name, p in current["ports"].items() if mg in p["untagged"] or mg in p["tagged"]}
    unprotected = sorted(carriers - protected, key=port_sort_key)
    if unprotected:
        reasons.append(f"ports currently carrying management VLAN {mg} are not in protected_ports: " + ", ".join(unprotected))
    if mgmt_ifaces is not None:
        mif = set(mgmt_ifaces)
        if mg not in mif:
            reasons.append(f"management VLAN {mg} has no IP interface on the switch (IP interfaces on VLANs: {format_ranges(mif) or 'none'})")
    else:
        mif = {mg}
    for c in changes:
        op = c["op"]
        if op in ("member_set", "member_remove") and c["port"] in protected and c["vlan"] == mg:
            reasons.append(f"refused: {describe(c)} (management VLAN on protected port)")
        elif op == "pvid" and c["port"] in protected:
            reasons.append(f"refused: {describe(c)} (PVID of protected port)")
        elif op == "vlan_remove" and (c["vlan"] == mg or c["vlan"] in mif):
            reasons.append(f"refused: {describe(c)} (management VLAN)")
        elif op == "poe" and c["port"] in protected and not c["new"]["enabled"]:
            reasons.append(f"refused: {describe(c)} (power to protected port)")
    return reasons


# =============================================================================
# Snapshot rendering (offline)
# =============================================================================

_MANAGED_PORT_LINE = re.compile(r"^\s*(switchport\s+general\s+(allowed\s+vlan|pvid)\b|power\s+inline\b)")


def render_snapshot(text: str, desired: dict) -> str:
    """Rewrite the managed lines of a snapshot so that it parses to the desired state."""
    state = desired["state"]
    pdo = desired.get("poe_defaults_offline")
    lines = text.rstrip("\n").split("\n")
    out: List[str] = []
    i = 0
    seen_vlan_ifaces = set()
    first_port_idx = None
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if s == "vlan database":
            out.append(line)
            i += 1
            while i < len(lines) and lines[i].strip() != "exit":
                if not re.match(r"^vlan\s+[\d,\-]+$", lines[i].strip()):
                    out.append(lines[i])
                i += 1
            others = [v for v in state["vlans"] if v != 1]
            if others:
                out.append(f"vlan {format_ranges(others)}")
            continue
        m = re.match(r"^interface\s+vlan\s+(\d+)$", s, re.I)
        if m:
            vid = int(m.group(1))
            seen_vlan_ifaces.add(vid)
            block = [line]
            i += 1
            while i < len(lines) and lines[i].strip() not in ("!", "exit") and not lines[i].startswith("interface"):
                if not re.match(r"^\s*name\s", lines[i]):
                    block.append(lines[i])
                i += 1
            name = state["vlans"].get(vid, "")
            if name:
                block.insert(1, f" name {name}")
            if len(block) > 1 or vid == 1:
                out.extend(block)
                if i < len(lines) and lines[i].strip() == "!":
                    out.append(lines[i])
                    i += 1
            elif i < len(lines) and lines[i].strip() == "!":
                i += 1
            continue
        m = re.match(r"^interface\s+(\S+)$", s, re.I)
        if m and short_iface(m.group(1)) and not line.startswith(" "):
            pname = short_iface(m.group(1))
            if first_port_idx is None:
                first_port_idx = len(out)
            block = [line]
            insert_at = None
            i += 1
            while i < len(lines) and lines[i].strip() not in ("!", "exit") and not lines[i].startswith("interface"):
                if _MANAGED_PORT_LINE.match(lines[i]):
                    if insert_at is None:
                        insert_at = len(block)
                else:
                    block.append(lines[i])
                i += 1
            if insert_at is None:
                insert_at = min(2, len(block))
            p = state["ports"].get(pname)
            managed: List[str] = []
            poe_lines: List[str] = []
            if p is not None:
                # Same layout as the switch: runs of equal tagging, in VLAN order.
                runs: List[Tuple[str, List[int]]] = []
                for vid, mode in sorted(_members(p).items()):
                    if runs and runs[-1][0] == mode:
                        runs[-1][1].append(vid)
                    else:
                        runs.append((mode, [vid]))
                for mode, vids in runs:
                    managed.append(f" switchport general allowed vlan add {format_ranges(vids)} {mode}")
                if p["pvid"] != 1:
                    managed.append(f" switchport general pvid {p['pvid']}")
                # PoE lines go at the end of the block, in the switch's order.
                if p["poe"] is not None and pdo:
                    if p["poe"]["limit_mw"] is not None and p["poe"]["limit_mw"] != pdo["limit_mw"]:
                        poe_lines.append(f" power inline limit {p['poe']['limit_mw']}")
                    if p["poe"]["priority"] != pdo["priority"]:
                        poe_lines.append(f" power inline priority {PRIORITY_TO_CLI[p['poe']['priority']]}")
                    if p["poe"]["enabled"] != pdo["enabled"]:
                        poe_lines.append(" power inline " + ("auto" if p["poe"]["enabled"] else "never"))
            block[insert_at:insert_at] = managed
            block.extend(poe_lines)
            out.extend(block)
            continue
        out.append(line)
        i += 1
    # VLAN names without an "interface vlan" block: add one before the first port.
    missing = [v for v, n in state["vlans"].items() if n and v not in seen_vlan_ifaces]
    if missing:
        add: List[str] = []
        for v in missing:
            add += [f"interface vlan {v}", f" name {state['vlans'][v]}", "!"]
        at = first_port_idx if first_port_idx is not None else len(out)
        out[at:at] = add
    return "\n".join(out) + "\n"


# =============================================================================
# Credentials
# =============================================================================

def _ssl_ctx(skip_env: str, ca_env: str) -> ssl.SSLContext:
    if os.environ.get(skip_env, "").lower() in ("1", "true", "yes"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=os.environ.get(ca_env) or None)


def vault_read(path: str) -> dict:
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        raise NgError("VAULT_ADDR is not set and NETGEAR_* credentials are not in the environment")
    ctx = _ssl_ctx("VAULT_SKIP_VERIFY", "VAULT_CACERT")
    token = os.environ.get("VAULT_TOKEN")
    if not token:
        jwt = os.environ.get("VAULT_ID_TOKEN")
        if not jwt:
            raise NgError("neither VAULT_TOKEN nor VAULT_ID_TOKEN is set")
        body = json.dumps({"role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": jwt}).encode()
        req = urllib.request.Request(f"{addr}/v1/auth/jwt/login", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                token = json.load(r)["auth"]["client_token"]
        except (urllib.error.URLError, KeyError, ValueError) as e:
            raise NgError(f"Vault JWT login failed: {type(e).__name__}") from None
    mount, _, rest = path.partition("/")
    req = urllib.request.Request(f"{addr}/v1/{mount}/data/{rest}", headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            data = json.load(r)["data"]["data"]
    except urllib.error.HTTPError as e:
        raise NgError(f"Vault read of {path} failed: HTTP {e.code}") from None
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise NgError(f"Vault read of {path} failed: {type(e).__name__}") from None
    for k in ("username", "password", "host"):
        if not data.get(k):
            raise NgError(f"Vault secret {path} has no '{k}' field")
    return {"host": data["host"], "username": data["username"], "password": data["password"]}


def credentials(desired: dict) -> dict:
    env = {k: os.environ.get(f"NETGEAR_{k.upper()}") for k in ("host", "username", "password")}
    if all(env.values()):
        return env
    return vault_read(desired["vault_path"])


# =============================================================================
# Switch client (web UI API)
# =============================================================================

def _rsa_pkcs1_encrypt(pem_body: str, message: bytes) -> str:
    der = base64.b64decode(re.sub(r"\s", "", pem_body))

    def rdlen(b, i):
        n = b[i]
        i += 1
        if n & 0x80:
            nb = n & 0x7F
            n = int.from_bytes(b[i:i + nb], "big")
            i += nb
        return n, i

    _, i = rdlen(der, 1)          # SEQUENCE
    i += 1                        # INTEGER tag (modulus)
    ln, i = rdlen(der, i)
    n = int.from_bytes(der[i:i + ln], "big")
    i += ln + 1                   # INTEGER tag (exponent)
    le, i = rdlen(der, i)
    e = int.from_bytes(der[i:i + le], "big")
    klen = (n.bit_length() + 7) // 8
    if len(message) > klen - 11:
        raise NgError("login message too long for the switch key")
    pad = b""
    while len(pad) < klen - 3 - len(message):
        c = os.urandom(1)
        if c != b"\x00":
            pad += c
    h = format(pow(int.from_bytes(b"\x00\x02" + pad + b"\x00" + message, "big"), e, n), "x")
    return ("0" + h) if len(h) % 2 else h


def _children(el: ET.Element) -> dict:
    return {c.tag: (c.text or "").strip() for c in el if len(c) == 0}


class Switch:
    def __init__(self, host: str, username: str, password: str, timeout: int = 30, log=None):
        self.host = host
        self.base = f"http://{host}"
        self._user = username
        self._pw = password
        self.timeout = timeout
        self.cs = None
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cj))
        self.iface_extra: Dict[str, dict] = {}
        self.poe_limit_mode = None

    # -- transport -------------------------------------------------------------
    def _open(self, url, data=None, headers=None):
        h = {"User-Agent": "Mozilla/5.0 (ngswitch)"}
        h.update(headers or {})
        r = self.op.open(urllib.request.Request(url, data=data, headers=h), timeout=self.timeout)
        body = r.read()
        if body[:2] == b"\x1f\x8b":
            import gzip
            body = gzip.decompress(body)
        return r, body

    def _cookie(self, name, value):
        self.cj.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, self.host, False, False, "/", True, False, None, True, None, None, {}))

    def login(self, attempts: int = 4):
        last = "unknown"
        for n in range(attempts):
            try:
                r, _ = self._open(self.base + "/")
                m = re.search(r"/(cs[0-9a-f]+)/", r.geturl())
                if not m:
                    raise NgError("could not find the session path prefix")
                self.cs = m.group(1)
                _, body = self._open(f"{self.cfg}/device/wcd?{{EncryptionSetting}}")
                km = re.search(r"BEGIN RSA PUBLIC KEY-----(.*?)-----END", body.decode("latin-1"), re.S)
                if not km:
                    raise NgError("switch did not return its login key")
                msg = f"user={self._user}&password={self._pw}&ssd=true&".encode()
                cred = _rsa_pkcs1_encrypt(km.group(1), msg)
                r, body = self._open(f"{self.cfg}/system.xml?action=login&cred={cred}")
                text = body.decode("latin-1")
                if "<statusCode>0</statusCode>" in text:
                    sid = r.headers.get("sessionID")
                    if sid:
                        self._cookie("sessionID", sid)
                        self._cookie("http:sessionID", sid)
                    self._cookie("userStatus", "ok")
                    self._cookie("usernme", self._user)
                    return
                sm = re.search(r"<statusString>([^<]*)</statusString>", text)
                last = sm.group(1) if sm else "login refused"
            except (urllib.error.URLError, OSError) as e:
                last = type(e).__name__
            if n + 1 < attempts:
                time.sleep(10 * (n + 1))
        raise NgError(f"login to {self.host} failed: {last}")

    def logout(self):
        if not self.cs:
            return
        try:
            self._open(f"{self.cfg}/authentication_page.htm?logOff")
        except Exception:  # noqa: BLE001 - best effort
            pass
        self.cs = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    @property
    def cfg(self):
        return f"{self.base}/{self.cs}/config"

    def read(self, section: str) -> ET.Element:
        _, body = self._open(f"{self.cfg}/device/wcd?{{{section}}}")
        root = ET.fromstring(body)
        code = root.findtext("ActionStatus/statusCode") or ""
        if code not in ("", "0"):
            raise NgError(f"read of {section} failed: status {code} {root.findtext('ActionStatus/statusString') or ''}")
        el = root.find(f"DeviceConfiguration/{section}")
        if el is None:
            raise NgError(f"read of {section}: section missing from response")
        return el

    def post(self, xml: bytes) -> None:
        _, body = self._open(f"{self.cfg}/device/wcd", data=xml, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"})
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise NgError("switch returned a non-XML response to a write") from None
        code = root.findtext(".//ActionStatus/statusCode")
        if code != "0":
            msg = root.findtext(".//ActionStatus/statusString") or ""
            raise NgError(f"write rejected by switch: status {code} {msg}".strip())

    def export(self) -> str:
        _, body = self._open(f"{self.base}/{self.cs}/Maintenance/Http_download?action=2")
        text = body.decode("latin-1")
        if "config-file-header" not in text[:400]:
            raise NgError("configuration download did not return a configuration file")
        return sanitize(text)

    # -- firmware ----------------------------------------------------------------
    def image_status(self) -> dict:
        return parse_image_status(self.read("ImageUnitList"))

    def load_status(self) -> Optional[dict]:
        try:
            return _children(self.read("LoadStatus"))
        except NgError:
            return None

    def clear_load_status(self) -> None:
        try:
            self.post(build_xml("LoadStatus", "delete", _el("unitID", 0)))
        except NgError:
            pass

    def upload_firmware(self, image: Path, timeout: int = 1200, progress=None) -> None:
        """HTTP File Download (Maintenance > Download), file type Software, then wait for the flash write."""
        _, form = self._open(f"{self.base}/{self.cs}/Maintenance/httpconfigprocess.htm")
        m = re.search(r'id="rlCopyFreeHistoryIndex\$scalar"\s+value="([^"]*)"', form.decode("latin-1"))
        if not m:
            raise NgError("upload form did not contain rlCopyFreeHistoryIndex")
        idx = m.group(1)
        fields = [("restoreUrl", ""), ("errorCollector", ""), ("rlCopyFreeHistoryIndex$scalar", idx),
                  ("rlCopyDestinationFileType", FW_FILE_TYPE_SOFTWARE), ("rlCopyFreeHistoryIndex", idx)]
        body, ctype = multipart_body(fields, "srcFileName", image.name, image.read_bytes())
        old = self.timeout
        self.timeout = 600
        try:
            self._open(f"{self.base}/{self.cs}/Maintenance/httpconfigprocess.htm", data=body,
                       headers={"Content-Type": ctype})
        finally:
            self.timeout = old
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            st = self.load_status()
            code = int(st.get("copyStatusType") or 0) if st else 0
            if code in (3, 4):
                msg = st.get("errorMessage") or "transfer failed"
                self.clear_load_status()
                raise NgError(f"firmware upload failed on the switch: {msg}")
            if code == 5:
                if progress:
                    progress(f"   transfer done: {st.get('destinationFileName', '?')}, "
                             f"{st.get('bytesTransfered', '?')} bytes")
                self.clear_load_status()
                return
            if progress and st and st.get("bytesTransfered") != last:
                last = st.get("bytesTransfered")
                progress(f"   transfer status {code}, bytes {last}")
            time.sleep(5)
        raise NgError(f"firmware upload did not finish within {timeout}s")

    def set_next_boot(self, slot: int) -> None:
        """Dual Image Configuration: activate image<slot> for the next boot (keeps its description)."""
        entry = _children(self.read("ImageUnitList").find("Entry"))
        self.post(next_boot_payload(slot, entry.get("currentImage", ""), entry.get(f"image{slot}Description", "")))

    def reboot(self) -> None:
        xml = build_xml("Reload", "set", _el("UnitList", None, _el("UnitEntry", None, _el("unitID", 1))))
        try:
            self.post(xml)
        except (NgError, urllib.error.URLError, OSError):
            pass  # the switch may drop the connection while it goes down
        self.cs = None  # sessions do not survive the reboot

    def wait_for_return(self, timeout: int, down_timeout: int = 180, log=None) -> float:
        """Wait for the web UI to go away and come back. Tolerates any network error. Returns seconds."""
        log = log or self.log
        t0 = time.time()
        def up():
            try:
                req = urllib.request.Request(self.base + "/", headers={"User-Agent": "Mozilla/5.0 (ngswitch)"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return bool(re.search(r"/cs[0-9a-f]+/", r.geturl()))
            except Exception:  # noqa: BLE001 - any failure means "not up"
                return False
        while time.time() - t0 < down_timeout and up():
            time.sleep(3)
        log(f"   switch went down after {time.time() - t0:.0f}s")
        while time.time() - t0 < timeout:
            if up():
                log(f"   web UI answering after {time.time() - t0:.0f}s")
                return time.time() - t0
            time.sleep(10)
        raise NgError(f"switch web UI did not come back within {timeout}s")

    # -- state -------------------------------------------------------------------
    def read_state(self) -> Tuple[dict, List[int]]:
        vlans: Dict[int, str] = {}
        for v in self.read("VLANList").findall("VLAN"):
            vlans[int(v.findtext("VLANID"))] = (v.findtext("VLANName") or "").strip()
        ports: Dict[str, dict] = {}
        for it in self.read("Standard802_3List").findall("Entry"):
            d = _children(it)
            if d.get("interfaceType") != "1":
                continue
            if d.get("LAGID") not in (None, "", "0"):
                raise NgError(f"{d['interfaceName']} is a LAG member; LAGs are not supported")
            ports[d["interfaceName"]] = {"pvid": 1, "untagged": set(), "tagged": set(), "poe": None}
        for it in self.read("VLANInterfaceList").findall("Interface"):
            d = _children(it)
            if d.get("interfaceType") != "1" or d["interfaceName"] not in ports:
                continue
            ports[d["interfaceName"]]["pvid"] = int(d["PVID"])
            self.iface_extra[d["interfaceName"]] = {k: d.get(k, "") for k in ("frameType", "ingressFilteringEnabled", "portDefaultCoS")}
        for v in self.read("VLANMembershipList").findall("VLAN"):
            vid = int(v.findtext("VLANID"))
            for mem in v.findall("MembershipList/VLANMember"):
                d = _children(mem)
                # LAG interfaces are not managed (no physical port is a LAG
                # member, checked above); their VLAN lines are in the snapshot.
                if d.get("interfaceType") != "1" or d["interfaceName"] not in ports:
                    continue
                ports[d["interfaceName"]][UI_TO_TAGGING[d["taggingMode"]]].add(vid)
        try:
            self.poe_limit_mode = self.read("PoEGlobalSetting").findtext("powerLimitMode")
            poe_list = self.read("PoEPSEInterfaceList").findall("Interface")
        except NgError:
            poe_list = []
        for it in poe_list:
            d = _children(it)
            if d["interfaceName"] not in ports:
                continue
            ports[d["interfaceName"]]["poe"] = {
                "enabled": d.get("adminEnable") == "1",
                "priority": UI_TO_PRIORITY.get(d.get("powerPriority"), d.get("powerPriority")),
                "limit_mw": int(d["powerLimit"]) if self.poe_limit_mode == "0" and d.get("powerLimit") else None,
            }
        mgmt = []
        for e in self.read("IPv4InterfaceList").findall("ifEntry"):
            m = re.match(r"VLAN(\d+)$", e.findtext("interfaceName") or "")
            if m:
                mgmt.append(int(m.group(1)))
        return normalize_state({"vlans": vlans, "ports": ports}), mgmt


# -- write payloads (format follows the switch's own UI pages) --------------------

def _el(tag: str, text=None, *children) -> ET.Element:
    e = ET.Element(tag)
    if text is not None:
        e.text = str(text)
    e.extend(children)
    return e


def build_xml(section: str, action: str, *items: ET.Element) -> bytes:
    root = ET.Element("DeviceConfiguration")
    sec = ET.SubElement(root, section, action=action)
    sec.extend(items)
    return b"<?xml version='1.0' encoding='utf-8'?>" + ET.tostring(root, encoding="unicode").encode()


FW_FILE_TYPE_SOFTWARE = "8"   # rlCopyDestinationFileType on the HTTP File Download page


def next_boot_payload(slot: int, current: str, description: str) -> bytes:
    """Same XML as the Dual Image Configuration page with "Activate Image" checked."""
    if slot not in (1, 2):
        raise NgError(f"image slot must be 1 or 2, not {slot}")
    tag = f"image{slot}Description"
    x = build_xml("ImageUnitList", "set", _el(
        "Entry", None, _el("nextBootImage", slot), _el("unitID", 1), _el("currentImage", current),
        _el(tag, description)))
    # The UI sends an empty element as an open/close pair; do the same.
    return x.replace(f"<{tag} />".encode(), f"<{tag}></{tag}>".encode())


def multipart_body(fields: List[Tuple[str, str]], file_field: str, filename: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "----ngswitch" + base64.b16encode(os.urandom(12)).decode().lower()
    out = []
    for k, v in fields:
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
               f"Content-Type: application/octet-stream\r\n\r\n".encode())
    out.append(data)
    out.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def payload(c: dict, iface_extra: Optional[dict] = None, poe_limit_mode: Optional[str] = "0") -> bytes:
    op = c["op"]
    if op in ("vlan_add", "vlan_rename"):
        return build_xml("VLANList", "set", _el("VLAN", None, _el("VLANID", c["vlan"]), _el("VLANName", c["name"])))
    if op == "vlan_remove":
        return build_xml("VLANList", "delete", _el("VLAN", None, _el("VLANID", c["vlan"])))
    if op == "member_set":
        mem = _el("VLANMember", None, _el("interfaceName", c["port"]), _el("interfaceType", 1),
                  _el("membershipType", 2), _el("taggingMode", TAGGING_TO_UI[c["mode"]]))
        return build_xml("VLANMembershipList", "set",
                         _el("VLAN", None, _el("VLANID", c["vlan"]), _el("MembershipList", None, mem)))
    if op == "member_remove":
        mem = _el("VLANMember", None, _el("interfaceName", c["port"]), _el("interfaceType", 1))
        return build_xml("VLANMembershipList", "delete",
                         _el("VLAN", None, _el("VLANID", c["vlan"]), _el("MembershipList", None, mem)))
    if op == "pvid":
        extra = (iface_extra or {}).get(c["port"])
        if not extra:
            raise NgError(f"{c['port']}: missing current port VLAN settings for the PVID write")
        return build_xml("VLANInterfaceList", "set", _el(
            "Interface", None, _el("interfaceName", c["port"]), _el("PVID", c["pvid"]),
            _el("frameType", extra["frameType"]), _el("ingressFilteringEnabled", extra["ingressFilteringEnabled"]),
            _el("portDefaultCoS", extra["portDefaultCoS"])))
    if op == "poe":
        n = c["new"]
        kids = [_el("interfaceName", c["port"]), _el("adminEnable", "1" if n["enabled"] else "2"),
                _el("powerPriority", PRIORITY_TO_UI[n["priority"]])]
        if poe_limit_mode == "0" and n.get("limit_mw") is not None:
            kids.append(_el("powerLimit", n["limit_mw"]))
        return build_xml("PoEPSEInterfaceList", "set", _el("Interface", None, *kids))
    raise NgError(f"unknown change {op}")


# =============================================================================
# Firmware artifact (download, verify, extract)
# =============================================================================

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def firmware_cache_dir(arg: Optional[str]) -> Path:
    if arg:
        return Path(arg)
    if os.environ.get("NETGEAR_FIRMWARE_DIR"):
        return Path(os.environ["NETGEAR_FIRMWARE_DIR"])
    return Path.home() / ".cache" / "ngswitch-firmware"


def image_version(blob: bytes) -> Optional[str]:
    """Version string from the header of a NETGEAR .bin image ("NG10" magic, version at 0x14)."""
    if blob[12:16] != b"NG10":
        return None
    m = re.match(rb"(\d+\.\d+\.\d+\.\d+)\x00", blob[0x14:0x34])
    return m.group(1).decode() if m else None


def fetch_firmware(fw: dict, cache: Path, log=print) -> Path:
    """Return the path of the verified .bin image, downloading the zip if needed.

    The zip and the image are both checked against the desired-state hashes;
    a cached file that does not match is downloaded again.
    """
    cache.mkdir(parents=True, exist_ok=True)
    zpath = cache / fw["url"].rsplit("/", 1)[-1]
    ipath = cache / fw["image"]
    if ipath.exists() and sha256_file(ipath) == fw["image_sha256"]:
        log(f"   firmware image cached: {ipath} (sha256 ok)")
        return ipath
    if not (zpath.exists() and sha256_file(zpath) == fw["sha256"]):
        log(f"   downloading {fw['url']}")
        tmp = zpath.with_name(zpath.name + ".part")
        req = urllib.request.Request(fw["url"], headers={"User-Agent": "Mozilla/5.0 (ngswitch)"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
        except (urllib.error.URLError, OSError) as e:
            raise NgError(f"firmware download failed: {type(e).__name__}: {e}") from None
        got = sha256_file(tmp)
        if got != fw["sha256"]:
            tmp.unlink()
            raise NgError(f"firmware zip sha256 mismatch: got {got}, desired state says {fw['sha256']}")
        tmp.replace(zpath)
    log(f"   firmware zip verified: {zpath}")
    with zipfile.ZipFile(zpath) as z:
        if fw["image"] not in z.namelist():
            raise NgError(f"{fw['image']} is not in {zpath.name} (contents: {', '.join(z.namelist())})")
        tmp = ipath.with_name(ipath.name + ".part")
        with z.open(fw["image"]) as src, open(tmp, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
    got = sha256_file(tmp)
    if got != fw["image_sha256"]:
        tmp.unlink()
        raise NgError(f"firmware image sha256 mismatch: got {got}, desired state says {fw['image_sha256']}")
    with open(tmp, "rb") as fh:
        v = image_version(fh.read(256))
    if v != fw["version"]:
        tmp.unlink()
        raise NgError(f"image header names version {v}, desired state says {fw['version']}")
    tmp.replace(ipath)
    log(f"   firmware image verified: {ipath} (version {v})")
    return ipath


def parse_image_status(el: ET.Element) -> dict:
    """ImageUnitList -> {"image1", "image2", "current", "next"} (slots are 1 or 2)."""
    entry = el.find("Entry")
    if entry is None:
        entry = el.find("Unit")
    d = _children(entry if entry is not None else el)
    def slot(v):
        return int(v) if v in ("1", "2") else None
    return {"image1": d.get("image1Version", ""), "image2": d.get("image2Version", ""),
            "current": slot(d.get("currentImage")), "next": slot(d.get("nextBootImage"))}


def running_version(img: dict) -> Optional[str]:
    return img.get(f"image{img['current']}") if img.get("current") else None


def describe_images(img: dict) -> str:
    return (f"image1 {img['image1'] or '-'}, image2 {img['image2'] or '-'}, "
            f"running image{img['current']}, next boot image{img['next']}")


# =============================================================================
# Comparison helpers
# =============================================================================

def state_diff_text(a: dict, b: dict, la: str, lb: str) -> str:
    def dump(s):
        return json.dumps(s, indent=1, sort_keys=True, default=sorted).splitlines()
    return "\n".join(difflib.unified_diff(dump(a), dump(b), la, lb, lineterm="", n=2))


# Export header line naming the running firmware, e.g. "v6.7.0.42 / RNG510_923_071_017".
_FW_HEADER_RE = re.compile(r"^v(\d+(?:\.\d+){3})\s*/")


def export_firmware_version(text: str) -> Optional[str]:
    """Firmware version from the header of an export, or None."""
    for line in text.split("\n")[:10]:
        m = _FW_HEADER_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def unmanaged_diff(snapshot_text: str, live_text: str) -> str:
    """Diff of everything outside the managed settings (order-insensitive per context).

    The firmware header line is left out: firmware is compared on its own
    (see the firmware block of the desired state).
    """
    _, us = parse_running_config(snapshot_text)
    _, ul = parse_running_config(live_text)

    def keep(c, l):
        return not (c == "" and _FW_HEADER_RE.match(l))
    fa = [f"[{c or 'global'}] {l}" for c, l in sorted(us) if keep(c, l)]
    fb = [f"[{c or 'global'}] {l}" for c, l in sorted(ul) if keep(c, l)]
    return "\n".join(difflib.unified_diff(fa, fb, "snapshot", "live", lineterm="", n=1))


def strip_poe(state: dict) -> dict:
    s = json.loads(json.dumps(state))
    for p in s["ports"].values():
        p["poe"] = None
        p.pop("poe_lines", None)
    s["vlans"] = {int(k): v for k, v in s["vlans"].items()}
    return s


# =============================================================================
# Commands
# =============================================================================

def _desired_files(args_files: List[str]) -> List[Path]:
    if args_files:
        return [Path(f) for f in args_files]
    return sorted(p for p in HERE.glob("*.yml"))


def _snapshot_state(desired: dict, text: str) -> dict:
    st, _ = parse_running_config(text, offline_poe_defaults(desired))
    return st


def cmd_check(args) -> int:
    rc = 0
    for f in _desired_files(args.files):
        try:
            d = load_desired(f)
            snap = snapshot_path(f)
            if not snap.exists():
                raise NgError(f"{f.name}: snapshot {snap.name} is missing")
            text = snap.read_text()
            assert_sanitized(text)
            st = _snapshot_state(d, text)
            changes, _ = plan(st, d["state"])
            if changes:
                print(f"{f.name}: snapshot {snap.name} does not match the desired state;"
                      f" run 'ngswitch.py render-snapshot {f.name}' and commit the result:")
                for c in changes:
                    print(f"  - {describe(c)}")
                rc = 1
            else:
                print(f"{f.name}: OK (schema valid, snapshot consistent)")
        except NgError as e:
            print(f"ERROR {e}")
            rc = 1
    return rc


def _print_plan(name: str, changes: List[dict], notes: List[str], source: str) -> None:
    print(f"== {name}: plan against {source}")
    for n in notes:
        print(f"   note: {n}")
    if not changes:
        print("   no changes")
    for c in changes:
        print(f"   ~ {describe(c)}")


def cmd_plan(args) -> int:
    rc = 0
    for f in _desired_files(args.files):
        d = load_desired(f)
        if args.offline:
            snap = Path(args.snapshot) if args.snapshot and len(args.files) <= 1 else snapshot_path(f)
            if args.snapshot_dir:
                snap = Path(args.snapshot_dir) / snapshot_path(f).name
            if not snap.exists():
                print(f"== {d['name']}: no committed snapshot ({snap}); first apply will read the live switch")
                continue
            current = _snapshot_state(d, snap.read_text())
            changes, notes = plan(current, d["state"])
            mgmt = None
            source = f"snapshot {snap}"
        else:
            with Switch(**credentials(d)) as sw:
                current, mgmt = sw.read_state()
            changes, notes = plan(current, d["state"])
            source = "live switch"
        _print_plan(d["name"], changes, notes, source)
        reasons = check_guard(changes, d, current, mgmt)
        for r in reasons:
            print(f"   GUARD {r}")
        if reasons:
            rc = 1
        elif changes and args.detailed_exitcode:
            rc = max(rc, 2)
    return rc


def cmd_apply(args) -> int:
    rc = 0
    for f in _desired_files(args.files):
        d = load_desired(f)
        name = d["name"]
        with Switch(**credentials(d)) as sw:
            current, mgmt = sw.read_state()
            changes, notes = plan(current, d["state"])
            _print_plan(name, changes, notes, "live switch")
            reasons = check_guard(changes, d, current, mgmt)
            if reasons:
                raise GuardError(f"{name}: refusing to apply:\n  " + "\n  ".join(reasons))
            if len(changes) > args.max_changes:
                raise NgError(f"{name}: {len(changes)} changes exceed --max-changes {args.max_changes}; refusing")
            if not changes:
                print(f"== {name}: nothing to do")
                continue
            if args.dry_run:
                print(f"== {name}: dry run, nothing written")
                continue
            done = 0
            for c in changes:
                try:
                    sw.post(payload(c, sw.iface_extra, sw.poe_limit_mode))
                except NgError as e:
                    raise NgError(f"{name}: write {done + 1}/{len(changes)} failed ({describe(c)}): {e}; "
                                  f"{done} change(s) were applied before it") from None
                done += 1
                print(f"   applied: {describe(c)}")
            after, mgmt_after = sw.read_state()
            remaining, _ = plan(after, d["state"])
            export = sw.export()
        if remaining:
            print(f"!! {name}: VERIFY FAILED, the switch still differs from the desired state:")
            for c in remaining:
                print(f"   ~ {describe(c)}")
            rc = 1
            continue
        pdef = offline_poe_defaults(d)
        exp_state, _ = parse_running_config(export, pdef)
        exp_changes, _ = plan(exp_state, d["state"] if pdef else strip_poe(d["state"]))
        if exp_changes:
            print(f"!! {name}: VERIFY FAILED, the exported configuration differs from the desired state:")
            for c in exp_changes:
                print(f"   ~ {describe(c)}")
            rc = 1
            continue
        if d["management"]["vlan"] not in mgmt_after:
            print(f"!! {name}: management IP interface no longer on VLAN {d['management']['vlan']}")
            rc = 1
            continue
        print(f"== {name}: applied {done} change(s); live state and export match the desired state")
        if args.export_dir:
            out = Path(args.export_dir) / snapshot_path(f).name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(export)
            print(f"   sanitized export written to {out}")
    return rc


def cmd_drift(args) -> int:
    rc = 0
    report: List[str] = []
    for f in _desired_files(args.files):
        d = load_desired(f)
        name = d["name"]
        snap = snapshot_path(f)
        fw_line = None
        with Switch(**credentials(d)) as sw:
            current, mgmt = sw.read_state()
            export = sw.export()
            if d["firmware"] is not None:
                try:
                    fw_line, behind = firmware_status_line(name, d["firmware"], sw.image_status())
                    fw_line = ("   note: " if behind else "   ") + fw_line[3:]
                except NgError as e:
                    fw_line = f"   note: firmware status not read: {e}"
        changes, notes = plan(current, d["state"])
        problems: List[str] = []
        if changes:
            problems.append("live switch differs from the desired state (apply would make these changes):")
            problems += [f"  ~ {describe(c)}" for c in changes]
        if d["management"]["vlan"] not in mgmt:
            problems.append(f"management IP interface is not on VLAN {d['management']['vlan']}")
        if snap.exists():
            snap_text = snap.read_text()
            us = unmanaged_diff(snap_text, export)
            if us:
                problems.append(f"unmanaged configuration differs from {snap.name}:")
                problems += ["  " + l for l in us.splitlines()]
            snap_state = strip_poe(_snapshot_state(d, snap_text))
            exp_state, _ = parse_running_config(export)
            if strip_poe(exp_state) != snap_state:
                problems.append(f"managed lines of the export differ from {snap.name}:")
                problems += ["  " + l for l in state_diff_text(snap_state, strip_poe(exp_state), "snapshot", "live").splitlines()]
        else:
            problems.append(f"no committed snapshot {snap.name}")
        if args.export_dir:
            out = Path(args.export_dir) / snap.name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(export)
        if problems:
            rc = 2
            report.append(f"== {name}: DRIFT")
            report += ["   " + p for p in problems]
        else:
            report.append(f"== {name}: no drift")
        if fw_line:
            # Informational only: a firmware behind the desired version does not fail drift.
            report.append(fw_line)
    print("\n".join(report))
    if args.report:
        Path(args.report).write_text("\n".join(report) + "\n")
    return rc


def firmware_status_line(name: str, fw: Optional[dict], img: dict) -> Tuple[str, bool]:
    """Return (report line, behind) for one switch."""
    run = running_version(img)
    if fw is None:
        return f"== {name}: firmware {run} ({describe_images(img)}); no desired version declared", False
    if run == fw["version"]:
        return f"== {name}: firmware {run} = desired ({describe_images(img)})", False
    return f"== {name}: firmware {run}, desired {fw['version']}: BEHIND ({describe_images(img)})", True


def cmd_firmware_status(args) -> int:
    rc = 0
    for f in _desired_files(args.files):
        d = load_desired(f)
        with Switch(**credentials(d)) as sw:
            img = sw.image_status()
        line, behind = firmware_status_line(d["name"], d["firmware"], img)
        print(line)
        if behind and args.detailed_exitcode:
            rc = 2
    return rc


def _stamp(t0: float):
    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')} +{time.time() - t0:5.0f}s] {msg}", flush=True)
    return log


def cmd_firmware_upgrade(args) -> int:
    t0 = time.time()
    log = _stamp(t0)
    f = Path(args.file)
    d = load_desired(f)
    name, fw = d["name"], d["firmware"]
    if fw is None:
        raise NgError(f"{f.name}: no firmware block in the desired state")
    image = fetch_firmware(fw, firmware_cache_dir(args.cache_dir), log)
    creds = credentials(d)
    sw = Switch(**creds, log=log)
    sw.login()
    try:
        img = sw.image_status()
        run = running_version(img)
        log(f"{name}: {describe_images(img)}")
        if run == fw["version"]:
            raise NgError(f"{name}: already running {run}; refusing to upgrade to the same version")
        if img["current"] is None:
            raise NgError(f"{name}: could not tell which image is running")
        current, mgmt = sw.read_state()
        changes, _ = plan(current, d["state"])
        if changes:
            raise NgError(f"{name}: live switch differs from the desired state ({len(changes)} change(s)); "
                          "run apply or drift first so a post-upgrade check means something")
        if d["management"]["vlan"] not in mgmt:
            raise NgError(f"{name}: management IP interface is not on VLAN {d['management']['vlan']}")
        backup = Path(args.backup_dir) / f"{name}.pre-{fw['version']}.{time.strftime('%Y%m%dT%H%M%S')}.running-config.txt"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(sw.export())
        log(f"{name}: sanitized pre-upgrade export written to {backup}")
        if args.dry_run:
            log(f"{name}: dry run; would upload {image.name}, boot it and verify")
            return 0
        sw.clear_load_status()
        log(f"{name}: uploading {image.name} ({image.stat().st_size} bytes)")
        sw.upload_firmware(image, timeout=args.upload_timeout, progress=log)
        img = sw.image_status()
        log(f"{name}: after upload: {describe_images(img)}")
        new_slot = slot_after_upload(img, fw["version"])
        fallback = 2 if new_slot == 1 else 1
        if not img[f"image{fallback}"] or img[f"image{fallback}"] == fw["version"]:
            raise NgError(f"{name}: no previous firmware left in image{fallback} to fall back to; not rebooting")
        old_slot, old_version = fallback, img[f"image{fallback}"]
        if img["next"] != new_slot:
            log(f"{name}: setting image{new_slot} as the next boot image")
            sw.set_next_boot(new_slot)
            img = sw.image_status()
            if img["next"] != new_slot:
                raise NgError(f"{name}: next boot image is still image{img['next']} after setting image{new_slot}")
        log(f"{name}: rebooting into image{new_slot} ({fw['version']}); previous {old_version} stays in image{old_slot}")
        sw.reboot()
    finally:
        sw.logout()
    sw.wait_for_return(args.reboot_timeout, log=log)
    sw2 = Switch(**creds, log=log)
    deadline = time.time() + 300
    while True:  # the web UI answers before login works
        try:
            sw2.login(attempts=1)
            break
        except NgError:
            if time.time() > deadline:
                raise
            time.sleep(15)
    try:
        img = sw2.image_status()
    finally:
        sw2.logout()
    run = running_version(img)
    log(f"{name}: after reboot: {describe_images(img)}")
    if run != fw["version"]:
        raise NgError(f"{name}: switch came back running {run}, not {fw['version']}")
    log(f"{name}: running {run}; checking for drift")
    drc = cmd_drift(argparse.Namespace(files=[str(f)], report=args.report, export_dir=args.export_dir))
    if drc != 0:
        print(f"!! {name}: configuration drift after the upgrade (see above). To fall back to {old_version}:\n"
              f"   web UI: Maintenance > File Management > Dual Image > Dual Image Configuration,\n"
              f"   make image{old_slot} the active image, then Maintenance > Reset > Device Reboot.")
        return 1
    log(f"{name}: upgraded {old_version} -> {run} in {time.time() - t0:.0f}s; no drift")
    return 0


def slot_after_upload(img: dict, version: str) -> int:
    """Slot holding the newly uploaded version (prefer the one not running)."""
    slots = [s for s in (1, 2) if img[f"image{s}"] == version]
    if not slots:
        raise NgError(f"neither image slot holds {version} after the upload ({describe_images(img)})")
    other = [s for s in slots if s != img["current"]]
    return other[0] if other else slots[0]


def cmd_export(args) -> int:
    d = load_desired(Path(args.file))
    with Switch(**credentials(d)) as sw:
        text = sw.export()
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_generate(args) -> int:
    stub = {"vault_path": args.vault_path}
    creds = credentials(stub)
    with Switch(**creds) as sw:
        current, mgmt = sw.read_state()
        export = sw.export()
    if args.mgmt_vlan not in mgmt:
        raise NgError(f"management VLAN {args.mgmt_vlan} has no IP interface on the switch")
    carriers = [p for p, s in current["ports"].items() if args.mgmt_vlan in s["untagged"] + s["tagged"]]
    if args.uplink not in carriers:
        raise NgError(f"uplink {args.uplink} does not carry VLAN {args.mgmt_vlan}")
    protected = sorted(set(carriers) | {args.uplink}, key=port_sort_key)
    poe_vals = [p["poe"] for p in current["ports"].values() if p["poe"] is not None]
    poe_defaults = None
    if poe_vals:
        poe_defaults = {"enabled": True, "priority": "low", "limit_mw": max(p["limit_mw"] or 0 for p in poe_vals) or None}
    text = render_desired_yaml(args.name, args.model, args.vault_path, args.mgmt_vlan, args.uplink,
                               protected, current, poe_defaults)
    d = validate_desired(yaml.safe_load(text), args.name)
    changes, _ = plan(current, d["state"])
    if changes:
        raise NgError("generated desired state does not round-trip: " + "; ".join(describe(c) for c in changes))
    out = Path(args.output_dir)
    (out / f"{args.name}.yml").write_text(text)
    (out / f"{args.name}.running-config.txt").write_text(export)
    print(f"wrote {out / (args.name + '.yml')} and {args.name}.running-config.txt; protected ports: {', '.join(protected)}")
    return 0


def cmd_render_snapshot(args) -> int:
    for f in _desired_files(args.files):
        d = load_desired(f)
        snap = snapshot_path(f)
        text = render_snapshot(snap.read_text(), d)
        assert_sanitized(text)
        changes, _ = plan(_snapshot_state(d, text), d["state"])
        if changes:
            raise NgError(f"{f.name}: rendered snapshot does not match: " + "; ".join(describe(c) for c in changes))
        snap.write_text(text)
        print(f"{snap.name}: updated")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="offline schema and snapshot consistency check")
    s.add_argument("files", nargs="*")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("export", help="write the sanitized running configuration")
    s.add_argument("file")
    s.add_argument("-o", "--output")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("plan", help="show the changes needed to reach the desired state")
    s.add_argument("files", nargs="*")
    s.add_argument("--offline", action="store_true", help="compare against a snapshot instead of the live switch")
    s.add_argument("--snapshot", help="snapshot file (single desired-state file only)")
    s.add_argument("--snapshot-dir", help="directory holding <name>.running-config.txt baselines")
    s.add_argument("--detailed-exitcode", action="store_true", help="exit 2 when there are changes")
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("apply", help="write only the differences, then verify")
    s.add_argument("files", nargs="*")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max-changes", type=int, default=24)
    s.add_argument("--export-dir", help="write the post-apply sanitized export here")
    s.set_defaults(fn=cmd_apply)

    s = sub.add_parser("drift", help="compare the live switch with desired state and snapshot")
    s.add_argument("files", nargs="*")
    s.add_argument("--report", help="also write the report to this file")
    s.add_argument("--export-dir", help="write the live sanitized export here")
    s.set_defaults(fn=cmd_drift)

    s = sub.add_parser("firmware", help="firmware status / upgrade (upgrade reboots the switch)")
    fsub = s.add_subparsers(dest="fwcmd", required=True)
    fs = fsub.add_parser("status", help="running and stored image versions vs the desired version")
    fs.add_argument("files", nargs="*")
    fs.add_argument("--detailed-exitcode", action="store_true", help="exit 2 when a switch is behind")
    fs.set_defaults(fn=cmd_firmware_status)
    fs = fsub.add_parser("upgrade", help="upload the desired firmware, boot it, verify, check drift")
    fs.add_argument("file")
    fs.add_argument("--cache-dir", help="firmware download cache (default $NETGEAR_FIRMWARE_DIR or ~/.cache/ngswitch-firmware)")
    fs.add_argument("--backup-dir", default="netgear-backup", help="where the pre-upgrade export is written")
    fs.add_argument("--export-dir", help="write the post-upgrade sanitized export here")
    fs.add_argument("--report", help="write the post-upgrade drift report here")
    fs.add_argument("--upload-timeout", type=int, default=1200)
    fs.add_argument("--reboot-timeout", type=int, default=900)
    fs.add_argument("--dry-run", action="store_true", help="checks and backup only; no upload or reboot")
    fs.set_defaults(fn=cmd_firmware_upgrade)

    s = sub.add_parser("generate", help="write a desired-state file and snapshot from the live switch")
    s.add_argument("--name", required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--vault-path", required=True)
    s.add_argument("--mgmt-vlan", type=int, required=True)
    s.add_argument("--uplink", required=True)
    s.add_argument("--output-dir", default=str(HERE))
    s.set_defaults(fn=cmd_generate)

    s = sub.add_parser("render-snapshot", help="update managed lines of the snapshot from the desired state")
    s.add_argument("files", nargs="*")
    s.set_defaults(fn=cmd_render_snapshot)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except NgError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
