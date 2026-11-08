"""Load and validate the repo's fact files (inventory/*.yml).

Shared by render.py (generated config) and drift.py (live comparison).
Needs PyYAML, which every CI image that runs Ansible has.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HARDWARE = "inventory/hardware.yml"
DEPENDENCIES = "inventory/dependencies.yml"
INVENTORY = "ansible/inventory.yml"

ROLES = {"data", "boot", "persistent_data", "retired", "empty"}
KINDS = {"container", "systemd", "mount", "zpool", "device", "endpoint", "host", "process"}
PROBES = {"container", "systemd", "mount", "zpool", "tcp", "dns"}
PORT_RE = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")
VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class FactsError(ValueError):
    pass


def load_yaml(rel: str, root: Path = ROOT):
    with open(root / rel, encoding="utf-8") as f:
        return yaml.safe_load(f)


def inventory_vars(root: Path = ROOT) -> dict:
    """all.vars of ansible/inventory.yml: plain values only (no templates)."""
    inv = load_yaml(INVENTORY, root)
    out = {}
    for k, v in ((inv.get("all") or {}).get("vars") or {}).items():
        if isinstance(v, (str, int, float)) and "{{" not in str(v):
            out[k] = str(v)
    return out


def substitute(value, variables: dict):
    """Replace {{ var }} in strings (recursively) with inventory values."""
    if isinstance(value, str):
        def rep(m):
            if m.group(1) not in variables:
                raise FactsError(f"unknown variable {{{{ {m.group(1)} }}}} (not a plain all.vars value in {INVENTORY})")
            return variables[m.group(1)]
        return VAR_RE.sub(rep, value)
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    return value


def mapper_sort_key(mapper: str):
    m = re.match(r"^(\D*)(\d+)$", mapper)
    return (m.group(1), int(m.group(2))) if m else (mapper, -1)


def validate_hardware(hw: dict) -> list[str]:
    errors = []
    ctrls = hw.get("disk_controllers") or {}
    slots, mappers, uuids = set(), set(), set()
    pools = hw.get("zpools") or {}
    for i, d in enumerate(hw.get("disks") or []):
        where = f"disks[{i}] ({d.get('slot', '?')})"
        slot = d.get("slot", "")
        ctrl, _, local = slot.partition("/")
        if ctrl not in ctrls or not local:
            errors.append(f"{where}: slot must be <controller>/<slot> with a controller from disk_controllers")
        if slot in slots:
            errors.append(f"{where}: duplicate slot")
        slots.add(slot)
        role = d.get("role")
        if role not in ROLES:
            errors.append(f"{where}: role must be one of {sorted(ROLES)}")
        if role == "empty":
            continue
        for key in ("model", "serial_suffix"):
            if not d.get(key):
                errors.append(f"{where}: {key} missing")
        suffix = str(d.get("serial_suffix", ""))
        if len(suffix) > 4:
            errors.append(f"{where}: serial_suffix longer than 4 characters (the repo is public)")
        luks = d.get("luks") or {}
        if not luks.get("mapper"):
            errors.append(f"{where}: luks.mapper missing")
        elif luks["mapper"] in mappers:
            errors.append(f"{where}: duplicate luks.mapper {luks['mapper']}")
        mappers.add(luks.get("mapper"))
        if luks.get("crypttab", True):
            if not re.match(r"^[0-9a-f-]{36}$", str(luks.get("uuid", ""))):
                errors.append(f"{where}: luks.uuid missing or malformed")
            elif luks["uuid"] in uuids:
                errors.append(f"{where}: duplicate luks.uuid")
            uuids.add(luks.get("uuid"))
        zp = d.get("zpool")
        if role == "data":
            if not zp:
                errors.append(f"{where}: role data needs zpool")
            elif zp.get("pool") not in pools or zp.get("vdev") not in (pools[zp["pool"]].get("vdevs") or []):
                errors.append(f"{where}: zpool {zp} is not a declared pool/vdev")
        elif zp:
            errors.append(f"{where}: only role data disks are pool members")
    ctrls_usb = hw.get("usb_controllers") or {}
    ids = set()
    for i, z in enumerate(hw.get("audio_zones") or []):
        where = f"audio_zones[{i}] ({z.get('id', '?')})"
        if not re.match(r"^[A-Za-z0-9_]{1,15}$", str(z.get("id", ""))):
            errors.append(f"{where}: id must be an ALSA card id (letters, digits, _; at most 15)")
        if z.get("id") in ids:
            errors.append(f"{where}: duplicate id")
        ids.add(z.get("id"))
        c = ctrls_usb.get(z.get("controller"))
        m = PORT_RE.match(str(z.get("port", "")))
        if not c:
            errors.append(f"{where}: unknown controller")
        elif not m or int(m.group(1)) != int(c.get("bus", -1)):
            errors.append(f"{where}: port must be <bus>-<port>[.<port>...] on bus {c.get('bus')}")
    return errors


def validate_dependencies(deps: dict) -> list[str]:
    errors = []
    nodes = deps.get("nodes") or {}
    for name, n in nodes.items():
        if n.get("kind") not in KINDS:
            errors.append(f"nodes.{name}: kind must be one of {sorted(KINDS)}")
        for need in n.get("needs") or []:
            if need not in nodes:
                errors.append(f"nodes.{name}: needs unknown node {need}")
        probe = n.get("probe")
        if probe is not None:
            if not isinstance(probe, dict) or len(probe) != 1 or next(iter(probe)) not in PROBES:
                errors.append(f"nodes.{name}: probe must be one of {sorted(PROBES)}")
            elif n.get("host", "router") != "router" and next(iter(probe)) not in ("tcp", "dns"):
                errors.append(f"nodes.{name}: probes run on the router; a {n['host']} node can only use tcp or dns")
    # cycles
    state = {}

    def visit(name, path):
        if state.get(name) == 1:
            errors.append("dependency cycle: " + " -> ".join(path + [name]))
            return
        if state.get(name) == 2:
            return
        state[name] = 1
        for need in nodes.get(name, {}).get("needs") or []:
            if need in nodes:
                visit(need, path + [name])
        state[name] = 2

    for name in nodes:
        visit(name, [])
    return errors


def load(root: Path = ROOT):
    """(hardware, dependencies with {{ vars }} resolved). Raises FactsError."""
    hw = load_yaml(HARDWARE, root)
    deps = load_yaml(DEPENDENCIES, root)
    errors = validate_hardware(hw) + validate_dependencies(deps)
    if errors:
        raise FactsError("\n".join(errors))
    deps = substitute(deps, inventory_vars(root))
    return hw, deps


def dependents(deps: dict, name: str) -> list[str]:
    """Every node that needs `name`, directly or transitively, sorted."""
    nodes = deps.get("nodes") or {}
    out, todo = set(), [name]
    while todo:
        cur = todo.pop()
        for other, n in nodes.items():
            if cur in (n.get("needs") or []) and other not in out:
                out.add(other)
                todo.append(other)
    return sorted(out)
