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
  interfaces every compose interface_name exists in its container, and Home
            Assistant's configured_adapters (zeroconf/HomeKit) equals
            homeassistant_network_adapters (ansible/inventory.yml)
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
import time
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


def ssh_argv(*remote: str) -> list[str]:
    root = Path(os.environ.get("CI_PROJECT_DIR") or facts.ROOT)
    return ["ssh", "-i", str(root / "tmp"), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
           "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
           "-o", f"UserKnownHostsFile={root / 'ansible' / 'known_hosts'}", "-o", "LogLevel=ERROR",
           f"root@{HOST}", *remote]


def collect_remote(spec: dict) -> dict:
    program = "_SPEC_JSON = %r\n" % json.dumps(spec) + (HERE / "collect.py").read_text()
    p = subprocess.run(ssh_argv("python3", "-"), input=program, capture_output=True, text=True, timeout=300)
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
        # every DRIFT item with a stable key and, for software drift, the fix
        self.items: list[dict] = []
        self.key = None  # default key for the items being added (set by the checks)

    def add(self, section: str, level: str, text: str, **meta):
        self.sections.setdefault(section, []).append((level, text))
        if level == "DRIFT":
            self.items.append({"section": section, "text": text,
                               "key": meta.get("key") or self.key or f"{section}:{text}",
                               "action": meta.get("action")})

    def drift(self, section, text, key=None, action=None):
        self.add(section, "DRIFT", text, key=key, action=action)

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
    rep.key = None
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
        rep.key = f"slot:{slot}"
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
            elif d["role"] == "spare":
                if leaf["vdev"] != "spares":
                    rep.drift(S, f"{slot} ({mapper}): declared a spare, is in {leaf['vdev']} of {zp['pool']}.")
                elif leaf["state"] not in ("AVAIL", "INUSE"):
                    rep.drift(S, f"{slot} ({mapper}, spare): {leaf['state']} in {zp['pool']}.{suffix_note(d)}")
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
        rep.key = f"path:{path}"
        if path not in declared_paths:
            rep.drift(S, f"undeclared disk {got['model']} ...{got['serial_suffix']} (/dev/{got['dev']}) at {path}.")
    if pools_live is None:
        rep.drift(S, "could not read zpool status (see collector errors)")
    else:
        for pname, spec in (hw.get("zpools") or {}).items():
            pool = pools_live.get(pname)
            rep.key = f"pool:{pname}"
            if not pool:
                rep.drift(S, f"pool {pname} not imported.")
                continue
            if pool["state"] != "ONLINE":
                bad = [f"{v} {s}" for v, s in pool["vdevs"].items() if s != "ONLINE" and v not in facts.SPECIAL_VDEVS]
                rep.drift(S, f"pool {pname} is {pool['state']}" + (f" ({', '.join(bad)})" if bad else "") + ".")
            for v in spec.get("vdevs") or []:
                if v not in pool["vdevs"]:
                    rep.drift(S, f"pool {pname}: vdev {v} missing.")
            for v in pool["vdevs"]:
                if v not in (spec.get("vdevs") or []) and v not in facts.SPECIAL_VDEVS:
                    rep.drift(S, f"pool {pname}: undeclared vdev {v}.")
            for leaf in pool["leaves"]:
                if (pname, leaf) not in leaf_seen:
                    rep.key = f"mapper:{leaf}"
                    rep.drift(S, f"pool {pname}: member {leaf} is not declared in inventory/hardware.yml.")
    n = sum(1 for d in hw["disks"] if d["role"] != "empty")
    return f"{n} declared disks, {len(disks_live)} found"


def check_audio(hw: dict, live: dict, rep: Report) -> str:
    S = "audio zones"
    rep.key = None
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
        rep.key = f"zone:{z['id']}"
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
        rep.key = f"card:{c.get('usb_port')}"
        if c.get("usb_port") and c["id"] not in zones:
            rep.drift(S, f"USB sound card `{c['id']}` on port {c['usb_port']} matches no zone (unnamed); "
                         "move it to its zone's port or record the port in audio_zones.")
    return f"{ok}/{len(zones)} zones on their declared ports"


def check_services(deps: dict, live: dict, rep: Report) -> str:
    S = "services and endpoints"
    rep.key = None
    nodes = deps.get("nodes") or {}
    probes = live.get("probes") or {}
    down = []
    for name, n in nodes.items():
        if not n.get("probe"):
            continue
        res = probes.get(name)
        rep.key = f"node:{name}"
        if res is None:
            rep.drift(S, f"{name}: not probed")
            continue
        if not res["ok"]:
            down.append(name)
            (kind, arg), = n["probe"].items()
            target = arg if isinstance(arg, str) else f"{arg.get('name')} @ {arg.get('server')}"
            affects = facts.dependents(deps, name)
            action = ({"type": "systemd", "unit": arg} if kind == "systemd" and n.get("host", "router") == "router"
                      else {"type": "container", "name": arg}
                      if kind == "container" and not res["detail"].startswith("running") else None)
            rep.drift(S, f"{name} ({kind} {target}): {res['detail']}"
                         + (f"; affects {', '.join(affects)}" if affects else "") + "." + suffix_note(n),
                      action=action)
    total = sum(1 for n in nodes.values() if n.get("probe"))
    return f"{total - len(down)}/{total} probes up"


def check_amps(hw: dict, live: dict, rep: Report) -> str:
    S = "audio zones"
    rep.key = None
    serial = live.get("serial_by_id")
    amps = {n: a for n, a in (hw.get("amps") or {}).items() if a.get("connected", True) is not False}
    if serial is None:
        return "not checked"
    ok = 0
    for name, amp in amps.items():
        rep.key = f"amp:{name}"
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
    rep.key = None
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
            rep.drift(S, f"{name}: {problem} (declared in docker/docker-compose.yml).", key=f"container:{name}",
                      # only a stopped container is started; an unhealthy one needs a look
                      action={"type": "container", "name": name}
                      if want == "running" and not problem.startswith("running") else None)
    return f"{len(expected) - bad}/{len(expected)} declared containers up"


ROUTER = "router.lab.nkontur.com"


def expected_interfaces(root: Path = facts.ROOT) -> dict:
    """{"containers": {container: {network: interface_name}}, "ha_adapters": [..] | None,
    "ha_core_network": path} from the router's compose file and inventory."""
    containers = {}
    for svc, spec in facts.compose_services(root).items():
        nets = spec.get("networks")
        names = {n: (c or {}).get("interface_name") for n, c in nets.items()} if isinstance(nets, dict) else {}
        names = {n: i for n, i in names.items() if i}
        if names and not spec.get("profiles"):
            containers[spec.get("container_name") or svc] = names
    data = facts.inventory_vars(root).get("docker_persistent_data_path", "/persistent_data/application")
    return {"containers": containers,
            "ha_adapters": facts.host_vars(ROUTER, root).get("homeassistant_network_adapters"),
            "ha_core_network": f"{data}/homeassistant/.storage/core.network"}


def interface_spec(ifaces: dict) -> dict:
    spec = {"interfaces": sorted(ifaces["containers"])}
    if ifaces.get("ha_adapters"):
        spec["ha_core_network"] = ifaces["ha_core_network"]
    return spec


def check_interfaces(ifaces: dict, live: dict, rep: Report) -> str:
    S = "interfaces"
    if "interfaces" not in live:
        rep.note(S, "not collected (snapshot from before this check)")
        return "not checked"
    have = live.get("interfaces") or {}
    bad = 0
    for name, nets in sorted(ifaces["containers"].items()):
        names = have.get(name)
        if names is None:
            bad += 1
            rep.drift(S, f"{name}: not running, interface names not checked.")
            continue
        for net, ifname in sorted(nets.items()):
            if ifname not in names:
                bad += 1
                rep.drift(S, f"{name}: no interface {ifname} for network {net} (has {', '.join(names)}); "
                             "docker/docker-compose.yml sets interface_name.")
    want = ifaces.get("ha_adapters")
    if want:
        cur = live.get("ha_adapters")
        if sorted(cur or []) != sorted(want):
            bad += 1
            rep.drift(S, f"homeassistant configured_adapters is {cur}, not homeassistant_network_adapters "
                         f"{want} (ansible/inventory.yml): zeroconf/HomeKit on the wrong networks. A deploy resets it.")
    total = sum(len(n) for n in ifaces["containers"].values()) + (1 if want else 0)
    return f"{total - bad}/{total} interface names and adapter selections as declared"


def compare(hw: dict, deps: dict, live: dict, expected: dict | None = None,
            ifaces: dict | None = None) -> tuple[Report, dict]:
    rep = Report()
    audio = check_audio(hw, live, rep)
    checked = {
        "disks": check_disks(hw, live, rep),
        "audio zones": audio + "; " + check_amps(hw, live, rep),
    }
    if expected is not None:
        checked["containers"] = check_containers(expected, live, rep)
    if ifaces is not None:
        checked["interfaces"] = check_interfaces(ifaces, live, rep)
    checked["services and endpoints"] = check_services(deps, live, rep)
    rep.key = None
    for e in live.get("errors") or []:
        section = ("disks" if e.startswith(("disks", "zpools")) else
                   "containers" if e.startswith("containers") else
                   "interfaces" if e.startswith(("interfaces", "ha_adapters")) else "audio zones")
        rep.drift(section, f"collector error: {e}")
    return rep, checked


def run_on_router(cmd: str, local: bool) -> tuple[int, str]:
    argv = ["sh", "-c", cmd] if local else ssh_argv(cmd)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return p.returncode, (p.stdout + p.stderr).strip()[-400:]


def collect(args, spec):
    if args.snapshot:
        return json.loads(args.snapshot.read_text())
    return collect_local(spec) if args.local else collect_remote(spec)


def resolve_drift(args, hw, deps, expected, spec, live, rep, checked, ifaces=None) -> tuple[str, int]:
    """Fix, record, report (resolve.py). Returns (report text, exit code)."""
    import render  # noqa: PLC0415
    import resolve  # noqa: PLC0415

    root, today = args.root, resolve.today()
    lines = []
    # 1. software drift: fix in place, then look again
    services = resolve.container_services(root)
    tried = {}
    for it in rep.items:
        if not it["action"]:
            continue
        cmd, why = resolve.remediation_command(it["action"], services)
        if not cmd:
            tried[it["key"]] = f"not fixed automatically: {why}"
            continue
        if args.dry_run:
            lines.append(f"- would run `{cmd}` for {it['key']}")
            tried[it["key"]] = f"dry run: would run `{cmd}`"
            continue
        rc, out = run_on_router(cmd, args.local)
        lines.append(f"- `{cmd}` -> rc {rc}" + (f": {out[-200:]}" if rc else ""))
        tried[it["key"]] = f"auto-fix `{cmd}` " + ("ran, still failing" if rc == 0 else f"failed (rc {rc}): {out[-200:]}")
    if any(not v.startswith(("not fixed", "dry run")) for v in tried.values()):
        time.sleep(args.settle)
        live = collect(args, spec)
        rep, checked = compare(hw, deps, live, expected, ifaces)
    fixed = [k for k, v in tried.items() if not v.startswith(("not fixed", "dry run"))
             and k not in {i["key"] for i in rep.items}]
    # 2. physical changes that explain themselves: the record MR
    new_disks, changes, covered, extra = resolve.propose_disks(hw, live, today)
    record = {}
    if new_disks is not None:
        path = root / facts.HARDWARE
        before = path.read_text(encoding="utf-8")
        try:
            path.write_text(resolve.rewrite_hardware(before, hw["disks"], new_disks), encoding="utf-8")
            facts.load(root)  # the proposal must validate like any MR
            for rel, text in render.targets(root).items():
                if (root / rel).read_text(encoding="utf-8") != text:
                    record[rel] = text
            record[facts.HARDWARE] = path.read_text(encoding="utf-8")
        except facts.FactsError as e:
            lines.append(f"- could not build the record MR: {e}")
            covered = set()
        finally:
            path.write_text(before, encoding="utf-8")
    # 3. the rest needs hands
    hands = [(it["section"], it["key"], it["text"] + (f" ({tried[it['key']]})" if it["key"] in tried else ""))
             for it in rep.items if it["key"] not in covered]
    hands += [("disks", k, t) for k, t in extra if k not in {h[1] for h in hands}]
    job_url = os.environ.get("CI_JOB_URL")
    out = [rep.render(checked).rstrip("\n"), "", "## What facts:drift did", ""]
    out += [f"- fixed automatically: {k}" for k in fixed] or ["- nothing to fix automatically"]
    out += lines
    out += [f"- record: {c}" for c in changes]
    rc = 0
    if args.dry_run or args.local and not os.environ.get("AUTOMERGE_TOKEN"):
        out.append(f"- (no GitLab writes) {len(hands)} item(s) would go to the living issue")
    else:
        token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITLAB_TOKEN")
        if not token:
            out.append("- AUTOMERGE_TOKEN is not set: no record MR, no living issue")
            rc = 1
        else:
            gl = resolve.GitLab(token)
            try:
                if record:
                    out.append("- " + resolve.record_mr(gl, record, changes, job_url))
                out.append("- living issue: " + resolve.update_issue(gl, hands, today, job_url))
            except Exception as e:  # noqa: BLE001
                out.append(f"- GitLab write failed: {e}")
                rc = 1
    out.append(f"- needs hands: {len(hands)} item(s)")
    return "\n".join(out) + "\n", rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--local", action="store_true", help="collect on this machine")
    src.add_argument("--snapshot", type=Path, help="compare a saved collector output")
    ap.add_argument("--save-snapshot", type=Path, help="write the collector output here")
    ap.add_argument("--report", type=Path, help="also write the report here")
    ap.add_argument("--root", type=Path, default=facts.ROOT)
    ap.add_argument("--resolve", action="store_true",
                    help="fix software drift, open the record MR, rewrite the living issue (resolve.py)")
    ap.add_argument("--dry-run", action="store_true", help="with --resolve: change nothing, say what would happen")
    ap.add_argument("--settle", type=int, default=90, help="seconds to wait after a fix before looking again")
    args = ap.parse_args(argv)
    try:
        hw, deps = facts.load(args.root)
        expected = expected_containers(args.root)
        ifaces = expected_interfaces(args.root)
        spec = {**probe_spec(deps), **interface_spec(ifaces)}
        live = collect(args, spec)
    except Exception as e:  # noqa: BLE001 - any failure here means the check did not run
        msg = f"## Facts drift: check failed\n\n{type(e).__name__}: {e}\n"
        print(msg)
        if args.report:
            args.report.write_text(msg)
        return 1
    if args.save_snapshot:
        args.save_snapshot.write_text(json.dumps(live, indent=1, sort_keys=True) + "\n")
    rep, checked = compare(hw, deps, live, expected, ifaces)
    if args.resolve:
        text, rc = resolve_drift(args, hw, deps, expected, spec, live, rep, checked, ifaces)
    else:
        text, rc = rep.render(checked), (2 if rep.drift_count else 0)
    print(text)
    if args.report:
        args.report.write_text(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
