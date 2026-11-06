#!/usr/bin/env python3
"""Configuration management for the Aruba S2500 core switch: export, drift, plan, apply.

The switch carries the router's uplinks (port-channel 0), both NETGEAR trunks
(0/1/0, 0/1/1) and its own management link (interface mgmt). A wrong command
there takes the whole LAN down. So `apply` only ever sends an allow-listed set
of edge-port changes and refuses anything else (see ALLOWED / BLOCKED below and
README.md, "Configuration").

Files (next to this script)
  s2500.yml                    desired state: access profiles and edge ports
  s2500.running-config.txt     sanitized `show running-config` (committed baseline)

Subcommands
  check                        offline: desired state, snapshot and template agree
  export [--out FILE]          read the live running-config, sanitize, write it
  plan [--offline]             commands that would make the switch match s2500.yml
  drift [--report FILE]        live vs snapshot and live vs s2500.yml (exit 2 on drift)
  apply [--yes]                send the planned commands, `write memory`, verify

Credentials as in s2500ctl.py (Vault homelab/network/aruba-s2500 or S2500_*).
Exit codes: 0 ok, 1 failed, 2 drift / changes pending, 3 refused, 4 no credentials.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import s2500ctl as ctl  # noqa: E402

DESIRED_FILE = HERE / "s2500.yml"
SNAPSHOT_FILE = HERE / "s2500.running-config.txt"
TEMPLATE_FILE = HERE.parent / "switches" / "aruba-config"
TEMPLATE_SECRET_LINE = "enable secret {{ lookup('env', 'ARUBA_CONFIG_SECRET') }}"

EXIT_DRIFT, EXIT_REFUSED, EXIT_NO_CREDS = 2, 3, 4
REDACTED = "<redacted>"


class Refused(Exception):
    """A change outside the allow-list."""


# --------------------------------------------------------------------------- sanitize

# (pattern, replacement) applied to each line; the snapshot keeps the line so a
# changed user or community still shows up as drift, without the secret.
_REDACT = [
    (re.compile(r"^(\s*enable secret)\s+\S.*$"), r"\1 " + REDACTED),
    (re.compile(r"^(\s*mgmt-user\s+\S+\s+\S+)\s+\S+(.*)$"), r"\1 " + REDACTED + r"\2"),
    (re.compile(r"^(\s*snmp-server community)\s+\S+(.*)$"), r"\1 " + REDACTED + r"\2"),
    (re.compile(r"^(\s*snmp-server user\s+\S+).*$"), r"\1 " + REDACTED),
    (re.compile(r"^(\s*snmp-server host\s+\S+(?:\s+version\s+\S+)?)\s+\S+(.*)$"), r"\1 " + REDACTED + r"\2"),
    (re.compile(r"^(\s*snmp-server engine-id)\s+\S+.*$"), r"\1 " + REDACTED),
    (re.compile(r"^(\s*(?:key|secret|passphrase|psk|wpa-passphrase|shared-secret))\s+\S.*$", re.I), r"\1 " + REDACTED),
    (re.compile(r"^(.*\b(?:password|passwd)\b)\s+\S.*$", re.I), r"\1 " + REDACTED),
    # a revision counter that every `write memory` bumps: not configuration
    (re.compile(r"^(controller config)\s+\d+$"), r"\1 <revision>"),
    (re.compile(r"^(\s*crypto isakmp key)\s+\S+(.*)$"), r"\1 " + REDACTED + r"\2"),
]
_LEFTOVER = [
    re.compile(r"\b[0-9a-fA-F]{24,}\b"),                     # hashes, engine IDs
    re.compile(r"\b(?:secret|password|community|key)\s+(?!" + re.escape(REDACTED) + r")(?!\")\S{6,}", re.I),
]
_LEFTOVER_OK = re.compile(r"^\s*(?:netservice|ip access-list|interface-profile|ssh mgmt-auth|ssh |crypto ipsec transform-set)")


def sanitize(raw: str) -> str:
    """`show running-config` output -> committed snapshot text (no prompts, no secrets)."""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    started = False
    for ln in lines:
        s = ln.rstrip()
        if re.match(r"^\([^)]*\) ?(\([^)]*\) ?)?[#>]", s):     # prompt (with the echoed command)
            continue
        if not started:
            if s.startswith("version ") or s.startswith("# Configuration file") or s == "#":
                started = True
            else:
                continue
        if re.match(r"^\s*Building [Cc]onfiguration", s):
            continue
        for rx, rep in _REDACT:
            s = rx.sub(rep, s)
        out.append(s)
    while out and out[-1] == "":
        out.pop()
    # collapse runs of blank lines (the CLI pads some sections)
    text, prev = [], None
    for s in out:
        if s == "" and prev == "":
            continue
        text.append(s)
        prev = s
    result = "\n".join(text) + "\n"
    assert_sanitized(result)
    return result


def assert_sanitized(text: str) -> None:
    for n, ln in enumerate(text.split("\n"), 1):
        if _LEFTOVER_OK.match(ln):
            continue
        for rx in _LEFTOVER:
            if rx.search(ln):
                raise ctl.S2500Error(f"sanitize: line {n} still looks like it holds a secret or hash; not writing it")


# --------------------------------------------------------------------------- parse

@dataclass
class Block:
    header: str
    body: List[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return " ".join(s.split())


def parse_blocks(text: str) -> List[Block]:
    """Top-level lines; indented lines belong to the block above. `!` closes a block."""
    blocks: List[Block] = []
    cur: Optional[Block] = None
    for ln in text.split("\n"):
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        if ln.strip() == "!":
            cur = None
            continue
        if ln[0] in " \t" and cur is not None:
            cur.body.append(_norm(ln))
            continue
        cur = Block(_norm(ln))
        blocks.append(cur)
    return blocks


def block_map(text: str) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for b in parse_blocks(text):
        out.setdefault(b.header, []).extend(b.body)
    return out


_GE_HDR = re.compile(r'^interface gigabitethernet "?(\d+/\d+/\d+)"?$')
_SP_HDR = re.compile(r'^interface-profile switching-profile "([^"]+)"$')
_POE_HDR = re.compile(r'^interface-profile poe-profile "([^"]+)"$')


def _quoted(line: str, key: str) -> Optional[str]:
    m = re.match(r'^' + re.escape(key) + r'\s+"?([^"]*)"?$', line)
    return m.group(1) if m else None


@dataclass
class Port:
    name: str
    profile: str = "default"
    poe_profile: Optional[str] = None
    description: Optional[str] = None
    other: List[str] = field(default_factory=list)


@dataclass
class Config:
    blocks: Dict[str, List[str]]
    ports: Dict[str, Port]
    switching_profiles: Dict[str, List[str]]
    poe_profiles: Dict[str, List[str]]
    group_poe_profile: Optional[str]

    def port(self, name: str) -> Port:
        return self.ports.get(name) or Port(name)

    def poe_enabled(self, name: str) -> bool:
        prof = self.port(name).poe_profile or self.group_poe_profile
        return bool(prof) and "enable" in self.poe_profiles.get(prof, [])

    def access_vlan(self, profile: str) -> Optional[int]:
        for ln in self.switching_profiles.get(profile, []):
            m = re.match(r"^access-vlan (\d+)$", ln)
            if m:
                return int(m.group(1))
        return None


def parse_config(text: str) -> Config:
    blocks = block_map(text)
    ports: Dict[str, Port] = {}
    sps: Dict[str, List[str]] = {}
    poes: Dict[str, List[str]] = {}
    group = None
    for hdr, body in blocks.items():
        m = _GE_HDR.match(hdr)
        if m:
            p = Port(m.group(1))
            for ln in body:
                v = _quoted(ln, "switching-profile")
                if v is not None:
                    p.profile = v
                    continue
                v = _quoted(ln, "poe-profile")
                if v is not None:
                    p.poe_profile = v
                    continue
                v = _quoted(ln, "description")
                if v is not None:
                    p.description = v
                    continue
                p.other.append(ln)
            ports[p.name] = p
            continue
        m = _SP_HDR.match(hdr)
        if m:
            sps[m.group(1)] = body
            continue
        m = _POE_HDR.match(hdr)
        if m:
            poes[m.group(1)] = body
            continue
        if hdr == 'interface-group gigabitethernet "default"':
            for ln in body:
                v = _quoted(ln, "poe-profile")
                if v is not None:
                    group = v
    return Config(blocks, ports, sps, poes, group)


# --------------------------------------------------------------------------- desired state

EDGE_PORT = re.compile(r"^0/0/(\d|[1-3]\d|4[0-7])$")       # the 48 copper ports
PROFILE_NAME = re.compile(r"^vlan([1-9]\d{0,3})$")
DESC_OK = re.compile(r'^[\w .,()/:+-]{1,60}$')


def load_desired(path: Path = DESIRED_FILE) -> dict:
    import yaml
    d = yaml.safe_load(Path(path).read_text()) or {}
    errs = []
    profiles = d.get("access_profiles") or {}
    for name, vlan in profiles.items():
        m = PROFILE_NAME.match(str(name))
        if not m or int(m.group(1)) != vlan:
            errs.append(f"access_profiles: {name}: must be vlan<N> -> N")
    vlans = set(int(v) for v in (d.get("vlans") or []))
    for name, vlan in profiles.items():
        if vlans and vlan not in vlans:
            errs.append(f"access_profiles: {name}: VLAN {vlan} is not in vlans")
    protected = [str(p) for p in d.get("protected_ports") or []]
    ports = {}
    for name, spec in (d.get("ports") or {}).items():
        name = str(name)
        spec = spec or {}
        if not EDGE_PORT.match(name):
            errs.append(f"ports: {name}: only edge copper ports 0/0/0-0/0/47 are managed")
            continue
        unknown = set(spec) - {"profile", "poe", "description"}
        if unknown:
            errs.append(f"ports: {name}: unknown keys {sorted(unknown)}")
        prof = spec.get("profile", "default")
        if prof != "default" and prof not in profiles:
            errs.append(f"ports: {name}: profile {prof!r} is not in access_profiles")
        if spec.get("poe", True) not in (True, False):
            errs.append(f"ports: {name}: poe must be true or false")
        desc = spec.get("description")
        if desc is not None and not DESC_OK.match(str(desc)):
            errs.append(f"ports: {name}: description must be 1-60 plain characters")
        ports[name] = {"profile": prof, "poe": spec.get("poe", True),
                       "description": None if desc is None else str(desc)}
    for p in protected:
        if not EDGE_PORT.match(p):
            errs.append(f"protected_ports: {p}: not an edge port (uplinks are always protected)")
    if errs:
        raise ctl.S2500Error(f"{path}: " + "; ".join(errs))
    return {"access_profiles": {str(k): int(v) for k, v in profiles.items()}, "ports": ports,
            "protected_ports": protected, "vlans": sorted(vlans)}


# --------------------------------------------------------------------------- plan

@dataclass
class Change:
    target: str            # header of the block the change touches
    lines: List[str]       # sub-mode lines
    why: str

    def commands(self) -> List[str]:
        return [self.target] + self.lines + ["exit"]


def plan(desired: dict, cfg: Config) -> List[Change]:
    changes: List[Change] = []
    for name, vlan in sorted(desired["access_profiles"].items()):
        have = cfg.switching_profiles.get(name)
        if have is None:
            changes.append(Change(f'interface-profile switching-profile "{name}"', [f"access-vlan {vlan}"],
                                  f"new access profile {name} (VLAN {vlan})"))
        elif cfg.access_vlan(name) != vlan or [ln for ln in have if not ln.startswith("access-vlan")]:
            raise Refused(f"switching-profile {name} exists with different settings ({have}); "
                          "existing profiles are not changed by this tool")
    for n in range(48):
        name = f"0/0/{n}"
        want = desired["ports"].get(name, {"profile": "default", "poe": True, "description": None})
        have = cfg.port(name)
        lines, why = [], []
        if have.profile != want["profile"]:
            lines.append(f'switching-profile "{want["profile"]}"' if want["profile"] != "default"
                         else "no switching-profile")
            why.append(f"profile {have.profile} -> {want['profile']}")
        if cfg.poe_enabled(name) != want["poe"]:
            if not want["poe"]:
                raise Refused(f"{name}: turning PoE off is not supported by apply (no PoE-off profile); "
                              "do it by hand and record it here")
            if "enable" not in cfg.poe_profiles.get("poe-factory-initial", []):
                raise Refused('poe-profile "poe-factory-initial" is missing or does not enable PoE')
            lines.append('poe-profile "poe-factory-initial"')
            why.append("PoE off -> on")
        if (have.description or None) != want["description"]:
            lines.append(f'description "{want["description"]}"' if want["description"] else "no description")
            why.append(f"description {have.description!r} -> {want['description']!r}")
        if lines:
            if name in desired["protected_ports"]:
                raise Refused(f"{name} is a protected port ({'; '.join(why)}); change it by hand")
            changes.append(Change(f'interface gigabitethernet {name}', lines, "; ".join(why)))
    for c in changes:
        check_allowed(c.commands())
    return changes


# --------------------------------------------------------------------------- allow-list

# Anything matching BLOCKED is refused wherever it appears, whatever the plan says.
BLOCKED = [re.compile(p, re.I) for p in (
    r"port-channel", r"\b0/1/\d", r"^interface mgmt", r"^interface vlan", r"^vlan\b", r"\bvlan-profile\b",
    r"lacp", r"trunk", r"native-vlan", r"spanning-tree|mstp|pvst",
    r"mgmt-user", r"^aaa\b", r"user-role", r"^enable\b", r"secret", r"password",
    r"^snmp", r"^logging", r"^ntp", r"^ip ", r"^ip-profile", r"^hostname", r"^clock",
    r"firmware", r"\bboot\b", r"\bcopy\b", r"reload", r"write\s+erase", r"erase", r"reset", r"^write",
    r"shutdown", r"interface-group", r"stack", r"^crypto", r"^ssh", r"web-server", r"^service",
)]

# Every command must match one of these, in a context that allows it.
_TOP_ALLOWED = [
    (re.compile(r'^interface gigabitethernet "?0/0/(\d+)"?$'), "port"),
    (re.compile(r'^interface-profile switching-profile "(vlan\d+)"$'), "profile"),
]
_PORT_LINES = [re.compile(p) for p in (
    r'^switching-profile "(default|vlan\d+)"$', r"^no switching-profile$",
    r'^poe-profile "poe-factory-initial"$', r"^no poe-profile$",
    r'^description "[\w .,()/:+-]{1,60}"$', r"^no description$",
)]
_PROFILE_LINES = [re.compile(r"^access-vlan \d+$")]


def check_allowed(commands: List[str], protected: Optional[List[str]] = None) -> None:
    """Raise Refused unless every command is an allow-listed edge-port change."""
    ctx = None
    for raw in commands:
        c = _norm(raw)
        if c in ("exit", "!", "end"):
            ctx = None
            continue
        # A description may contain any word ("trunk", "boot"...): it is checked on its own.
        if ctx == "port" and c.startswith('description "'):
            if not _PORT_LINES[4].match(c):
                raise Refused(f"description not allow-listed: {c!r}")
            continue
        for rx in BLOCKED:
            if rx.search(c):
                raise Refused(f"blocked command: {c!r}")
        if ctx is None:
            for rx, kind in _TOP_ALLOWED:
                m = rx.match(c)
                if m:
                    if kind == "port":
                        n = int(m.group(1))
                        if n > 47:
                            raise Refused(f"not an edge port: {c!r}")
                        if protected and f"0/0/{n}" in protected:
                            raise Refused(f"protected port: {c!r}")
                    ctx = kind
                    break
            else:
                raise Refused(f"not allow-listed: {c!r}")
            continue
        allowed = _PORT_LINES if ctx == "port" else _PROFILE_LINES
        if not any(rx.match(c) for rx in allowed):
            raise Refused(f"not allow-listed inside {ctx}: {c!r}")


def changed_blocks(before: str, after: str) -> List[str]:
    """Headers whose block differs between two sanitized configs."""
    a, b = block_map(before), block_map(after)
    return sorted(h for h in set(a) | set(b) if a.get(h) != b.get(h))


def verify_scope(before: str, after: str, changes: List[Change]) -> List[str]:
    """Blocks that changed but were not planned (empty = the apply stayed in scope)."""
    want = set()
    for c in changes:
        m = re.match(r"^interface gigabitethernet (\S+)$", c.target)
        want.add(f'interface gigabitethernet "{m.group(1)}"' if m else c.target)
    return [h for h in changed_blocks(before, after) if h not in want]


# --------------------------------------------------------------------------- template

def template_from_snapshot(snapshot: str) -> str:
    """The Ansible template networking/switches/aruba-config: the snapshot, with the
    enable secret from the environment and the redacted lines left out."""
    out = []
    for ln in snapshot.split("\n"):
        if ln.strip().startswith("enable secret"):
            out.append(TEMPLATE_SECRET_LINE)
            continue
        if REDACTED in ln or "<revision>" in ln:
            continue
        out.append(ln)
    return "\n".join(out)


def check_offline(desired_path=DESIRED_FILE, snapshot_path=SNAPSHOT_FILE, template_path=TEMPLATE_FILE) -> List[str]:
    problems = []
    desired = load_desired(desired_path)
    snap = Path(snapshot_path).read_text()
    try:
        assert_sanitized(snap)
    except ctl.S2500Error as e:
        problems.append(f"{snapshot_path.name}: {e}")
    cfg = parse_config(snap)
    try:
        pending = plan(desired, cfg)
        for c in pending:
            problems.append(f"{snapshot_path.name} differs from {desired_path.name}: {c.target}: {c.why}")
    except Refused as e:
        problems.append(f"plan refused: {e}")
    if Path(template_path).read_text() != template_from_snapshot(snap):
        problems.append(f"{template_path} is not the snapshot (run `s2500cfg.py render-template`)")
    return problems


# --------------------------------------------------------------------------- live

def read_live(sess) -> str:
    return sanitize(sess.cmd("show running-config", timeout=120))


def send_changes(sess, changes: List[Change], protected: List[str], log=print) -> None:
    cmds = [c for ch in changes for c in ch.commands()]
    check_allowed(cmds, protected)
    out = sess.cmd("configure terminal")
    if "(config)" not in out:
        raise ctl.S2500Error("configure terminal was refused")
    try:
        for c in cmds:
            out = sess.cmd(c)
            body = out.split("\n", 1)[-1]
            log(f"   > {c}")
            if ctl.ERROR_RE.search(body):
                raise ctl.S2500Error(f"the switch rejected {c!r}: " + " ".join(body.split())[:200])
    finally:
        sess.cmd("end")


def unified(a: str, b: str, an: str, bn: str) -> str:
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), an, bn, n=1))


def cmd_check(args) -> int:
    problems = check_offline()
    for p in problems:
        print(f"!! {p}")
    print("== OK" if not problems else f"== {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_render_template(args) -> int:
    TEMPLATE_FILE.write_text(template_from_snapshot(SNAPSHOT_FILE.read_text()))
    print(f"wrote {TEMPLATE_FILE}")
    return 0


def _creds_or_exit():
    creds = ctl.credentials()
    if not creds:
        print(ctl.NO_CREDS)
    return creds


def cmd_export(args) -> int:
    creds = _creds_or_exit()
    if not creds:
        return EXIT_NO_CREDS
    sess = ctl.connect(creds)
    try:
        text = read_live(sess)
    finally:
        sess.close()
    Path(args.out).write_text(text)
    print(f"wrote {args.out} ({len(text.splitlines())} lines)")
    return 0


def _print_plan(changes: List[Change]) -> None:
    if not changes:
        print("== no changes")
        return
    for c in changes:
        print(f"-- {c.why}")
        for ln in c.commands():
            print(("   " if ln not in (c.target,) else "") + ln)


def cmd_plan(args) -> int:
    desired = load_desired()
    if args.offline:
        text = SNAPSHOT_FILE.read_text()
    else:
        creds = _creds_or_exit()
        if not creds:
            return EXIT_NO_CREDS
        sess = ctl.connect(creds)
        try:
            text = read_live(sess)
        finally:
            sess.close()
    try:
        changes = plan(desired, parse_config(text))
    except Refused as e:
        print(f"REFUSED: {e}")
        return EXIT_REFUSED
    _print_plan(changes)
    return EXIT_DRIFT if changes and args.detailed_exitcode else 0


def drift_report(live: str, snapshot: str, desired: dict) -> Tuple[bool, str]:
    out, drift = [], False
    try:
        changes = plan(desired, parse_config(live))
    except Refused as e:
        changes, drift = [], True
        out.append(f"- desired state cannot be planned: {e}")
    if changes:
        drift = True
        out.append("### Live vs `s2500.yml`\n")
        for c in changes:
            out.append(f"- `{c.target}`: {c.why}")
        out.append("")
    diff = unified(snapshot, live, "s2500.running-config.txt", "live")
    if diff:
        drift = True
        out.append("### Live vs `s2500.running-config.txt`\n\n```diff\n" + diff.rstrip("\n") + "\n```")
    if not drift:
        out.append("No drift: the live configuration matches the snapshot and `s2500.yml`.")
    return drift, "## Aruba S2500 configuration drift\n\n" + "\n".join(out) + "\n"


def cmd_drift(args) -> int:
    creds = _creds_or_exit()
    if not creds:
        return EXIT_NO_CREDS
    sess = ctl.connect(creds)
    try:
        live = read_live(sess)
    finally:
        sess.close()
    if args.export:
        Path(args.export).write_text(live)
    drift, report = drift_report(live, SNAPSHOT_FILE.read_text(), load_desired())
    print(report)
    if args.report:
        Path(args.report).write_text(report)
    return EXIT_DRIFT if drift else 0


def cmd_apply(args) -> int:
    if os.environ.get("CI") and not os.environ.get("S2500_APPLY_IN_CI"):
        print("REFUSED: apply does not run in CI (README.md, \"Configuration\")")
        return EXIT_REFUSED
    desired = load_desired()
    creds = _creds_or_exit()
    if not creds:
        return EXIT_NO_CREDS
    sess = ctl.connect(creds)
    try:
        before = read_live(sess)
        try:
            changes = plan(desired, parse_config(before))
        except Refused as e:
            print(f"REFUSED: {e}")
            return EXIT_REFUSED
        _print_plan(changes)
        if not changes:
            return 0
        if not args.yes:
            print("== dry run (pass --yes to send)")
            return EXIT_DRIFT
        send_changes(sess, changes, desired["protected_ports"])
        after = read_live(sess)
        extra = verify_scope(before, after, changes)
        if extra:
            print("!! blocks changed outside the plan (NOT saved; check by hand): " + "; ".join(extra))
            return 1
        left = plan(desired, parse_config(after))
        if left:
            print("!! still differs after apply (NOT saved): " + "; ".join(c.why for c in left))
            return 1
        out = sess.cmd("write memory", timeout=120)
        if not re.search(r"Saved|saved", out):
            print("!! write memory did not confirm: " + " ".join(out.split())[:200])
            return 1
        print("== applied and saved (write memory)")
        if args.export:
            Path(args.export).write_text(after)
    finally:
        sess.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("render-template").set_defaults(fn=cmd_render_template)
    s = sub.add_parser("export")
    s.add_argument("--out", default=str(SNAPSHOT_FILE))
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("plan")
    s.add_argument("--offline", action="store_true", help="plan against the committed snapshot")
    s.add_argument("--detailed-exitcode", action="store_true")
    s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("drift")
    s.add_argument("--report")
    s.add_argument("--export", help="also write the sanitized live config here")
    s.set_defaults(fn=cmd_drift)
    s = sub.add_parser("apply")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--export")
    s.set_defaults(fn=cmd_apply)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return EXIT_REFUSED
    except ctl.S2500Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
