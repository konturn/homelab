#!/usr/bin/env python3
"""Collect live hardware and service facts on the router, as JSON (read-only).

Run on the router by ci/facts/drift.py over SSH (the source is sent on stdin,
so nothing is installed on the host). Python stdlib only.

    python3 collect.py < spec.json      spec: {"probes": {node: {kind: arg}}}

Nothing here changes the host: it reads /sys, /dev/disk/by-path, lsblk,
`zpool status`, `docker ps`/`docker inspect`, /dev/serial/by-id, `systemctl is-active`, and opens TCP/UDP
connections for the probes.
"""

import json
import os
import random
import re
import socket
import struct
import subprocess
import sys

BY_PATH = "/dev/disk/by-path"
VDEV_RE = re.compile(r"^(raidz\d?|mirror|draid\d?|replacing|spare)(-\d+)?$")


def run(argv, timeout=30):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)


def disks():
    """{by-path name: {dev, model, serial_suffix, mappers: [..]}} for whole disks."""
    rc, out, err = run(["lsblk", "-J", "-o", "NAME,TYPE,MODEL,SERIAL"])
    if rc != 0:
        raise RuntimeError("lsblk failed: " + err)
    info = {}

    def mappers(node):
        found = []
        for c in node.get("children") or []:
            if c.get("type") == "crypt":
                found.append(c["name"])
            found += mappers(c)
        return found

    for b in json.loads(out).get("blockdevices", []):
        if b.get("type") == "disk":
            info[b["name"]] = {"dev": b["name"], "model": (b.get("model") or "").strip(),
                               # last 4 only: the report can end up in a public place
                               "serial_suffix": (b.get("serial") or "").strip()[-4:], "mappers": mappers(b)}
    result = {}
    for name in sorted(os.listdir(BY_PATH)):
        # skip partitions and libata's duplicate per-link names (ata-1.0 next to ata-1)
        if "-part" in name or re.search(r"-ata-\d+\.\d+$", name):
            continue
        dev = os.path.basename(os.path.realpath(os.path.join(BY_PATH, name)))
        if dev in info:
            result[name] = info[dev]
    return result


def zpool_status():
    rc, out, err = run(["zpool", "status"])
    if rc != 0:
        raise RuntimeError("zpool status failed: " + err)
    return parse_zpool_status(out)


def parse_zpool_status(out):
    """{pool: {state, vdevs: {vdev: state}, leaves: {leaf: {vdev, state, errors}}}}."""
    pools, pool, in_config, vdev = {}, None, False, None
    for line in out.splitlines():
        m = re.match(r"^\s*pool: (\S+)", line)
        if m:
            pool = {"state": None, "vdevs": {}, "leaves": {}}
            pools[m.group(1)] = pool
            in_config = False
            continue
        if re.match(r"^\s*config:", line):
            in_config = True
            continue
        if re.match(r"^\s*errors:", line):
            in_config = False
            continue
        if not in_config or pool is None or not line.strip() or line.strip().startswith("NAME"):
            continue
        # Config lines: a tab, then two spaces per level (pool 0, vdev 1, disk 2).
        body = line.lstrip("\t")
        depth = (len(body) - len(body.lstrip(" "))) // 2
        f = body.split()
        name, state, errs = f[0], (f[1] if len(f) > 1 else ""), " ".join(f[2:])
        if depth == 0:
            pool["state"] = state
            vdev = None
        elif VDEV_RE.match(name) or name in ("logs", "cache", "spares", "special", "dedup"):
            if depth == 1:
                vdev = name
                pool["vdevs"][name] = state
            # nested replacing-N / spare-N: keep the top-level vdev
        else:
            pool["leaves"][name] = {"vdev": vdev if depth > 1 else name, "state": state, "errors": errs}
    return pools


def sound_cards():
    cards = []
    base = "/sys/class/sound"
    for name in sorted(os.listdir(base)):
        if not re.match(r"^card\d+$", name):
            continue
        path = os.path.realpath(os.path.join(base, name))
        try:
            with open(os.path.join(path, "id")) as f:
                cid = f.read().strip()
        except OSError:
            cid = ""
        m = re.search(r"/(\d+-[\d.]+):1\.(\d+)/sound/", path)
        cards.append({"card": name, "id": cid, "devpath": path[len("/sys"):] if path.startswith("/sys") else path,
                      "usb_port": m.group(1) if m else None})
    return cards


def usb_ports():
    """{port: 'hub'|'device'} for every USB device below a root hub."""
    out = {}
    base = "/sys/bus/usb/devices"
    for name in os.listdir(base):
        if not re.match(r"^\d+-[\d.]+$", name):
            continue
        try:
            with open(os.path.join(base, name, "bDeviceClass")) as f:
                cls = f.read().strip()
        except OSError:
            cls = ""
        out[name] = "hub" if cls == "09" else "device"
    return out


def containers():
    """{name: {state, exit_code, health}} for every container (docker ps -a)."""
    rc, out, err = run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}"])
    if rc != 0:
        raise RuntimeError("docker ps failed: " + err)
    result = {}
    for line in out.splitlines():
        name, state, status = (line.split("\t") + ["", ""])[:3]
        m = re.search(r"Exited \((-?\d+)\)", status)
        h = re.search(r"\((healthy|unhealthy|health: starting)\)", status)
        result[name] = {"state": state, "exit_code": int(m.group(1)) if m else None,
                        "health": h.group(1) if h else None}
    return result


def serial_by_id():
    """{by-id name: tty} from /dev/serial/by-id."""
    base = "/dev/serial/by-id"
    if not os.path.isdir(base):
        return {}
    return {n: os.path.basename(os.path.realpath(os.path.join(base, n))) for n in sorted(os.listdir(base))}


def mount_source(path):
    rc, out, _ = run(["findmnt", "-n", "-o", "SOURCE", path])
    return out.strip() if rc == 0 else None


def dns_query(server, name, timeout=3.0):
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    q += struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    rid, flags, _qd, an = struct.unpack(">HHHH", data[:8])
    rcode = flags & 0xF
    if rid != qid:
        return False, "mismatched DNS id"
    if rcode != 0 or an == 0:
        return False, "rcode %d, %d answers" % (rcode, an)
    return True, "%d answer(s)" % an


def probe(kind, arg):
    """(ok, detail)."""
    if kind == "container":
        rc, out, err = run(["docker", "inspect", "-f",
                            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}", arg])
        if rc != 0:
            return False, "not found (%s)" % (err.splitlines()[0] if err else rc)
        status, health = out.split()
        if status != "running":
            return False, status
        if health == "unhealthy":
            return False, "running, unhealthy"
        return True, status + ("" if health == "-" else ", " + health)
    if kind == "systemd":
        rc, out, _ = run(["systemctl", "is-active", arg])
        return out.strip() == "active", out.strip() or "rc %d" % rc
    if kind == "mount":
        if not os.path.ismount(arg):
            return False, "not mounted"
        try:
            os.listdir(arg)
        except OSError as e:
            return False, "mounted, unreadable: %s" % e
        return True, "mounted from %s" % mount_source(arg)
    if kind == "zpool":
        rc, out, err = run(["zpool", "list", "-H", "-o", "health", arg])
        return (rc == 0 and out.strip() == "ONLINE"), (out.strip() or err)
    if kind == "tcp":
        host, _, port = arg.rpartition(":")
        try:
            socket.create_connection((host, int(port)), timeout=3).close()
            return True, "connect ok"
        except OSError as e:
            return False, "connect failed: %s" % e
    if kind == "dns":
        try:
            return dns_query(arg["server"], arg["name"])
        except OSError as e:
            return False, "query failed: %s" % e
    return False, "unknown probe kind %s" % kind


def collect(spec):
    out = {"errors": []}
    for key, fn in (("disks", disks), ("zpools", zpool_status), ("sound_cards", sound_cards),
                    ("usb_ports", usb_ports), ("containers", containers), ("serial_by_id", serial_by_id)):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 - reported, the comparison marks the section as unchecked
            out[key] = None
            out["errors"].append("%s: %s" % (key, e))
    out["probes"] = {}
    for node, pr in (spec.get("probes") or {}).items():
        (kind, arg), = pr.items()
        ok, detail = probe(kind, arg)
        out["probes"][node] = {"ok": ok, "detail": detail}
    return out


if __name__ == "__main__":
    _spec = json.loads(globals().get("_SPEC_JSON") or sys.stdin.read() or "{}")
    json.dump(collect(_spec), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
