#!/usr/bin/env python3
"""Compare the live router with inventory/hardware.yml and dependencies.yml.

    drift.py [--report FILE] [--local | --snapshot FILE] [--save-snapshot FILE]

Default: runs collect.py on the router over SSH (key ./tmp, pinned host keys,
like the other drift jobs) and compares. --local runs collect.py on this
machine (use it on the router itself). --snapshot compares a saved collector
output (tests, or replaying a run).

Checks:
  disks     every declared slot holds the declared model / serial suffix, its
            LUKS mapper is open, data disks are ONLINE members of the declared
            pool vdev; no undeclared disk in any slot; pools and vdevs ONLINE
  audio     a sound card named after each zone on the zone's port; no USB
            sound card left unnamed
  amps      each connected amp's USB-serial adapter is present
  containers every service in docker/docker-compose.yml (the router's) is
            running and not unhealthy; one-shot init services (no restart
            policy) exited 0. Skipped: services behind a compose profile and
            snapclient_disabled_zones (ansible/inventory.yml)
  services  every node in dependencies.yml with a probe is up (container
            running and not unhealthy, unit active, mount readable, pool
            ONLINE, TCP port open, DNS answering); a down node is reported
            with everything that depends on it

Exit 0: no drift. 2: drift (report lists it). 1: the check could not run.
Items marked NOTE are reported but do not count as drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import facts  # noqa: E402

HERE = Path(__file__).resolve().parent
HOST = os.environ.get("FACTS_HOST", "10.4.0.1")


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def model_matches(declared: str, live: str) -> bool:
    """lsblk truncates models to 16 chars ("WDC WD240KFGX-68", "Samsung SSD 870")."""
    d, l = norm(declared), norm(live)
    return bool(d and l) and (d in l or d.startswith(l))


def probe_spec(deps: dict) -> dict:
    return {"probes": {name: n["probe"] for name, n in (deps.get("nodes") or {}).items() if n.get("probe")}}


def collect_remote(spec: dict) -> dict:
    root = Path(os.environ.get("CI_PROJECT_DIR") or facts.ROOT)
    program = "_SPEC_JSON = %r\n" % json.dumps(spec) + (HERE / "collect.py").read_text()
    ssh = ["ssh", "-i", str(root / "tmp"), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={root / 'ansible' / 'known_hosts'}", "-o", "LogLevel=ERROR",
           f"root@{HOST}", "python3", "-"]
    p = subprocess.run(ssh, input=program, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"collector on {HOST} failed (rc {p.returncode}): {p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


def collect_local(spec: dict) -> dict:
    p = subprocess.run([sys.executable, str(HERE / "collect.py")], input=json.dumps(spec),
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"collector failed (rc {p.returncode}): {p.stderr.strip()[:500]}")
    return json.loads(p.stdout)


class Report:
    def __init__(self):
        self.sections: dict[str, list[tuple[str, str]]] = {}

    def add(self, section: str, level: str, text: str):
        self.sections.setdefault(section, []).append((level, text))

    def drift(self, section, text):
        self.add(section, "DRIFT", text)

    def note(self, section, text):
        self.add(section, "NOTE", text)

    @property
    def drift_count(self):
        return sum(1 for items in self.sections.values() for lvl, _ in items if lvl == "DRIFT")

    def render(self, checked: dict[str, str]) -> str:
        n = self.drift_count
        head = "Facts drift: %s" % ("no drift" if n == 0 else "%d item(s) differ from inventory/*.yml" % n)
        out = [f"## {head}", ""]
        for section, summary in checked.items():
            items = self.sections.get(section, [])
            out.append(f"### {section}: {summary}")
            for lvl, text in items:
                out.append(f"- **{lvl}** {text}")
            if not items:
                out.append("- ok")
            out.append("")
        out.append("Source of truth: inventory/hardware.yml, inventory/dependencies.yml "
                   "(inventory/README.md). Fix the hardware, or update the data in an MR.")
        return "\n".join(out) + "\n"


def suffix_note(d: dict) -> str:
    return f" Known issue: {' '.join(d['known_issue'].split())}" if d.get("known_issue") else ""


def check_disks(hw: dict, live: dict, rep: Report) -> str:
    S = "disks"
    ctrls = hw["disk_controllers"]
    disks_live = live.get("disks")
    pools_live = live.get("zpools")
    if disks_live is None:
        rep.drift(S, "could not read the disks (see collector errors)")
        return "not checked"
    declared_paths = {}
    for d in hw["disks"]:
        ctrl, _, local = d["slot"].partition("/")
        declared_paths[ctrls[ctrl]["by_path"].format(slot=local)] = d
    leaf_seen = set()
    for path, d in declared_paths.items():
        got = disks_live.get(path)
        slot, mapper = d["slot"], (d.get("luks") or {}).get("mapper")
        if d["role"] == "empty":
            if got:
                rep.drift(S, f"{slot}: declared empty, holds {got['model']} ...{got['serial_suffix']} "
                             f"(/dev/{got['dev']}). Declare it in inventory/hardware.yml.")
            continue
        want = f"{d['model']} ...{d['serial_suffix']} ({mapper}, {d['role']})"
        if not got:
            rep.drift(S, f"{slot}: no disk; expected {want}.{suffix_note(d)}")
            continue
        if not model_matches(d["model"], got["model"]) or got["serial_suffix"] != str(d["serial_suffix"]):
            rep.drift(S, f"{slot}: holds {got['model']} ...{got['serial_suffix']} (/dev/{got['dev']}); expected {want}.")
            continue
        if mapper not in got["mappers"]:
            if got["mappers"]:
                rep.note(S, f"{slot} ({d['model']} ...{d['serial_suffix']}): LUKS open as "
                            f"{', '.join(got['mappers'])}, declared {mapper} (crypttab uses {mapper} at boot).")
            else:
                rep.drift(S, f"{slot} ({d['model']} ...{d['serial_suffix']}): LUKS mapper {mapper} not open.{suffix_note(d)}")
        zp = d.get("zpool")
        if zp and pools_live is not None:
            pool = pools_live.get(zp["pool"])
            leaf = (pool or {}).get("leaves", {}).get(mapper)
            leaf_seen.add((zp["pool"], mapper))
            if not pool:
                rep.drift(S, f"{slot}: pool {zp['pool']} not imported.")
            elif not leaf:
                rep.drift(S, f"{slot} ({mapper}): not a member of {zp['pool']}; declared in {zp['vdev']}.{suffix_note(d)}")
            elif leaf["vdev"] != zp["vdev"]:
                rep.drift(S, f"{slot} ({mapper}): in {leaf['vdev']}, declared {zp['vdev']}.")
            elif leaf["state"] != "ONLINE":
                rep.drift(S, f"{slot} ({mapper}, {d['model']} ...{d['serial_suffix']}): {leaf['state']} in "
                             f"{zp['pool']}/{zp['vdev']} (read/write/cksum errors: {leaf['errors']}).{suffix_note(d)}")
        elif d["role"] == "retired" and pools_live:
            for pname, pool in pools_live.items():
                if mapper in pool.get("leaves", {}):
                    rep.drift(S, f"{slot} ({mapper}): declared retired but is a member of {pname}.")
    for path, got in sorted(disks_live.items()):
        if path not in declared_paths:
            rep.drift(S, f"undeclared disk {got['model']} ...{got['serial_suffix']} (/dev/{got['dev']}) at {path}.")
    if pools_live is None:
        rep.drift(S, "could not read zpool status (see collector errors)")
    else:
        for pname, spec in (hw.get("zpools") or {}).items():
            pool = pools_live.get(pname)
            if not pool:
                rep.drift(S, f"pool {pname} not imported.")
                continue
            if pool["state"] != "ONLINE":
                bad = [f"{v} {s}" for v, s in pool["vdevs"].items() if s != "ONLINE"]
                rep.drift(S, f"pool {pname} is {pool['state']}" + (f" ({', '.join(bad)})" if bad else "") + ".")
            for v in spec.get("vdevs") or []:
                if v not in pool["vdevs"]:
                    rep.drift(S, f"pool {pname}: vdev {v} missing.")
            for v in pool["vdevs"]:
                if v not in (spec.get("vdevs") or []):
                    rep.drift(S, f"pool {pname}: undeclared vdev {v}.")
            for leaf in pool["leaves"]:
                if (pname, leaf) not in leaf_seen:
                    rep.drift(S, f"pool {pname}: member {leaf} is not declared in inventory/hardware.yml.")
    n = sum(1 for d in hw["disks"] if d["role"] != "empty")
    return f"{n} declared disks, {len(disks_live)} found"


def check_audio(hw: dict, live: dict, rep: Report) -> str:
    S = "audio zones"
    cards = live.get("sound_cards")
    ports = live.get("usb_ports") or {}
    if cards is None:
        rep.drift(S, "could not read the sound cards (see collector errors)")
        return "not checked"
    ctrls = hw["usb_controllers"]
    zones = {z["id"]: z for z in hw["audio_zones"]}
    by_id = {c["id"]: c for c in cards}
    ok = 0
    for z in hw["audio_zones"]:
        c = by_id.get(z["id"])
        where = f"port {z['port']} [{ctrls[z['controller']]['desc']}]"
        if c and c.get("usb_port") == z["port"]:
            ok += 1
            continue
        if c:
            rep.drift(S, f"{z['id']}: card is on port {c.get('usb_port')}, declared {where}.")
            continue
        here = ports.get(z["port"])
        found = next((x for x in cards if x.get("usb_port") == z["port"]), None)
        if found:
            what = f"card `{found['id']}` is there"
        elif here == "hub":
            what = "a USB hub is there, not a sound card"
        elif here:
            what = "a non-audio USB device is there"
        else:
            what = "nothing is plugged in there"
        rep.drift(S, f"{z['id']}: no card named {z['id']}; expected on {where}, {what}.")
    for c in cards:
        if c.get("usb_port") and c["id"] not in zones:
            rep.drift(S, f"USB sound card `{c['id']}` on port {c['usb_port']} matches no zone (unnamed); "
                         "move it to its zone's port or record the port in audio_zones.")
    return f"{ok}/{len(zones)} zones on their declared ports"


def check_services(deps: dict, live: dict, rep: Report) -> str:
    S = "services and endpoints"
    nodes = deps.get("nodes") or {}
    probes = live.get("probes") or {}
    down = []
    for name, n in nodes.items():
        if not n.get("probe"):
            continue
        res = probes.get(name)
        if res is None:
            rep.drift(S, f"{name}: not probed")
            continue
        if not res["ok"]:
            down.append(name)
            (kind, arg), = n["probe"].items()
            target = arg if isinstance(arg, str) else f"{arg.get('name')} @ {arg.get('server')}"
            affects = facts.dependents(deps, name)
            rep.drift(S, f"{name} ({kind} {target}): {res['detail']}"
                         + (f"; affects {', '.join(affects)}" if affects else "") + "." + suffix_note(n))
    total = sum(1 for n in nodes.values() if n.get("probe"))
    return f"{total - len(down)}/{total} probes up"


def check_amps(hw: dict, live: dict, rep: Report) -> str:
    S = "audio zones"
    serial = live.get("serial_by_id")
    amps = {n: a for n, a in (hw.get("amps") or {}).items() if a.get("connected", True) is not False}
    if serial is None:
        return "not checked"
    ok = 0
    for name, amp in amps.items():
        if amp["serial_by_id"] in serial:
            ok += 1
        else:
            rep.drift(S, f"amp {name}: USB-serial adapter /dev/serial/by-id/{amp['serial_by_id']} not present "
                         f"(Home Assistant {amp['ha_port']}).")
    return f"{ok}/{len(amps)} amps connected"


def expected_containers(root: Path = facts.ROOT) -> dict:
    """{container name: 'running' | 'oneshot'} for the router's compose file."""
    disabled = set(facts.host_vars("router.lab.nkontur.com", root).get("snapclient_disabled_zones") or [])
    out = {}
    for svc, spec in facts.compose_services(root).items():
        if spec.get("profiles"):
            continue
        if svc.startswith("snapclient_") and svc[len("snapclient_"):] in disabled:
            continue
        name = spec.get("container_name") or svc
        out[name] = "running" if spec.get("restart", "no") != "no" else "oneshot"
    return out


def check_containers(expected: dict, live: dict, rep: Report) -> str:
    S = "containers"
    have = live.get("containers")
    if have is None:
        rep.drift(S, "could not list containers (see collector errors)")
        return "not checked"
    bad = 0
    for name, want in sorted(expected.items()):
        c = have.get(name)
        if not c:
            problem = "no such container"
        elif want == "oneshot":
            problem = None if c["state"] == "running" or c.get("exit_code") == 0 else \
                f"one-shot {c['state']}, exit code {c.get('exit_code')}"
        elif c["state"] != "running":
            problem = c["state"] + (f", exit code {c['exit_code']}" if c.get("exit_code") is not None else "")
        elif c.get("health") == "unhealthy":
            problem = "running, unhealthy"
        else:
            problem = None
        if problem:
            bad += 1
            rep.drift(S, f"{name}: {problem} (declared in docker/docker-compose.yml).")
    return f"{len(expected) - bad}/{len(expected)} declared containers up"


def compare(hw: dict, deps: dict, live: dict, expected: dict | None = None) -> tuple[Report, dict]:
    rep = Report()
    audio = check_audio(hw, live, rep)
    checked = {
        "disks": check_disks(hw, live, rep),
        "audio zones": audio + "; " + check_amps(hw, live, rep),
    }
    if expected is not None:
        checked["containers"] = check_containers(expected, live, rep)
    checked["services and endpoints"] = check_services(deps, live, rep)
    for e in live.get("errors") or []:
        section = ("disks" if e.startswith(("disks", "zpools")) else
                   "containers" if e.startswith("containers") else "audio zones")
        rep.drift(section, f"collector error: {e}")
    return rep, checked


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--local", action="store_true", help="collect on this machine")
    src.add_argument("--snapshot", type=Path, help="compare a saved collector output")
    ap.add_argument("--save-snapshot", type=Path, help="write the collector output here")
    ap.add_argument("--report", type=Path, help="also write the report here")
    ap.add_argument("--root", type=Path, default=facts.ROOT)
    args = ap.parse_args(argv)
    try:
        hw, deps = facts.load(args.root)
        expected = expected_containers(args.root)
        spec = probe_spec(deps)
        if args.snapshot:
            live = json.loads(args.snapshot.read_text())
        elif args.local:
            live = collect_local(spec)
        else:
            live = collect_remote(spec)
    except Exception as e:  # noqa: BLE001 - any failure here means the check did not run
        msg = f"## Facts drift: check failed\n\n{type(e).__name__}: {e}\n"
        print(msg)
        if args.report:
            args.report.write_text(msg)
        return 1
    if args.save_snapshot:
        args.save_snapshot.write_text(json.dumps(live, indent=1, sort_keys=True) + "\n")
    rep, checked = compare(hw, deps, live, expected)
    text = rep.render(checked)
    print(text)
    if args.report:
        args.report.write_text(text)
    return 2 if rep.drift_count else 0


if __name__ == "__main__":
    sys.exit(main())
