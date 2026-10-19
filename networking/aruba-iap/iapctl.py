#!/usr/bin/env python3
"""Manage the Aruba Instant (IAP) cluster configuration as code.

The desired state is ``running-config.conf`` in this directory: the
cluster's ``show running-config``, normalised, with secret values replaced
by Vault placeholders.

Subcommands
  export   read the live config and write it (sanitised) to a file or stdout
  plan     show the CLI that ``apply`` would send (live, or offline against a
           second file with --current)
  apply    send only the changed blocks, ``commit apply``, re-export, verify
  drift    compare the live config with the desired file; exit 2 on drift

Credentials (live subcommands only)
  IAP_HOST / IAP_USER / IAP_PASS from the environment, or Vault:
  VAULT_ADDR plus VAULT_TOKEN, or VAULT_ID_TOKEN (GitLab id_token) with
  VAULT_JWT_ROLE (default "ci-deploy"). Secrets are never printed.

Exit codes: 0 ok / no changes, 1 error, 2 drift or pending changes (plan
with --detailed-exitcode, drift), 3 refused by a guard.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DESIRED = os.path.join(HERE, "running-config.conf")
DEFAULT_KNOWN_HOSTS = os.path.join(HERE, "known_hosts")

VAULT_IAP_PATH = "homelab/network/aruba-iap"
VAULT_SSID_PATH = "homelab/network/aruba-iap-ssids"

# --------------------------------------------------------------------------
# Secrets / sanitising
# --------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{\{\s*vault:([\w./-]+)#([\w.-]+)\s*\}\}")


def placeholder(path: str, fld: str) -> str:
    return "{{vault:%s#%s}}" % (path, fld)


def _slug(s: str) -> str:
    return re.sub(r"[^\w.-]+", "_", s.strip('"')).strip("_")


# (line regex, how many leading tokens to keep, vault path, field function)
# The field function gets (match, block_header) and returns the Vault field.
_SECRET_RULES: list[tuple[re.Pattern, str, object]] = [
    # wlan ssid-profile <name> / wpa-passphrase <encrypted>
    (re.compile(r"^(\s*wpa-passphrase\s+)\S+(.*)$"), VAULT_SSID_PATH,
     lambda m, hdr: _slug(hdr.split(None, 2)[2]) if hdr and hdr.startswith("wlan ssid-profile") else "wpa_passphrase"),
    (re.compile(r"^(\s*virtual-controller-key\s+)\S+(.*)$"), VAULT_IAP_PATH,
     lambda m, hdr: "virtual_controller_key"),
    # hash-mgmt-user <user> password hash <hash> [usertype ...]
    (re.compile(r"^(\s*hash-mgmt-user\s+\S+\s+password\s+hash\s+)\S+(.*)$"), VAULT_IAP_PATH,
     lambda m, hdr: "mgmt_user_hash"),
    (re.compile(r"^(\s*mgmt-user\s+\S+\s+)\S+(.*)$"), VAULT_IAP_PATH,
     lambda m, hdr: "mgmt_user_password"),
    # snmp-server community <c>
    (re.compile(r"^(\s*snmp-server\s+community\s+)\S+(.*)$"), VAULT_IAP_PATH,
     lambda m, hdr: "snmp_community"),
    # generic "<something>key|secret|passphrase|password <value>" lines,
    # e.g. RADIUS "key", "shared-key", airwave "shared-key", "mpsk ..."
    (re.compile(r"^(\s*(?:\S*-)?(?:key|secret|passphrase|password|psk|shared-key|community|passwd)\s+)\S+(.*)$", re.I),
     VAULT_IAP_PATH,
     lambda m, hdr: _slug(((hdr.split()[-1] + "_") if hdr else "") + m.group(1).strip())),
]

# Lines that look secret even if no rule above recognises them; their value
# is masked with a generic field so nothing leaks into git.
_FALLBACK_SECRET_RE = re.compile(r"(key|secret|passphrase|password|passwd|psk|community|hash)", re.I)
_NON_SECRET_OK = re.compile(
    r"^\s*(hash-mgmt-password|wpa3-transition-mode-key-mgmt|virtual-controller-key\s+\{\{)", re.I)


def sanitize_line(line: str, header: str | None) -> str:
    if PLACEHOLDER_RE.search(line):
        return line
    for rx, path, fn in _SECRET_RULES:
        m = rx.match(line)
        if m:
            return m.group(1) + placeholder(path, fn(m, header)) + m.group(2)
    toks = line.split()
    if len(toks) >= 2 and _FALLBACK_SECRET_RE.search(toks[0]) and not _NON_SECRET_OK.match(line):
        if True:
            indent = line[: len(line) - len(line.lstrip())]
            fld = _slug(((header.split()[-1] + "_") if header else "") + toks[0])
            return indent + toks[0] + " " + placeholder(VAULT_IAP_PATH, fld)
    return line


def is_secret_line(line: str) -> bool:
    return bool(PLACEHOLDER_RE.search(line))


# --------------------------------------------------------------------------
# Parsing / rendering
# --------------------------------------------------------------------------

@dataclass
class Block:
    header: str
    children: list[str] = field(default_factory=list)

    @property
    def is_block(self) -> bool:
        return bool(self.children)


# Lines that are never part of the managed config.
_IGNORED_TOP = re.compile(r"^(version\s|show running-config|.*#\s*$)")


def clean_cli_output(raw: str) -> str:
    """Strip the echoed command, prompts and CRs from a CLI capture."""
    lines = raw.replace("\r", "").split("\n")
    out = []
    for l in lines:
        if re.match(r"^\S+( \(.*\))? ?#.*$", l):  # prompt (with/without echo)
            continue
        if l.strip() == "show running-config":
            continue
        out.append(l.rstrip())
    return "\n".join(out)


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    cur: Block | None = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            if cur is None:
                raise ValueError("indented line without a header: %r" % line)
            cur.children.append(" " + line.strip())
            continue
        if _IGNORED_TOP.match(line):
            cur = None
            continue
        cur = Block(line.strip())
        blocks.append(cur)
    return blocks


def sanitize(blocks: list[Block]) -> list[Block]:
    out = []
    for b in blocks:
        hdr = sanitize_line(b.header, None)
        out.append(Block(hdr, [sanitize_line(c, b.header) for c in b.children]))
    return out


def render(blocks: list[Block]) -> str:
    out: list[str] = []
    prev_block = None
    for b in blocks:
        if out and (b.is_block or prev_block):
            out.append("")
        out.append(b.header)
        out.extend(b.children)
        prev_block = b.is_block
    return "\n".join(out) + "\n"


HEADER_COMMENT = (
    "# Aruba Instant cluster configuration (desired state).\n"
    "# Generated by iapctl.py export; values of the form {{vault:<path>#<field>}}\n"
    "# are kept in Vault and are not compared or applied unless required.\n"
)


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------

# Top-level commands that take a single value: a changed value is sent as
# the new line, without a "no" for the old one.
SINGLE_VALUED_TOP = {
    "name", "virtual-controller-country", "clock", "rf-band", "blacklist-time",
    "auth-failure-blacklist-time", "enet0-port-profile", "enet1-port-profile",
    "enet2-port-profile", "enet3-port-profile", "enet4-port-profile",
    "virtual-controller-ip", "virtual-controller-vlan", "virtual-controller-dnsip",
    "virtual-controller-netmask", "virtual-controller-gateway", "ntp-server",
    "auth-survivability", "dynamic-cpu-mgmt", "ip-mode", "organization",
    "syslog-server", "loginsession", "hostname",
}
# syslog-level <level> <facility>: one value per facility
TOP_KEYFUNCS = {
    "syslog-level": lambda t: "syslog-level " + t[-1],
}
# Children that may appear several times in a block (set semantics).
MULTI_CHILD = {"rule", "id", "allowed-ap", "ssid", "server", "vlan-id"}
# Blocks whose children are an ordered list.
ORDERED_BLOCK_RE = re.compile(r"^wlan access-rule ")


def top_key(line: str) -> str:
    t = line.split()
    if t[0] in TOP_KEYFUNCS:
        return TOP_KEYFUNCS[t[0]](t)
    if t[0] == "no" and len(t) > 1:
        return t[1] if t[1] in SINGLE_VALUED_TOP else line
    if t[0] in SINGLE_VALUED_TOP:
        return t[0]
    return line


def child_key(line: str) -> str:
    t = line.split()
    if t[0] == "no" and len(t) > 1:
        t = t[1:]
    if t[0] in MULTI_CHILD:
        return " ".join(t)
    return t[0]


def negate(line: str) -> str:
    s = line.strip()
    return s[3:] if s.startswith("no ") else "no " + s


def _same(desired: str, live: str) -> bool:
    if desired == live:
        return True
    # A placeholder matches whatever value is live.
    if is_secret_line(desired):
        d, pieces, last = desired.strip(), [], 0
        for m in PLACEHOLDER_RE.finditer(d):
            pieces += [re.escape(d[last:m.start()]), r"\S+"]
            last = m.end()
        pieces.append(re.escape(d[last:]))
        return re.fullmatch("".join(pieces), live.strip()) is not None
    return False


@dataclass
class Change:
    kind: str           # add-block, remove-block, modify-block, add-line, remove-line, replace-line
    key: str
    commands: list[str]  # CLI lines to send (block header first for blocks)
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_secret(self) -> bool:
        return any(is_secret_line(c) for c in self.commands)


def _index(blocks: list[Block]):
    lines, blks = {}, {}
    order = []
    for b in blocks:
        if b.is_block:
            blks[b.header] = b
            order.append(("b", b.header))
        else:
            k = top_key(sanitize_line(b.header, None))
            lines.setdefault(k, []).append(b.header)
            order.append(("l", k))
    return lines, blks, order


def diff(current: list[Block], desired: list[Block]) -> list[Change]:
    cl, cb, corder = _index(current)
    dl, db, dorder = _index(desired)
    changes: list[Change] = []
    seen = set()

    for kind, key in dorder:
        if (kind, key) in seen:
            continue
        seen.add((kind, key))
        if kind == "l":
            want, have = dl[key], cl.get(key, [])
            add = [l for l in want if not any(_same(l, h) for h in have)]
            rem = [h for h in have if not any(_same(l, h) for l in want)]
            if not add and not rem:
                continue
            if add and rem and key != " ".join(want):  # single-valued replace
                changes.append(Change("replace-line", key, list(add), rem, add))
            else:
                if add:
                    changes.append(Change("add-line", key, list(add), [], add))
                if rem:
                    changes.append(Change("remove-line", key, [negate(r) for r in rem], rem, []))
        else:
            want = db[key]
            have = cb.get(key)
            if have is None and key in cl:  # was a bare line, now a block
                have = Block(key)
            if have is None:
                changes.append(Change("add-block", key, [key] + [c.strip() for c in want.children],
                                      [], [key] + want.children))
                continue
            ch = _block_diff(have, want)
            if ch:
                changes.append(ch)

    for kind, key in corder:
        if (kind, key) in seen:
            continue
        seen.add((kind, key))
        if kind == "l":
            if key in db:
                continue
            rem = cl[key]
            changes.append(Change("remove-line", key, [negate(r) for r in rem], rem, []))
        else:
            if key in dl:
                continue
            changes.append(Change("remove-block", key, ["no " + key], [key] + cb[key].children, []))
    return changes


def _block_diff(have: Block, want: Block) -> Change | None:
    hk: dict[str, list[str]] = {}
    for c in have.children:
        hk.setdefault(child_key(c), []).append(c)
    wk: dict[str, list[str]] = {}
    for c in want.children:
        wk.setdefault(child_key(c), []).append(c)

    cmds: list[str] = []
    warnings: list[str] = []
    for k, wl in wk.items():
        hl = hk.get(k, [])
        for w in wl:
            if any(_same(w, h) for h in hl):
                continue
            cmds.append(w.strip())
    removals = []
    for k, hl in hk.items():
        wl = wk.get(k, [])
        for h in hl:
            if any(_same(w, h) for w in wl):
                continue
            if wl and k.split()[0] not in MULTI_CHILD:
                continue  # replaced by the new value above
            removals.append(negate(h))
    cmds = removals + cmds if ORDERED_BLOCK_RE.match(want.header) else cmds + removals
    if not cmds:
        hs = [c for c in have.children]
        ws = [c for c in want.children]
        if ORDERED_BLOCK_RE.match(want.header) and hs != ws and sorted(hs) == sorted(ws):
            warnings.append("rule order differs; reorder manually")
            return Change("modify-block", want.header, [], have.children, want.children, warnings)
        return None
    if ORDERED_BLOCK_RE.match(want.header):
        rest = [c for c in have.children if negate(c) not in removals]
        rest += [c for c in want.children if c.strip() in cmds]
        if [c.strip() for c in rest] != [c.strip() for c in want.children]:
            warnings.append("resulting rule order differs from the desired file")
    return Change("modify-block", want.header, [want.header] + cmds, have.children, want.children, warnings)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

PROTECTED_PATTERNS = [
    (re.compile(r"^virtual-controller-(ip|ipv6|vlan|dnsip|netmask|gateway|key)\b"), "virtual controller address/VLAN"),
    (re.compile(r"^uplink\b"), "uplink"),
    (re.compile(r"^uplink-vlan\b"), "uplink"),
    (re.compile(r"^enet\d-port-profile\b"), "uplink/wired port assignment"),
    (re.compile(r"^(no )?mesh|^extended-ssid|^ap-mesh|^mesh-"), "mesh / extended-ssid (reboots APs)"),
    (re.compile(r"^(allowed-ap|allow-new-aps)\b"), "cluster membership"),
    (re.compile(r"^cluster-security\b"), "cluster security"),
    (re.compile(r"^(hash-)?mgmt-(user|password)|^hash-mgmt-password|^terminal-access|^restricted-mgmt-access|^allow-rest-api"), "management access"),
    (re.compile(r"^ip-mode\b|^ipv6\b"), "addressing mode"),
]


def protected_reasons(ch: Change, desired: list[Block], current: list[Block]) -> list[str]:
    reasons = set()
    # Wired port profiles assigned to an uplink-capable port are part of the uplink.
    uplink_profiles = set()
    for b in list(current) + list(desired):
        m = re.match(r"^enet0-port-profile\s+(\S+)", b.header)
        if m:
            uplink_profiles.add("wired-port-profile " + m.group(1))
    key = ch.key
    for rx, why in PROTECTED_PATTERNS:
        if rx.search(key) or any(rx.search(c.strip()) for c in ch.commands if ch.kind.endswith("line")):
            reasons.add(why)
    if key in uplink_profiles:
        reasons.add("uplink wired-port profile (%s)" % key.split()[-1])
    # SSID/port changes that move traffic onto the management VLAN.
    for c in ch.commands:
        if re.match(r"^(no )?(native-vlan|uplink-vlan|vlan)\s+4\b", c.strip()) and key.startswith("wired-port-profile"):
            reasons.add("management VLAN on a wired port")
    return sorted(reasons)


# --------------------------------------------------------------------------
# Secrets resolution (apply)
# --------------------------------------------------------------------------

class Secrets:
    def __init__(self, fetch):
        self._fetch = fetch
        self._cache: dict[str, dict] = {}

    def get(self, path: str, fld: str) -> str:
        if path not in self._cache:
            self._cache[path] = self._fetch(path) or {}
        data = self._cache[path]
        if fld not in data or not data[fld]:
            raise KeyError("%s#%s" % (path, fld))
        return data[fld]

    def resolve(self, line: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: self.get(m.group(1), m.group(2)), line)


def _vault_request(url: str, token: str | None = None, data: dict | None = None) -> dict:
    ctx = ssl.create_default_context()
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
        ctx = ssl._create_unverified_context()  # noqa: S323 (matches the other CI jobs)
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 method="POST" if data else "GET")
    if token:
        req.add_header("X-Vault-Token", token)
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.load(r)


def vault_token() -> tuple[str, str] | None:
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        return None
    tok = os.environ.get("VAULT_TOKEN")
    if not tok and os.environ.get("VAULT_ID_TOKEN"):
        resp = _vault_request(addr.rstrip("/") + "/v1/auth/jwt/login", data={
            "role": os.environ.get("VAULT_JWT_ROLE") or os.environ.get("VAULT_ROLE", "ci-deploy"), "jwt": os.environ["VAULT_ID_TOKEN"]})
        tok = resp["auth"]["client_token"]
    return (addr.rstrip("/"), tok) if tok else None


def vault_kv_read(path: str) -> dict | None:
    vt = vault_token()
    if not vt:
        return None
    addr, tok = vt
    mount, _, rest = path.partition("/")
    try:
        return _vault_request("%s/v1/%s/data/%s" % (addr, mount, rest), token=tok)["data"]["data"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def iap_credentials() -> tuple[str, str, str]:
    host, user, pw = (os.environ.get(k) for k in ("IAP_HOST", "IAP_USER", "IAP_PASS"))
    if not (host and user and pw):
        data = vault_kv_read(VAULT_IAP_PATH)
        if not data:
            sys.exit("error: no IAP credentials (IAP_HOST/IAP_USER/IAP_PASS or Vault)")
        host = host or data.get("host")
        user = user or data.get("username")
        pw = pw or data.get("password")
    return host, user, pw


# --------------------------------------------------------------------------
# SSH session
# --------------------------------------------------------------------------

PROMPT_RE = re.compile(r"(?:^|\n)[\w.\-]+(?: \([^)]*\))? ?# ?$")
ERROR_RE = re.compile(r"% ?Parse error|Invalid input|^ERROR|% ?Invalid|% ?Incomplete|not allowed", re.M | re.I)


class Session:
    def __init__(self, host, user, password, known_hosts=DEFAULT_KNOWN_HOSTS, timeout=20):
        import paramiko  # imported lazily so offline use needs no dependency
        self.client = paramiko.SSHClient()
        if known_hosts and os.path.exists(known_hosts):
            self.client.load_host_keys(known_hosts)
            self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            self.client.set_missing_host_key_policy(paramiko.WarningPolicy())
        self.client.connect(host, username=user, password=password, look_for_keys=False,
                            allow_agent=False, timeout=timeout, banner_timeout=timeout)
        self.shell = self.client.invoke_shell(width=511, height=1000)
        self._read(30)
        self.cmd("no paging")

    def _read(self, timeout) -> str:
        buf, end = "", time.time() + timeout
        while time.time() < end:
            if self.shell.recv_ready():
                d = self.shell.recv(65535).decode(errors="replace")
                buf += d
                if "--More--" in d[-200:]:
                    self.shell.send(" ")
                if PROMPT_RE.search(buf.replace("\r", "").rstrip("\n")):
                    time.sleep(0.3)
                    if not self.shell.recv_ready():
                        return buf.replace("\r", "")
            else:
                time.sleep(0.05)
        raise TimeoutError("no prompt after %ss" % timeout)

    def cmd(self, line: str, timeout=120) -> str:
        self.shell.send(line + "\n")
        return self._read(timeout)

    def close(self):
        try:
            self.shell.send("exit\n")
            time.sleep(0.3)
        finally:
            self.client.close()


def live_config(sess: Session) -> list[Block]:
    raw = sess.cmd("show running-config")
    return sanitize(parse(clean_cli_output(raw)))


def push(sess: Session, changes: list[Change], secrets: Secrets, log=print) -> None:
    """Send the changes in one config session and commit them."""
    out = sess.cmd("configure terminal")
    if ERROR_RE.search(out):
        raise RuntimeError("configure terminal failed")
    try:
        for ch in changes:
            if not ch.commands:
                continue
            block = ch.kind.endswith("block") and ch.kind != "remove-block"
            for i, c in enumerate(ch.commands):
                shown = c if not is_secret_line(c) else PLACEHOLDER_RE.sub("<secret>", c)
                log(("  " if block and i else "") + shown)
                out = sess.cmd(secrets.resolve(c) if is_secret_line(c) else c)
                if ERROR_RE.search(out.split("\n", 1)[-1]):
                    raise RuntimeError("device rejected: %s" % shown)
            if block:
                sess.cmd("exit")
    except Exception:
        sess.cmd("end")
        raise
    sess.cmd("end")
    out = sess.cmd("commit apply", timeout=180)
    if ERROR_RE.search(out.split("\n", 1)[-1]):
        raise RuntimeError("commit apply failed")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def load(path: str) -> list[Block]:
    with open(path) as f:
        return parse(f.read())


def format_plan(changes: list[Change], guards: dict[int, list[str]]) -> str:
    if not changes:
        return "No changes."
    out = []
    sym = {"add-block": "+", "remove-block": "-", "modify-block": "~", "add-line": "+",
           "remove-line": "-", "replace-line": "~"}
    for i, ch in enumerate(changes):
        out.append("%s %s" % (sym[ch.kind], ch.key))
        if ch.kind == "modify-block":
            for o in ch.old:
                if o not in ch.new and not any(_same(n, o) for n in ch.new):
                    out.append("    - " + o.strip())
            for n in ch.new:
                if n not in ch.old and not any(_same(n, o) for o in ch.old):
                    out.append("    + " + n.strip())
        elif ch.kind == "replace-line":
            out += ["    - " + o for o in ch.old] + ["    + " + n for n in ch.new]
        out.append("  cli:")
        out += ["    " + c for c in ch.commands] or ["    (none)"]
        for w in ch.warnings:
            out.append("  warning: " + w)
        if guards.get(i):
            out.append("  PROTECTED: " + "; ".join(guards[i]))
    n = len(changes)
    p = sum(1 for g in guards.values() if g)
    out.append("")
    out.append("%d change(s), %d touching protected settings." % (n, p))
    return "\n".join(out)


def compute(current, desired):
    changes = diff(current, desired)
    guards = {i: protected_reasons(c, desired, current) for i, c in enumerate(changes)}
    return changes, guards


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="export the live config (sanitised)")
    e.add_argument("-o", "--out", help="write to this file instead of stdout")
    e.add_argument("--from-file", help="sanitise a saved CLI capture instead of connecting")

    for name in ("plan", "apply", "drift"):
        p = sub.add_parser(name)
        p.add_argument("--desired", default=DEFAULT_DESIRED)
        if name == "plan":
            p.add_argument("--current", help="offline: compare against this file instead of the device")
            p.add_argument("--detailed-exitcode", action="store_true", help="exit 2 when there are changes")
        if name == "apply":
            p.add_argument("--allow-protected", action="store_true",
                           default=os.environ.get("IAP_ALLOW_PROTECTED", "") in ("1", "true", "yes"),
                           help="allow changes to management VLAN, uplink, mesh, VC address, membership")
            p.add_argument("--dry-run", action="store_true", help="connect and plan, send nothing")
    args = ap.parse_args(argv)

    if args.cmd == "export":
        if args.from_file:
            with open(args.from_file) as f:
                blocks = sanitize(parse(clean_cli_output(f.read())))
        else:
            sess = Session(*iap_credentials())
            try:
                blocks = live_config(sess)
            finally:
                sess.close()
        text = HEADER_COMMENT + "\n" + render(blocks)
        if args.out:
            with open(args.out, "w") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
        return 0

    desired = load(args.desired)

    if args.cmd == "plan" and args.current:
        changes, guards = compute(load(args.current), desired)
        print(format_plan(changes, guards))
        return 2 if (changes and args.detailed_exitcode) else 0

    sess = Session(*iap_credentials())
    try:
        current = live_config(sess)
        changes, guards = compute(current, desired)
        print(format_plan(changes, guards))
        if args.cmd == "plan":
            return 2 if (changes and args.detailed_exitcode) else 0
        if args.cmd == "drift":
            if changes:
                print("\nDRIFT: the live configuration differs from %s" % os.path.relpath(args.desired))
                return 2
            print("No drift.")
            return 0
        # apply
        if not changes:
            print("Nothing to apply.")
            return 0
        blocked = [changes[i].key for i, g in guards.items() if g]
        if blocked and not args.allow_protected:
            print("\nREFUSED: %d change(s) touch protected settings: %s\n"
                  "Re-run with --allow-protected (CI: the manual aruba-iap:apply-protected job)."
                  % (len(blocked), ", ".join(blocked)))
            return 3
        unordered = [c.key for c in changes if c.warnings]
        if unordered:
            print("\nREFUSED: ordering cannot be reproduced for: %s" % ", ".join(unordered))
            return 3
        secrets = Secrets(vault_kv_read)
        missing = []
        for c in changes:
            for line in c.commands:
                for m in PLACEHOLDER_RE.finditer(line):
                    try:
                        secrets.get(m.group(1), m.group(2))
                    except KeyError as k:
                        missing.append(str(k))
        if missing:
            print("\nREFUSED: secret(s) not in Vault: %s" % ", ".join(sorted(set(missing))))
            return 3
        if args.dry_run:
            print("\nDry run: nothing sent.")
            return 0
        print("\nApplying:")
        push(sess, changes, secrets)
        print("Committed. Verifying...")
        after = live_config(sess)
        rest, _ = compute(after, desired)
        if rest:
            print("VERIFY FAILED: remaining differences after apply:")
            print(format_plan(rest, {}))
            return 1
        print("Verified: live configuration matches the desired file.")
        return 0
    finally:
        sess.close()


if __name__ == "__main__":
    sys.exit(main())
