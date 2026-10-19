#!/usr/bin/env python3
"""Render the weekly OS patch report from per-host facts (runs on the router).

Input: --data DIR with one <host>.json per host, written by
ansible/os-patch-report.yml (os_facts.py output, or {"error": ...}).
Writes os-patches.{md,json}. Report only: nothing is upgraded or rebooted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

# codename -> (end of security support, note). Dates are the end of all free
# security support (Debian LTS / Ubuntu standard support). Update yearly.
EOL: Dict[str, Tuple[str, str]] = {
    "stretch": ("2022-06-30", "Debian 9"),
    "buster": ("2024-06-30", "Debian 10"),
    "bullseye": ("2026-08-31", "Debian 11 (LTS ended)"),
    "bookworm": ("2028-06-30", "Debian 12 (LTS from 2026-06-10)"),
    "trixie": ("2030-06-30", "Debian 13"),
    "bionic": ("2023-05-31", "Ubuntu 18.04"),
    "focal": ("2025-05-31", "Ubuntu 20.04"),
    "jammy": ("2027-06-01", "Ubuntu 22.04"),
    "noble": ("2029-05-31", "Ubuntu 24.04"),
}
# macOS: Apple patches roughly the latest three majors. Majors at or below this are unsupported.
MACOS_LAST_UNSUPPORTED = 13
MACOS_CHECKED = "2026-10-19"
EOL_WARN_DAYS = 180


def eol_status(facts: dict, today: Optional[dt.date] = None) -> Optional[str]:
    today = today or dt.date.today()
    if facts.get("id") == "macos":
        try:
            major = int(str(facts.get("version_id", "")).split(".")[0])
        except ValueError:
            return None
        if major <= MACOS_LAST_UNSUPPORTED:
            return f"EOL (macOS {major} no longer gets security updates)"
        if major == MACOS_LAST_UNSUPPORTED + 1:
            return f"check support: macOS {major} may be out of support (table checked {MACOS_CHECKED})"
        return None
    cn = facts.get("codename", "")
    if cn not in EOL:
        return f"unknown release {cn!r}: add to EOL table" if cn else None
    end = dt.date.fromisoformat(EOL[cn][0])
    if end < today:
        return f"EOL since {end.isoformat()} ({EOL[cn][1]}, {cn})"
    if (end - today).days <= EOL_WARN_DAYS:
        return f"EOL on {end.isoformat()} ({EOL[cn][1]}, {cn})"
    return None


def host_line(name: str, f: dict, today: Optional[dt.date] = None) -> Tuple[str, dict]:
    short = name.split(".")[0]
    if "error" in f:
        return f"{short}: no data ({common.clip(f['error'], 80)})", {"error": True}
    stats = {"pending": 0, "security": 0, "reboot": False, "eol": eol_status(f, today)}
    bits = []
    if f.get("id") == "macos":
        su = f.get("softwareupdate") or []
        bo = f.get("brew_outdated") or []
        stats["pending"] = len(su) + len(bo)
        stats["security"] = len(su)
        stats["reboot"] = bool(f.get("softwareupdate_restart"))
        bits.append(f"{len(su)} macOS updates" + (f" ({common.join_limited(su, 2)})" if su else ""))
        if f.get("brew_error"):
            bits.append(f"brew: {f['brew_error']}")
        else:
            bits.append(f"{len(bo)} brew outdated" +
                        (f" ({common.join_limited([b['name'] for b in bo], 4)})" if bo else ""))
    else:
        up = f.get("upgradable") or []
        sec = [p for p in up if p.get("security")]
        stats["pending"], stats["security"] = len(up), len(sec)
        bits.append(f"{len(up)} upgradable ({len(sec)} security"
                    + (f": {common.join_limited([p['name'] for p in sec], 3)}" if sec else "") + ")")
        kr, kn = f.get("kernel_running", ""), f.get("kernel_newest", "")
        newer_kernel = kn and kr and kn != kr and common.version_key(kn) > common.version_key(kr)
        if f.get("reboot_required") or newer_kernel:
            stats["reboot"] = True
            why = []
            if newer_kernel:
                why.append(f"kernel {kr} → {kn}")
            if f.get("reboot_required"):
                why.append("reboot-required" + (f" by {common.join_limited(f.get('reboot_pkgs') or [], 2)}"
                                                if f.get("reboot_pkgs") else ""))
            bits.append("REBOOT pending (" + "; ".join(why) + ")")
        if f.get("apt_update_error"):
            bits.append(f"apt update warning: {common.clip(f['apt_update_error'], 60)}")
    if stats["eol"]:
        bits.append(stats["eol"])
    return f"{short} ({f.get('os') or '?'}): " + "; ".join(bits), stats


def build_report(hosts: Dict[str, dict], today: Optional[dt.date] = None):
    lines, totals = [], {"pending": 0, "security": 0}
    reboot, eol, missing = [], [], []
    for name in sorted(hosts):
        line, st = host_line(name, hosts[name], today)
        lines.append(line)
        short = name.split(".")[0]
        if st.get("error"):
            missing.append(short)
            continue
        totals["pending"] += st["pending"]
        totals["security"] += st["security"]
        if st["reboot"]:
            reboot.append(short)
        if st["eol"] and st["eol"].startswith("EOL"):
            eol.append(short)
    s = f"{len(hosts)} hosts: {totals['pending']} pending ({totals['security']} security)"
    if reboot:
        s += f"; reboot: {', '.join(reboot)}"
    if eol:
        s += f"; EOL OS: {', '.join(eol)}"
    if missing:
        s += f"; no data: {', '.join(missing)}"
    return s, lines


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    a = p.parse_args()
    common.read_secrets()
    hosts = {}
    for path in glob.glob(os.path.join(a.data, "*.json")):
        name = os.path.basename(path)[:-5]
        try:
            with open(path) as f:
                hosts[name] = json.load(f)
        except ValueError as e:
            hosts[name] = {"error": f"bad facts JSON: {e}"}
    if not hosts:
        print("no host facts found", file=sys.stderr)
        return 1
    summary, lines = build_report(hosts)
    common.write_report("os-patches", "OS patch status (weekly)", summary, lines, hosts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
