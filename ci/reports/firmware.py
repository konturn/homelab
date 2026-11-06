#!/usr/bin/env python3
"""Monthly read-only firmware inventory (runs on the router).

NETGEAR switches (web UI config download header), the Aruba Instant cluster
(REST `show version`), the Aruba S2500 (SSH CLI through
networking/aruba-s2500/s2500ctl.py when its credentials are in Vault, else
SNMPv2c sysDescr), and the router's BIOS/BMC (dmidecode, ipmitool) and drives
(smartctl -i). Nothing is changed on any device. Compared against KNOWN_LATEST
where an entry exists; the firmware bump jobs update the MS510TXPP and AP-515
entries, the others are maintained by hand and dated. Writes firmware.{md,json}.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import common  # noqa: E402

# Latest firmware known for each model: (version, date this entry was checked).
# AP-515: the release declared in networking/aruba-iap/firmware.yml (the HPE portal
# cannot be checked automatically; `iapctl.py firmware bump` updates this entry).
# S2500: ArubaOS-MAS 7.4 is the last train for the S2500 and 7.4.1.12 the newest
# release known (networking/aruba-s2500/README.md).
KNOWN_LATEST: Dict[str, Tuple[str, str]] = {
    "MS510TXPP": ("6.7.0.52", "2026-10-19"),
    "AP-515": ("8.13.1.0", "2026-11-06"),
    "S2500": ("7.4.1.12", "2026-11-06"),
}

S2500_HOST = os.environ.get("S2500_HOST", "10.100.0.1")
SYS_DESCR = "1.3.6.1.2.1.1.1.0"


# --------------------------------------------------------------------------- parsers

def parse_netgear_header(text: str) -> Optional[str]:
    m = re.search(r"^v(\d+(?:\.\d+)+)\b", text[:2000], re.M)
    return m.group(1) if m else None


def parse_aruba_version(text: str) -> Tuple[Optional[str], Optional[str]]:
    """'... (MODEL: 515), Version 8.13.1.0 LSR' -> ('8.13.1.0 LSR', '515')"""
    v = re.search(r"Version\s+([\d.]+(?:[-_][\w.]+)?(?:\s+(?:LSR|SSR|LTS|ESR))?)", text)
    m = re.search(r"MODEL:\s*([\w-]+)", text)
    return (v.group(1).strip() if v else None), (m.group(1) if m else None)


def parse_smart_info(text: str) -> Dict[str, str]:
    out = {}
    for key, pat in (("model", r"^(?:Device Model|Model Number|Product):\s*(.+)$"),
                     ("firmware", r"^(?:Firmware Version|Revision):\s*(.+)$")):
        m = re.search(pat, text, re.M)
        if m:
            out[key] = m.group(1).strip()
    return out


def compare(model: str, current: Optional[str]) -> Tuple[Optional[str], Optional[str], bool]:
    known = KNOWN_LATEST.get(model)
    if not known or not current:
        return None, None, False
    return known[0], known[1], common.newer(known[0], current)


# --------------------------------------------------------------------------- probes

def netgear(secrets: dict) -> List[dict]:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "networking", "switches", "netgear"))
    out = []
    try:
        import ngswitch  # type: ignore
    except ImportError as e:
        return [{"name": "netgear", "error": f"ngswitch not available: {e}"}]
    for name in ("netgear-movie", "netgear-office"):
        s = secrets.get(name)
        if not s:
            out.append({"name": name, "model": "MS510TXPP", "error": "no credentials"})
            continue
        try:
            with ngswitch.Switch(s["host"], s["username"], s["password"], log=lambda m: None) as sw:
                ver = parse_netgear_header(sw.export())
            out.append({"name": name, "model": "MS510TXPP", "version": ver})
        except Exception as e:  # noqa: BLE001
            out.append({"name": name, "model": "MS510TXPP", "error": common.clip(f"{type(e).__name__}: {e}", 80)})
    return out


def aruba_iap(secrets: dict) -> dict:
    s = secrets.get("aruba-iap")
    if not s:
        return {"name": "aruba-iap", "error": "no credentials"}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the cluster serves a self-signed certificate
    base = f"https://{s['host']}:4343/rest"

    def post(path, body):
        req = urllib.request.Request(f"{base}/{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.load(urllib.request.urlopen(req, context=ctx, timeout=20))

    try:
        sid = post("login", {"user": s["username"], "passwd": s["password"]}).get("sid")
        if not sid:
            return {"name": "aruba-iap", "error": "login refused"}
        try:
            q = urllib.parse.urlencode({"iap_ip_addr": s["host"], "cmd": "show version", "sid": sid})
            text = json.load(urllib.request.urlopen(f"{base}/show-cmd?{q}", context=ctx, timeout=20)).get(
                "Command output", "")
        finally:
            try:
                post("logout", {"sid": sid})
            except Exception:  # noqa: BLE001
                pass
        ver, model = parse_aruba_version(text)
        return {"name": "aruba-iap", "model": f"AP-{model}" if model else "Instant AP", "version": ver}
    except Exception as e:  # noqa: BLE001
        return {"name": "aruba-iap", "error": common.clip(f"{type(e).__name__}: {e}", 80)}


def _s2500ctl():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "networking", "aruba-s2500"))
    import s2500ctl  # type: ignore
    return s2500ctl


def aruba_s2500(secrets: Optional[dict] = None) -> dict:
    """Running version and both image partitions over SSH when the S2500 credentials
    are in Vault (homelab/network/aruba-s2500), else the running version from SNMP."""
    s = (secrets or {}).get("aruba-s2500")
    out = {"name": "aruba-s2500", "model": "S2500"}
    if s and s.get("username") and s.get("password"):
        try:
            ctl = _s2500ctl()
            sess = ctl.Session(s.get("host") or S2500_HOST, s["username"], s["password"], s.get("enable_secret"))
            try:
                st = ctl.read_state(sess)
            finally:
                sess.close()
            out.update(version=st["running"].release if st["running"] else None, variant=st.get("model"),
                       source="ssh", partitions=[f"{p['partition']} {p['parsed'] or 'empty'}"
                                                 + (" (boot)" if p["default_boot"] else "") for p in st["partitions"]])
            return out
        except Exception as e:  # noqa: BLE001 - fall back to SNMP below
            out["ssh_error"] = common.clip(f"{type(e).__name__}: {e}", 80)
    r = common.run(["snmpget", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-Oqv", S2500_HOST, SYS_DESCR],
                   timeout=20)
    if r.returncode != 0 or not r.stdout.strip():
        out["error"] = "no answer over SNMP" + ("" if s else " and no CLI credentials in Vault (homelab/network/aruba-s2500)")
        return out
    ver, model = parse_aruba_version(r.stdout)
    out.update(version=ver, variant=model, source="snmp")
    return out


def router_platform() -> dict:
    d = {}
    for k in ("bios-version", "bios-release-date", "baseboard-product-name", "system-product-name"):
        d[k] = common.run(["dmidecode", "-s", k]).stdout.strip() or None
    r = common.run(["ipmitool", "mc", "info"], timeout=30)
    m = re.search(r"Firmware Revision\s*:\s*(\S+)", r.stdout)
    d["bmc_firmware"] = m.group(1) if m else None
    return d


def router_drives() -> List[dict]:
    r = common.run(["lsblk", "-dno", "NAME,TYPE"])
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "disk" and not parts[0].startswith(("zd", "loop", "nbd")):
            info = parse_smart_info(common.run(["smartctl", "-i", f"/dev/{parts[0]}"], timeout=30).stdout)
            if info:
                out.append({"dev": parts[0], **info})
    return out


# --------------------------------------------------------------------------- report

def build_report(d: dict) -> Tuple[str, List[str]]:
    lines, updates, notes = [], [], []

    def device_line(x: dict) -> str:
        if "error" in x:
            notes.append(f"{x['name']} n/a")
            return f"{x['name']} ({x.get('model', '?')}): {x['error']}"
        latest, checked, newer = compare(x.get("model", ""), x.get("version"))
        s = f"{x['name']} ({x.get('model')}): {x.get('version') or '?'}"
        if x.get("partitions"):
            s += f" [partitions: {', '.join(x['partitions'])}]"
        if newer:
            updates.append(x["name"])
            s += f" → {latest} available (known-latest table {checked})"
        elif latest:
            s += f" (current; checked {checked})"
        else:
            s += " (latest not tracked)"
        return s

    for x in d.get("netgear", []):
        lines.append(device_line(x))
    lines.append(device_line(d.get("aruba_iap", {"name": "aruba-iap", "error": "not run"})))
    lines.append(device_line(d.get("aruba_s2500", {"name": "aruba-s2500", "error": "not run"})))
    p = d.get("router", {})
    if p:
        lines.append(f"router {p.get('baseboard-product-name') or '?'}: BIOS {p.get('bios-version') or '?'} "
                     f"({p.get('bios-release-date') or '?'}), BMC {p.get('bmc_firmware') or '?'} (latest not tracked)")
    groups: "OrderedDict[Tuple[str, str], int]" = OrderedDict()
    for dr in sorted(d.get("drives", []), key=lambda x: (x.get("model", ""), x.get("firmware", ""))):
        k = (dr.get("model", "?"), dr.get("firmware", "?"))
        groups[k] = groups.get(k, 0) + 1
    if groups:
        lines.append("drives: " + "; ".join(f"{n}x {m} fw {f}" for (m, f), n in groups.items()))
    summary_bits = []
    if updates:
        summary_bits.append(f"{len(updates)} with newer firmware: {', '.join(updates)}")
    else:
        summary_bits.append("no newer firmware known")
    inv = []
    for key, label in (("aruba_iap", "IAP"), ("aruba_s2500", "S2500")):
        x = d.get(key, {})
        if x.get("version"):
            inv.append(f"{label} {x['version']}")
    if p.get("bios-version"):
        inv.append(f"BIOS {p['bios-version']}")
    if inv:
        summary_bits.append(", ".join(inv))
    if notes:
        summary_bits.append("n/a: " + ", ".join(notes))
    return "; ".join(summary_bits), lines


def main() -> int:
    argparse.ArgumentParser().parse_known_args()
    secrets = common.read_secrets()
    data = {"known_latest": KNOWN_LATEST}
    for key, fn in (("netgear", lambda: netgear(secrets)), ("aruba_iap", lambda: aruba_iap(secrets)),
                    ("aruba_s2500", lambda: aruba_s2500(secrets)), ("router", router_platform),
                    ("drives", router_drives)):
        try:
            data[key] = fn()
        except Exception as e:  # noqa: BLE001
            data[key] = {"name": key, "error": f"{type(e).__name__}: {common.clip(str(e), 60)}"}
        common.log(f"{key}: done")
    summary, lines = build_report(data)
    common.write_report("firmware", "Firmware inventory (monthly)", summary, lines, data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
