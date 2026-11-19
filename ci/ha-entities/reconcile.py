"""Home Assistant entity registry from the repo (docs/HOMEASSISTANT.md, "Entity registry").

Desired state: docker/homeassistant/entities.yml plus the amps in
inventory/hardware.yml (zone names and entity ids, input names). Only the
entities and amp inputs named there are read or changed.

  reconcile.py plan    print what differs (dry run, the default for MR use)
  reconcile.py apply   make Home Assistant match the repo (main, on change)
  reconcile.py drift   Home Assistant edits since the last apply become an MR
                       that writes them back into the repo; nothing in Home
                       Assistant is changed (daily schedule)

Rules:
  - names: on an entity named after its device (no name of its own and no
    original name, e.g. monoprice zones) the name is the device's
    name_by_user; otherwise the entity's name. Areas follow the name.
  - entity_id renames only with rename_from, only when the old id exists and
    the new one does not. Anything else is reported, never guessed.
  - a disabled_by other than user/none (integration, config entry, device)
    is reported, not changed.
  - every change is printed; values are names and ids, nothing secret.

Talks to Home Assistant over its websocket API (stdlib client below) and the
REST options flow (monoprice sources). Token: HA_TOKEN, or Vault
homelab/docker/homeassistant field token through a JWT login (CI, VAULT_ID_TOKEN).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ENTITIES = "docker/homeassistant/entities.yml"
HARDWARE = "inventory/hardware.yml"
HA_URL = os.environ.get("HA_URL", "https://homeassistant.lab.nkontur.com")
ADOPT_BRANCH = "ha/entities-adopt"
ADOPT_LABEL = "ha-entities-adopt"
FIELDS = {"name", "area", "disabled", "rename_from"}


# ---------------------------------------------------------------------------
# desired state
# ---------------------------------------------------------------------------

def load_yaml(root: Path, rel: str) -> dict:
    return yaml.safe_load((root / rel).read_text(encoding="utf-8")) or {}


def desired(root: Path = ROOT) -> tuple[dict, dict]:
    """({entity_id: {name?, area?, disabled?, rename_from?, source}},
    {ha_port: {input number: name}}). `source` says which file a field came
    from, for the drift MR."""
    ents: dict[str, dict] = {}
    inputs: dict[str, dict[int, str]] = {}
    hw = load_yaml(root, HARDWARE)
    cfg = load_yaml(root, ENTITIES)
    amps = hw.get("amps") or {}
    for amp_name, amp in amps.items():
        if amp.get("connected", True) is False:
            continue
        for _zone, z in (amp.get("zones") or {}).items():
            if z.get("entity"):
                ents[z["entity"]] = {"name": z["name"], "source": {"name": HARDWARE}}
        port = amp.get("ha_port")
        for n, inp in (amp.get("inputs") or {}).items():
            inputs.setdefault(port, {})[int(n)] = inp["label"]
    for eid, spec in (cfg.get("entities") or {}).items():
        extra = set(spec or {}) - FIELDS
        if extra:
            # e.g. an unquoted comma in a flow mapping: {name: A (b, c)}
            raise ValueError(f"{ENTITIES}: {eid}: unknown field(s) {sorted(extra)}; quote names with commas")
        e = ents.setdefault(eid, {"source": {}})
        for k in sorted(FIELDS):
            if k in (spec or {}):
                e[k] = spec[k]
                e["source"][k] = ENTITIES
    for amp_name, extra in (cfg.get("amp_inputs") or {}).items():
        port = (amps.get(amp_name) or {}).get("ha_port")
        if not port:
            raise ValueError(f"{ENTITIES}: amp_inputs.{amp_name}: no such connected amp in {HARDWARE}")
        for n, label in (extra or {}).items():
            inputs.setdefault(port, {})[int(n)] = label
    return ents, inputs


# ---------------------------------------------------------------------------
# planning (pure)
# ---------------------------------------------------------------------------

def name_target(entry: dict) -> str:
    """'device' when the friendly name is the device's, else 'entity'."""
    # config/entity_registry/get does not return has_entity_name; an entity
    # with neither a user name nor an original name shows its device's name.
    if entry.get("name") or entry.get("original_name") or not entry.get("device_id"):
        return "entity"
    return "device"


def plan(want: dict, registry: dict, devices: dict, areas: set) -> tuple[list[dict], list[str]]:
    """Changes needed to make Home Assistant match `want`, and problems to
    report. registry: {entity_id: entity registry entry}; devices: {id: device}."""
    changes: list[dict] = []
    problems: list[str] = []
    for eid, w in sorted(want.items()):
        entry = registry.get(eid)
        if entry is None:
            old = w.get("rename_from")
            if old and old in registry:
                changes.append({"op": "rename", "entity_id": old, "new_entity_id": eid})
                entry = dict(registry[old], entity_id=eid)
            else:
                problems.append(f"{eid}: not in Home Assistant"
                                + (f" (rename_from {old} not there either)" if old else ""))
                continue
        elif w.get("rename_from") and w["rename_from"] in registry:
            problems.append(f"{eid}: both it and rename_from {w['rename_from']} exist; not renaming")
        target = name_target(entry)
        dev = devices.get(entry.get("device_id") or "") if target == "device" else None
        if target == "device" and dev is None:
            problems.append(f"{eid}: named after a device that is not in the device registry")
            continue
        if "name" in w:
            have = dev.get("name_by_user") if dev else entry.get("name")
            if have != w["name"]:
                if dev:
                    changes.append({"op": "device", "entity_id": eid, "device_id": dev["id"],
                                    "field": "name_by_user", "from": have, "to": w["name"]})
                else:
                    changes.append({"op": "entity", "entity_id": eid, "field": "name",
                                    "from": have, "to": w["name"]})
        if "area" in w:
            if w["area"] is not None and w["area"] not in areas:
                problems.append(f"{eid}: area {w['area']!r} does not exist in Home Assistant")
            else:
                if dev:
                    have = dev.get("area_id")
                    if have != w["area"]:
                        changes.append({"op": "device", "entity_id": eid, "device_id": dev["id"],
                                        "field": "area_id", "from": have, "to": w["area"]})
                    if entry.get("area_id") not in (None, w["area"]):
                        problems.append(f"{eid}: entity-level area {entry['area_id']!r} overrides the device's")
                else:
                    have = entry.get("area_id")
                    if have != w["area"]:
                        changes.append({"op": "entity", "entity_id": eid, "field": "area_id",
                                        "from": have, "to": w["area"]})
        if "disabled" in w:
            by = entry.get("disabled_by")
            if by not in (None, "user"):
                if bool(w["disabled"]) != True:
                    problems.append(f"{eid}: disabled by {by}, not by a user; not changing it")
            elif bool(w["disabled"]) != (by == "user"):
                changes.append({"op": "entity", "entity_id": eid, "field": "disabled_by",
                                "from": by, "to": "user" if w["disabled"] else None})
    return changes, problems


def plan_inputs(want: dict, current: dict) -> list[dict]:
    """want/current: {ha_port: {n: name}}. Only listed input numbers count."""
    out = []
    for port, names in sorted(want.items()):
        have = current.get(port)
        if have is None:
            continue
        diff = {n: (have.get(n), v) for n, v in sorted(names.items()) if have.get(n) != v}
        if diff:
            out.append({"op": "inputs", "port": port, "diff": diff})
    return out


def describe(c: dict) -> str:
    if c["op"] == "rename":
        return f"{c['entity_id']}: rename to {c['new_entity_id']}"
    if c["op"] == "inputs":
        return f"amp {c['port']}: " + ", ".join(f"input {n} {a!r} -> {b!r}" for n, (a, b) in c["diff"].items())
    where = "device " if c["op"] == "device" else ""
    return f"{c['entity_id']}: {where}{c['field']} {c['from']!r} -> {c['to']!r}"


# ---------------------------------------------------------------------------
# drift: write Home Assistant's values back into the repo (pure text edits)
# ---------------------------------------------------------------------------

def adopt(texts: dict[str, str], want: dict, changes: list[dict], inputs_src: dict) -> tuple[dict[str, str], list[str]]:
    """Rewrite entities.yml / hardware.yml so they say what Home Assistant
    has (the `from` side of each change). Returns (new texts, notes)."""
    texts = dict(texts)
    notes = []
    for c in changes:
        if c["op"] == "inputs":
            for n, (have, _want) in c["diff"].items():
                src = inputs_src.get((c["port"], n), ENTITIES)
                texts[src], ok = _set_input(texts[src], src, c["port"], n, have, texts[HARDWARE])
                notes.append(("" if ok else "NOT WRITTEN: ") + f"amp {c['port']} input {n}: {have!r}")
            continue
        if c["op"] == "rename":
            continue
        field = {"name_by_user": "name", "name": "name", "area_id": "area", "disabled_by": "disabled"}[c["field"]]
        value = c["from"] if field != "disabled" else (c["from"] == "user")
        if field == "name" and value is None:
            notes.append(f"NOT WRITTEN: {c['entity_id']}: name was cleared in Home Assistant")
            continue
        src = want[c["entity_id"]].get("source", {}).get(field, ENTITIES)
        if src == HARDWARE and field == "name":
            texts[HARDWARE], ok = _set_zone_name(texts[HARDWARE], c["entity_id"], value)
        else:
            texts[ENTITIES], ok = _set_entity_field(texts[ENTITIES], c["entity_id"], field, value)
        notes.append(("" if ok else "NOT WRITTEN: ") + f"{c['entity_id']}: {field} {value!r}")
    return texts, notes


def _flow(d: dict) -> str:
    return yaml.safe_dump(d, default_flow_style=True, sort_keys=False, width=1000).strip()


def _set_entity_field(text: str, eid: str, field: str, value) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    pat = re.compile(r"^(\s+)" + re.escape(eid) + r":\s*(\{.*\})?\s*$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            spec = yaml.safe_load(m.group(2) or "{}") or {}
            spec[field] = value
            lines[i] = f"{m.group(1)}{eid}: {_flow(spec)}\n"
            return "".join(lines), True
    # not listed yet (name from the inventory, area/disabled added in the UI)
    out, inside = [], False
    for line in lines:
        out.append(line)
        if line.rstrip() == "entities:":
            inside = True
            out.append(f"  {eid}: {_flow({field: value})}\n")
    return ("".join(out), True) if inside else (text, False)


def _set_zone_name(text: str, eid: str, name: str) -> tuple[str, bool]:
    pat = re.compile(r"^(\s+\d+: \{name: )(.*?)(, entity: " + re.escape(eid) + r"\}\s*)$", re.M)
    new, n = pat.subn(lambda m: m.group(1) + _flow({"x": name})[4:-1] + m.group(3), text)
    return (new, True) if n == 1 else (text, False)


def _set_input(text: str, src: str, port: str, n: int, label, hardware_text: str) -> tuple[str, bool]:
    if label is None:
        return text, False
    if src == HARDWARE:
        # the input line inside the amp block whose ha_port is `port`
        blocks = re.split(r"(?m)^(  [A-Z]\w*:\n)", text)
        for i in range(1, len(blocks), 2):
            body = blocks[i + 1]
            if re.search(r"(?m)^    ha_port: " + re.escape(port) + r"\s*$", body):
                pat = re.compile(r"(?m)^(      " + str(n) + r": \{label: )(.*?)((, stream: [^}]*)?\}\s*)$")
                body2, k = pat.subn(lambda m: m.group(1) + _flow({"x": label})[4:-1] + m.group(3), body)
                if k == 1:
                    blocks[i + 1] = body2
                    return "".join(blocks), True
        return text, False
    amp = _amp_for_port(hardware_text, port)
    if not amp:
        return text, False
    pat = re.compile(r"(?m)^(  " + re.escape(amp) + r": )(\{.*\})\s*$")
    m = pat.search(text)
    if not m:
        return text, False
    spec = yaml.safe_load(m.group(2)) or {}
    spec[n] = label
    return text[:m.start()] + m.group(1) + _flow(spec) + text[m.end():], True


def _amp_for_port(hardware_text: str, port: str) -> str | None:
    for name, amp in ((yaml.safe_load(hardware_text) or {}).get("amps") or {}).items():
        if amp.get("ha_port") == port:
            return name
    return None


def inputs_sources(root: Path = ROOT) -> dict:
    """{(ha_port, n): file} for every managed amp input."""
    out = {}
    hw = load_yaml(root, HARDWARE)
    for amp in (hw.get("amps") or {}).values():
        for n in (amp.get("inputs") or {}):
            out[(amp.get("ha_port"), int(n))] = HARDWARE
    for name, extra in (load_yaml(root, ENTITIES).get("amp_inputs") or {}).items():
        port = ((hw.get("amps") or {}).get(name) or {}).get("ha_port")
        for n in (extra or {}):
            out[(port, int(n))] = ENTITIES
    return out


# ---------------------------------------------------------------------------
# Home Assistant: a minimal stdlib websocket client and REST
# ---------------------------------------------------------------------------

class WS:
    def __init__(self, url: str, token: str, timeout: float = 30):
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock = sock
        self.buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET /api/websocket HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket upgrade refused: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        if self.recv().get("type") != "auth_required":
            raise RuntimeError("unexpected websocket greeting")
        self.send({"type": "auth", "access_token": token})
        if self.recv().get("type") != "auth_ok":
            raise RuntimeError("Home Assistant refused the token")
        self.n = 0

    def _read(self, k: int) -> bytes:
        while len(self.buf) < k:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:k], self.buf[k:]
        return out

    def _until(self, marker: bytes) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed during handshake")
            self.buf += chunk
        i = self.buf.index(marker) + len(marker)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def _frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, msg: dict):
        self._frame(0x1, json.dumps(msg).encode())

    def recv(self) -> dict:
        data = b""
        while True:
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                raise RuntimeError("websocket closed by Home Assistant")
            if op == 0x9:
                self._frame(0xA, payload)
                continue
            if op in (0x0, 0x1, 0x2):
                data += payload
                if b1 & 0x80:
                    return json.loads(data)

    def call(self, **msg):
        self.n += 1
        msg["id"] = self.n
        self.send(msg)
        while True:
            r = self.recv()
            if r.get("id") == self.n and r.get("type") == "result":
                if not r.get("success"):
                    raise RuntimeError(f"{msg['type']}: {r.get('error')}")
                return r.get("result")


class HA:
    def __init__(self, url: str, token: str):
        self.url, self.token = url.rstrip("/"), token
        self.ws = WS(self.url, token)

    def rest(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            return json.loads(payload) if payload else None

    def registry(self, eids) -> dict:
        out = {}
        for eid in eids:
            try:
                out[eid] = self.ws.call(type="config/entity_registry/get", entity_id=eid)
            except RuntimeError as e:
                if "not_found" not in str(e) and "Entity not found" not in str(e):
                    raise
        return out

    def devices(self) -> dict:
        return {d["id"]: d for d in self.ws.call(type="config/device_registry/list")}

    def areas(self) -> set:
        return {a["area_id"] for a in self.ws.call(type="config/area_registry/list")}

    def monoprice(self) -> dict:
        """{port (entry title): (entry_id, {n: name})} from each entry's options flow."""
        out = {}
        for e in self.rest("GET", "/api/config/config_entries/entry?domain=monoprice") or []:
            f = self.rest("POST", "/api/config/config_entries/options/flow", {"handler": e["entry_id"]})
            try:
                names = {int(s["name"].split("_")[1]): (s.get("description") or {}).get("suggested_value")
                         for s in f.get("data_schema", []) if s["name"].startswith("source_")}
            finally:
                self.rest("DELETE", "/api/config/config_entries/options/flow/" + f["flow_id"])
            out[e["title"]] = (e["entry_id"], names)
        return out

    def set_inputs(self, entry_id: str, names: dict):
        f = self.rest("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        body = {s["name"]: names.get(int(s["name"].split("_")[1])) for s in f["data_schema"]
                if s["name"].startswith("source_")}
        r = self.rest("POST", "/api/config/config_entries/options/flow/" + f["flow_id"],
                      {k: v for k, v in body.items() if v})
        if (r or {}).get("type") != "create_entry":
            raise RuntimeError(f"options flow for {entry_id}: {r}")

    def apply(self, c: dict):
        if c["op"] == "rename":
            self.ws.call(type="config/entity_registry/update", entity_id=c["entity_id"],
                         new_entity_id=c["new_entity_id"])
        elif c["op"] == "entity":
            self.ws.call(type="config/entity_registry/update", entity_id=c["entity_id"], **{c["field"]: c["to"]})
        elif c["op"] == "device":
            self.ws.call(type="config/device_registry/update", device_id=c["device_id"], **{c["field"]: c["to"]})


def token_from_env() -> str:
    if os.environ.get("HA_TOKEN"):
        return os.environ["HA_TOKEN"]
    addr = os.environ.get("VAULT_ADDR", "https://vault.lab.nkontur.com:8200").rstrip("/")
    ctx = ssl._create_unverified_context() if os.environ.get("VAULT_SKIP_VERIFY") == "true" else None
    vt = os.environ.get("VAULT_TOKEN")
    if not vt:
        jwt = os.environ.get("VAULT_ID_TOKEN")
        if not jwt:
            raise SystemExit("no HA_TOKEN, VAULT_TOKEN or VAULT_ID_TOKEN")
        body = json.dumps({"role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": jwt}).encode()
        with urllib.request.urlopen(urllib.request.Request(addr + "/v1/auth/jwt/login", data=body, method="POST"),
                                    context=ctx, timeout=15) as r:
            vt = json.loads(r.read())["auth"]["client_token"]
    req = urllib.request.Request(addr + "/v1/homelab/data/docker/homeassistant", headers={"X-Vault-Token": vt})
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.loads(r.read())["data"]["data"]["token"]


# ---------------------------------------------------------------------------
# GitLab: the adopt MR (one branch, refreshed in place)
# ---------------------------------------------------------------------------

def adopt_mr(texts: dict[str, str], notes: list[str], job_url: str | None) -> str:
    sys.path.insert(0, str(ROOT / "ci" / "facts"))
    import resolve  # GitLab client of facts:drift
    token = os.environ.get("AUTOMERGE_TOKEN") or os.environ.get("GITLAB_TOKEN")
    if not token:
        return "no GitLab token; MR not opened"
    gl = resolve.GitLab(token)
    branch_exists = gl.req("GET", "projects/{p}/repository/branches/" + urllib.parse.quote(ADOPT_BRANCH, safe=""))
    mrs = gl.req("GET", "projects/{p}/merge_requests",
                 {"state": "opened", "source_branch": ADOPT_BRANCH, "per_page": 5}) or []
    if branch_exists and mrs:
        same = all(gl.req("GET", "projects/{p}/repository/files/%s/raw" % urllib.parse.quote(p, safe=""),
                          {"ref": ADOPT_BRANCH}, raw=True) == t for p, t in texts.items())
        if same:
            return f"adopt MR !{mrs[0]['iid']} already proposes this"
    gl.req("POST", "projects/{p}/repository/commits", body={
        "branch": ADOPT_BRANCH, "start_branch": "main", "force": True,
        "commit_message": "homeassistant: adopt entity registry edits made in the UI\n\n"
                          + "\n".join(f"- {n}" for n in notes),
        "actions": [{"action": "update", "file_path": p, "content": t} for p, t in texts.items()],
    })
    desc = ("ha-entities:drift found entity registry values in Home Assistant that differ from the repo, "
            "most likely edited in the UI. Home Assistant was left as it is; this MR writes its values "
            "into the repo (docs/HOMEASSISTANT.md, \"Entity registry\"). Merge to keep them, or close "
            "and run the apply job to restore the repo's values.\n\n"
            + "\n".join(f"- {n}" for n in notes)
            + (f"\n\nDrift run: {job_url}" if job_url else "")
            + "\n\nOpened by the scheduled ha-entities:drift job. Not auto-merged.")
    if mrs:
        gl.req("PUT", "projects/{p}/merge_requests/%s" % mrs[0]["iid"], body={"description": desc})
        return f"adopt MR !{mrs[0]['iid']} updated"
    users = gl.req("GET", "users", {"username": "root"}) or []
    mr = gl.req("POST", "projects/{p}/merge_requests", body={
        "source_branch": ADOPT_BRANCH, "target_branch": "main", "remove_source_branch": True,
        "title": "homeassistant: adopt entity registry edits made in the UI", "description": desc,
        "labels": ADOPT_LABEL, **({"assignee_id": users[0]["id"]} if users else {}),
    })
    return f"adopt MR !{(mr or {}).get('iid')} opened"


# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["plan", "apply", "drift"], default="plan")
    ap.add_argument("--url", default=HA_URL)
    ap.add_argument("--only", action="append", default=[], help="limit to these entity ids (no inputs)")
    ap.add_argument("--report", help="also write the output here")
    args = ap.parse_args(argv)

    want, want_inputs = desired()
    if args.only:
        want = {k: v for k, v in want.items() if k in args.only}
        want_inputs = {}
    ha = HA(args.url, token_from_env())
    reg = ha.registry(set(want) | {w["rename_from"] for w in want.values() if w.get("rename_from")})
    changes, problems = plan(want, reg, ha.devices(), ha.areas())
    mono = ha.monoprice() if want_inputs else {}
    changes += plan_inputs(want_inputs, {port: names for port, (_eid, names) in mono.items()})
    for port in want_inputs:
        if port not in mono:
            problems.append(f"amp {port}: no monoprice config entry with that port")

    out = [f"{len(want)} entities, {sum(len(v) for v in want_inputs.values())} amp inputs managed; "
           f"{len(changes)} difference(s), {len(problems)} problem(s)"]
    out += [f"  {'apply' if args.mode == 'apply' else 'differs'}: {describe(c)}" for c in changes]
    out += [f"  PROBLEM: {p}" for p in problems]

    if args.mode == "apply":
        for c in changes:
            if c["op"] == "inputs":
                entry_id, names = mono[c["port"]]
                ha.set_inputs(entry_id, {**names, **{n: b for n, (_a, b) in c["diff"].items()}})
            else:
                ha.apply(c)
            print(f"applied: {describe(c)}", flush=True)
        if changes:
            left, _ = plan(want, ha.registry(set(want)), ha.devices(), ha.areas())
            if left:
                out += [f"  STILL DIFFERS: {describe(c)}" for c in left]
                problems.append("apply did not converge")
    elif args.mode == "drift" and changes:
        texts = {p: (ROOT / p).read_text(encoding="utf-8") for p in (ENTITIES, HARDWARE)}
        new, notes = adopt(texts, want, [c for c in changes if c["op"] != "rename"], inputs_sources())
        new = {p: t for p, t in new.items() if t != texts[p]}
        if new:
            if HARDWARE in new:
                new.update(rendered_after(new[HARDWARE]))
            out.append("  " + adopt_mr(new, notes, os.environ.get("CI_JOB_URL")))
        out += [f"  {n}" for n in notes if n.startswith("NOT WRITTEN")]

    text = "\n".join(out)
    print(text)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    return 1 if problems else 0


def rendered_after(hardware_text: str) -> dict[str, str]:
    """Files rendered from inventory/ (ci/facts/render.py), re-rendered with
    the adopted hardware.yml, so the MR's facts:test passes."""
    import shutil
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "repo"
        shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "tmp"))
        (dst / HARDWARE).write_text(hardware_text, encoding="utf-8")
        subprocess.run([sys.executable, "ci/facts/render.py"], cwd=dst, check=True, capture_output=True)
        out = {}
        for p in subprocess.run(["git", "-C", str(ROOT), "ls-files"], check=True, capture_output=True,
                                text=True).stdout.split():
            a, b = ROOT / p, dst / p
            if p != HARDWARE and b.is_file() and a.is_file() and a.read_bytes() != b.read_bytes():
                out[p] = b.read_text(encoding="utf-8")
        return out


if __name__ == "__main__":
    sys.exit(main())
