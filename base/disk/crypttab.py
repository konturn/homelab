#!/usr/bin/env python3
import sys, json, re, subprocess, shlex

KEYFILE_DEFAULT = "/etc/password_file.txt"

SPECIALS = {
    "sda5_crypt": ("none", "luks,discard"),
    "persistent_data": (KEYFILE_DEFAULT, "luks"),
}

DEVN_RE = re.compile(r"^dev(\d+)$")

def load_lsblk_json():
    data = sys.stdin.read()
    if not data.strip():
        # fallback: call lsblk ourselves
        cmd = "lsblk -o name,uuid --json"
        out = subprocess.run(shlex.split(cmd), capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    return json.loads(data)

def traverse(node, nearest_uuid, specials_map, dev_map):
    # Update nearest ancestor uuid if this node has one
    node_uuid = node.get("uuid") or nearest_uuid

    # Process children (names of interest are on children in your tree)
    for child in node.get("children", []) or []:
        name = child.get("name", "")
        # If this child is a special name
        if name in SPECIALS and node_uuid:
            specials_map[name] = node_uuid
        # If this child is devN
        m = DEVN_RE.match(name)
        if m and node_uuid:
            dev_map[int(m.group(1))] = node_uuid
        # Recurse further (handles deep nesting like sdb5 -> sda5_crypt -> ...)
        traverse(child, node_uuid, specials_map, dev_map)

def main():
    lsblk = load_lsblk_json()
    specials_map = {}
    dev_map = {}

    for dev in lsblk.get("blockdevices", []):
        traverse(dev, None, specials_map, dev_map)

    # Emit specials first if present, in deterministic order
    out_lines = []
    if "sda5_crypt" in specials_map:
        out_lines.append(f"sda5_crypt UUID={specials_map['sda5_crypt']} none luks,discard")
    if "persistent_data" in specials_map:
        out_lines.append(f"persistent_data UUID={specials_map['persistent_data']} {KEYFILE_DEFAULT} luks")

    # Then devN in numeric order
    for n in sorted(dev_map):
        out_lines.append(f"dev{n} UUID={dev_map[n]} {KEYFILE_DEFAULT} luks")

    # Print to stdout
    sys.stdout.write("\n".join(out_lines) + ("\n" if out_lines else ""))

if __name__ == "__main__":
    main()

