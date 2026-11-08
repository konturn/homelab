#!/usr/bin/env python3
"""Render config files from the fact files in inventory/ (inventory/README.md).

    render.py            write every generated file
    render.py --check    exit 1 and print a diff if a committed file is stale

Generated (committed, so a change shows up in the MR diff and the deploy
path stays a plain copy):
    base/udev/85-my-usb-audio.rules   from inventory/hardware.yml audio_zones
    base/disk/crypttab                from inventory/hardware.yml disks[].luks
                                      (boot, persistent_data, then data and
                                      spare disks by mapper number)
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import facts  # noqa: E402

KEYFILE = "/etc/password_file.txt"


def usb_devpath(ctrl: dict, port: str, interface: int = 0) -> str:
    """Kernel DEVPATH of a USB interface: /devices/<pci>/usb<bus>/<b>-<p>/<b>-<p>.<q>/.../<port>:1.<if>."""
    bus, _, chain = port.partition("-")
    parts = chain.split(".")
    hops = [f"{bus}-{'.'.join(parts[:i])}" for i in range(1, len(parts) + 1)]
    return "/devices/%s/usb%s/%s/%s:1.%d" % (ctrl["pci_path"], bus, "/".join(hops), port, interface)


def render_udev_audio(hw: dict) -> str:
    ctrls = hw["usb_controllers"]
    lines = [
        "# GENERATED from inventory/hardware.yml (audio_zones) by ci/facts/render.py.",
        "# Do not edit: change the data and run `python3 ci/facts/render.py`.",
        "# Names each zone's USB sound card by the USB port it is plugged into.",
        'SUBSYSTEM!="sound", GOTO="my_usb_audio_end"',
        'ACTION!="add", GOTO="my_usb_audio_end"',
        "",
    ]
    for z in hw["audio_zones"]:
        path = usb_devpath(ctrls[z["controller"]], z["port"], int(z.get("interface", 0)))
        lines.append(f'DEVPATH=="{path}/sound/card?", ATTR{{id}}="{z["id"]}"')
    lines += ["", "", "LABEL=\"my_usb_audio_end\"", "", ""]
    return "\n".join(lines) + "\n"


def render_crypttab(hw: dict) -> str:
    order = {"boot": 0, "persistent_data": 1}
    entries = []
    for d in hw["disks"]:
        luks = d.get("luks") or {}
        if d.get("role") == "empty" or not luks.get("crypttab", True):
            continue
        entries.append((order.get(d["role"], 2), facts.mapper_sort_key(luks["mapper"]), luks))
    lines = [
        "# GENERATED from inventory/hardware.yml (disks[].luks) by ci/facts/render.py.",
        "# Do not edit: change the data and run `python3 ci/facts/render.py`.",
    ]
    for _, _, luks in sorted(entries, key=lambda e: (e[0], e[1])):
        lines.append("%s UUID=%s %s %s" % (luks["mapper"], luks["uuid"], luks.get("keyfile", KEYFILE),
                                           luks.get("options", "luks")))
    return "\n".join(lines) + "\n"


def targets(root: Path = facts.ROOT) -> dict[str, str]:
    hw, _deps = facts.load(root)
    return {
        "base/udev/85-my-usb-audio.rules": render_udev_audio(hw),
        "base/disk/crypttab": render_crypttab(hw),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="fail if a committed file differs from its rendering")
    ap.add_argument("--root", type=Path, default=facts.ROOT)
    args = ap.parse_args(argv)
    try:
        out = targets(args.root)
    except facts.FactsError as e:
        print(f"Invalid fact files:\n{e}", file=sys.stderr)
        return 1
    stale = 0
    for rel, text in out.items():
        path = args.root / rel
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if old == text:
            print(f"ok     {rel}")
            continue
        if args.check:
            stale += 1
            print(f"STALE  {rel}: run `python3 ci/facts/render.py` and commit the result")
            sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), text.splitlines(True),
                                                       f"{rel} (committed)", f"{rel} (rendered)"))
        else:
            path.write_text(text, encoding="utf-8")
            print(f"wrote  {rel}")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
