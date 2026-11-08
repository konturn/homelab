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


def host_vars(host: str, root: Path = ROOT) -> dict:
    """The vars of one host in ansible/inventory.yml, wherever it is nested."""
    def walk(group):
        group = group or {}
        if host in (group.get("hosts") or {}):
            return (group["hosts"][host] or {})
        for child in (group.get("children") or {}).values():
            found = walk(child)
            if found is not None:
                return found
        return None
    return walk(load_yaml(INVENTORY, root).get("all")) or {}


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
    errors += validate_amps(hw, ids)
    return errors


def validate_amps(hw: dict, stream_ids: set) -> list[str]:
    errors = []
    by_id, ha_ports, entities = set(), set(), set()
    for name, amp in (hw.get("amps") or {}).items():
        where = f"amps.{name}"
        if amp.get("connected", True) is False:
            continue
        for key in ("serial_by_id", "ha_port"):
            if not amp.get(key):
                errors.append(f"{where}: {key} missing")
        if amp.get("serial_by_id") in by_id:
            errors.append(f"{where}: serial_by_id shared with another amp")
        if amp.get("ha_port") in ha_ports:
            errors.append(f"{where}: ha_port shared with another amp")
        by_id.add(amp.get("serial_by_id"))
        ha_ports.add(amp.get("ha_port"))
        inputs = amp.get("inputs") or {}
        if set(inputs) - set(range(1, 7)):
            errors.append(f"{where}: inputs are numbered 1-6")
        for n, inp in inputs.items():
            if inp.get("stream") and inp["stream"] not in stream_ids:
                errors.append(f"{where}.inputs.{n}: stream {inp['stream']} is not an audio_zones id")
        zones = amp.get("zones") or {}
        if set(zones) - set(range(11, 17)):
            errors.append(f"{where}: zones are numbered 11-16 (standalone amp)")
        for n, z in zones.items():
            if not str(z.get("entity", "")).startswith("media_player."):
                errors.append(f"{where}.zones.{n}: entity must be a media_player")
            if z.get("entity") in entities:
                errors.append(f"{where}.zones.{n}: duplicate entity {z.get('entity')}")
            entities.add(z.get("entity"))
    return errors


COMPOSE = "docker/docker-compose.yml"


def compose_services(root: Path = ROOT) -> dict:
    """services of the router's compose file. It is an Ansible template, so
    {{ ... }} becomes the string JINJA and {% ... %} lines are dropped."""
    text = (root / COMPOSE).read_text(encoding="utf-8")
    text = re.sub(r"\{\{.*?\}\}", "JINJA", text)
    text = "\n".join(l for l in text.splitlines() if not l.strip().startswith("{%"))
    return (yaml.safe_load(text) or {}).get("services") or {}


def check_amp_devices(hw: dict, services: dict) -> list[str]:
    """homeassistant's device mappings must be exactly the connected amps'."""
    want = sorted(f"/dev/serial/by-id/{a['serial_by_id']}:{a['ha_port']}"
                  for a in (hw.get("amps") or {}).values() if a.get("connected", True) is not False)
    have = sorted(d for d in (services.get("homeassistant") or {}).get("devices") or []
                  if str(d).startswith("/dev/serial/"))
    if want != have:
        return [f"{COMPOSE} homeassistant devices {have} do not match inventory/hardware.yml amps {want}"]
    return []


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
    variables = inventory_vars(root)
    errors = validate_hardware(hw) + validate_dependencies(deps)
    for name, dev in (hw.get("network_devices") or {}).items():
        for key in ("address_var", "mac_var"):
            if dev.get(key) and dev[key] not in variables:
                errors.append(f"network_devices.{name}.{key}: {dev[key]} is not in {INVENTORY} all.vars")
    errors += check_amp_devices(hw, compose_services(root))
    if errors:
        raise FactsError("\n".join(errors))
    deps = substitute(deps, variables)
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
