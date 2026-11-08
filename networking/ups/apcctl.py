#!/usr/bin/env python3
"""APC AP9631 network management card: configuration as code (read-only by default).

The card's whole configuration is its config.ini, which the card serves over
SCP (SSH is enabled on it; FTP is off). This tool downloads it, removes
secrets and volatile values, and compares it with the committed desired
state, apc-config.ini.

  apcctl.py check                       offline: apc-config.ini (if present) has no secrets
  apcctl.py sanitize IN [OUT]           offline: sanitize a config.ini
  apcctl.py export [--write]            download, sanitize, print (or write apc-config.ini)
  apcctl.py drift [--report FILE]       download, sanitize, diff against apc-config.ini;
                                        exit 1 on drift, 0 when equal or when the
                                        credentials or the baseline are not there yet
  apcctl.py pin-hostkey                 record the card's SSH host key in known_hosts
  apcctl.py apply --plan                show what an upload would send (nothing is sent)
  apcctl.py apply --upload --i-understand-this-reconfigures-the-ups
                                        upload the desired state to the card. Never
                                        run by CI; prepared for later, see docs/POWER.md.

Credentials: APC_USERNAME / APC_PASSWORD, or the Vault secret
homelab/infrastructure/ups-card (fields username, password; VAULT_ADDR plus
VAULT_TOKEN or VAULT_ID_TOKEN). The card allows one session at a time and
can lock users out after failed logins, so every command makes exactly one login
attempt (NumberOfPasswordPrompts=1) and never retries.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
DESIRED = HERE / "apc-config.ini"
KNOWN_HOSTS = HERE / "known_hosts"
HOST = os.environ.get("APC_HOST", "10.4.128.8")
VAULT_PATH = "homelab/infrastructure/ups-card"
REDACTED = "<redacted>"

# Keys whose values are secrets (matched against the key name, any section).
SECRET_KEY_RE = re.compile(r"(?i)(pass|phrase|community|secret|key|psk|token|hash|cert)")
# Values that change on their own (clock, uptime, counters, card-generated ids).
VOLATILE_KEYS = {
    ("SystemDate/Time", "Date"), ("SystemDate/Time", "Time"),
}
VOLATILE_KEY_RE = re.compile(r"(?i)(uptime|lastupdate|timestamp)")

SSH_OPTS = [
    # The card's SSH server (cryptlib, AOS 6.8) offers only ssh-rsa host keys
    # and older algorithms.
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group-exchange-sha1",
    "-o", "Ciphers=+aes128-cbc,aes256-cbc",
    "-o", "MACs=+hmac-sha1",
    "-o", "PreferredAuthentications=password,keyboard-interactive",
    "-o", "PubkeyAuthentication=no",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "ConnectTimeout=20",
    "-o", "ServerAliveInterval=10",
    # no connection sharing: root's ssh_config enables ControlMaster, and the
    # card allows one session at a time
    "-o", "ControlMaster=no",
    "-o", "ControlPath=none",
]

# scp -v prints "Sink: C0644 <size> <name>" for the file it receives.
SINK_RE = re.compile(r"Sink: C[0-7]{4} (\d+) ")


class ApcError(Exception):
    pass


class NoCredentials(ApcError):
    pass


# --------------------------------------------------------------------------- pure functions

def sanitize(text: str) -> str:
    """config.ini -> desired-state text: no secrets, no volatile values, no comments.

    Sections and key order are kept as the card writes them, so diffs stay
    readable. Secret values become <redacted>; volatile keys are dropped.
    """
    out: List[str] = [
        "; AP9631 configuration, desired state (networking/ups/apcctl.py export --write).",
        "; Secrets are <redacted>; apply never sends those lines.",
    ]
    section = ""
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            section = m.group(1).strip()
            out.append("")
            out.append(f"[{section}]")
            continue
        if "=" not in line:
            out.append(line)
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if (section, key) in VOLATILE_KEYS or VOLATILE_KEY_RE.search(key):
            continue
        if SECRET_KEY_RE.search(key) and value:
            value = REDACTED
        out.append(f"{key}={value}")
    return "\n".join(out).strip() + "\n"


def unredacted_secrets(text: str) -> List[str]:
    """Lines of a desired-state file that look like secrets left in clear."""
    bad = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith((";", "#", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if SECRET_KEY_RE.search(key) and value.strip() not in ("", REDACTED):
            bad.append(f"line {n}: {key.strip()}")
    return bad


def diff(desired: str, live: str) -> str:
    return "".join(difflib.unified_diff(desired.splitlines(True), live.splitlines(True),
                                        "apc-config.ini (desired)", "card (live, sanitized)"))


def upload_payload(desired: str) -> Tuple[str, int]:
    """Desired state minus comments and every secret-key line, redacted or empty
    (the card keeps values that are absent; an empty one would clear it)."""
    lines, skipped = [], 0
    for raw in desired.splitlines():
        line = raw.strip()
        if line.startswith((";", "#")):
            continue
        if "=" in line and not line.startswith("[") and SECRET_KEY_RE.search(line.partition("=")[0]):
            skipped += 1
            continue
        lines.append(raw)
    return "\r\n".join(lines) + "\r\n", skipped


# --------------------------------------------------------------------------- credentials

def _ssl_ctx() -> ssl.SSLContext:
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context(cafile=os.environ.get("VAULT_CACERT") or None)


def credentials() -> Dict[str, str]:
    user, pw = os.environ.get("APC_USERNAME"), os.environ.get("APC_PASSWORD")
    if user and pw:
        return {"username": user, "password": pw}
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        raise NoCredentials("APC_USERNAME/APC_PASSWORD not set and VAULT_ADDR not set")
    ctx = _ssl_ctx()
    token = os.environ.get("VAULT_TOKEN")
    if not token:
        jwt = os.environ.get("VAULT_ID_TOKEN")
        if not jwt:
            raise NoCredentials("neither VAULT_TOKEN nor VAULT_ID_TOKEN is set")
        body = json.dumps({"role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": jwt}).encode()
        req = urllib.request.Request(f"{addr}/v1/auth/jwt/login", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                token = json.load(r)["auth"]["client_token"]
        except (urllib.error.URLError, KeyError, ValueError) as e:
            raise ApcError(f"Vault JWT login failed: {type(e).__name__}") from None
    mount, _, rest = VAULT_PATH.partition("/")
    req = urllib.request.Request(f"{addr}/v1/{mount}/data/{rest}", headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            data = json.load(r)["data"]["data"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NoCredentials(f"Vault secret {VAULT_PATH} does not exist") from None
        raise ApcError(f"Vault read of {VAULT_PATH} failed: HTTP {e.code}") from None
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise ApcError(f"Vault read of {VAULT_PATH} failed: {type(e).__name__}") from None
    if not data.get("username") or not data.get("password"):
        raise NoCredentials(f"Vault secret {VAULT_PATH} needs 'username' and 'password' fields")
    return {"username": data["username"], "password": data["password"]}


# --------------------------------------------------------------------------- transport

def _scp(creds: Dict[str, str], src: str, dst: str, accept_new: bool = False) -> None:
    if not shutil.which("scp"):
        raise ApcError("scp not found")
    if not accept_new and not (KNOWN_HOSTS.exists() and KNOWN_HOSTS.read_text().strip()):
        raise ApcError(f"{KNOWN_HOSTS.name} is empty: run 'apcctl.py pin-hostkey' once and commit it")
    tmp = tempfile.mkdtemp(prefix="apcctl-")
    try:
        askpass = os.path.join(tmp, "askpass")
        with open(askpass, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s\\n" "$APCCTL_PW"\n')
        os.chmod(askpass, 0o700)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", DISPLAY=":0",
                   APCCTL_PW=creds["password"])
        # -v, not -q: the card closes the channel without an exit status, so
        # scp exits 1 even after a complete download; the size it announced
        # ("Sink: C0644 <size>") tells a complete copy from a failure.
        cmd = ["scp", "-O", "-v", *SSH_OPTS,
               "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
               "-o", "StrictHostKeyChecking=" + ("accept-new" if accept_new else "yes"),
               src.replace("CARD:", f"{creds['username']}@{HOST}:"),
               dst.replace("CARD:", f"{creds['username']}@{HOST}:")]
        r = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and not download_complete(r.stderr, dst):
            lines = [ln for ln in (r.stderr or r.stdout).splitlines() if not ln.startswith("debug")]
            msg = "\n".join(lines).strip().replace(creds["password"], "***")
            raise ApcError(f"scp failed (rc {r.returncode}): {msg[-300:]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def download_complete(scp_stderr: str, dst: str) -> bool:
    """True when scp announced a file of N bytes and dst (a local path) holds exactly N bytes."""
    if dst.startswith("CARD:"):
        return False  # uploads must exit 0
    m = SINK_RE.search(scp_stderr or "")
    try:
        return bool(m) and os.path.getsize(dst) == int(m.group(1))
    except OSError:
        return False


def download(creds: Dict[str, str], accept_new: bool = False) -> str:
    with tempfile.TemporaryDirectory(prefix="apcctl-") as d:
        dst = os.path.join(d, "config.ini")
        _scp(creds, "CARD:config.ini", dst, accept_new=accept_new)
        return Path(dst).read_text(errors="replace")


# --------------------------------------------------------------------------- commands

def cmd_check(_args) -> int:
    if not DESIRED.exists():
        print(f"{DESIRED.name}: not committed yet (baseline pending; see docs/POWER.md)")
        return 0
    bad = unredacted_secrets(DESIRED.read_text())
    if bad:
        print(f"{DESIRED.name}: secrets in clear text: " + ", ".join(bad), file=sys.stderr)
        return 1
    print(f"{DESIRED.name}: ok (no clear-text secrets)")
    return 0


def cmd_sanitize(args) -> int:
    text = sanitize(Path(args.input).read_text(errors="replace"))
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_export(args) -> int:
    text = sanitize(download(credentials()))
    if args.write:
        DESIRED.write_text(text)
        print(f"wrote {DESIRED} ({len(text.splitlines())} lines); review and commit it")
    else:
        sys.stdout.write(text)
    return 0


def cmd_drift(args) -> int:
    def report(msg: str) -> None:
        print(msg)
        if args.report:
            Path(args.report).write_text(msg + "\n")
    try:
        creds = credentials()
    except NoCredentials as e:
        report(f"ups-card drift: skipped, credentials pending ({e}). "
               f"Add {VAULT_PATH} with fields username, password (docs/POWER.md).")
        return 0
    if not DESIRED.exists():
        report("ups-card drift: skipped, no baseline yet: run 'apcctl.py export --write' and commit apc-config.ini.")
        return 0
    live = sanitize(download(creds))
    d = diff(DESIRED.read_text(), live)
    if not d:
        print("ups-card drift: none")
        return 0
    report("ups-card drift: the card differs from networking/ups/apc-config.ini\n\n" + d)
    return 1


def cmd_pin_hostkey(_args) -> int:
    creds = credentials()
    download(creds, accept_new=True)
    print(f"host key recorded in {KNOWN_HOSTS}; commit it")
    return 0


def cmd_apply(args) -> int:
    if not DESIRED.exists():
        print("no apc-config.ini to apply", file=sys.stderr)
        return 1
    payload, skipped = upload_payload(DESIRED.read_text())
    if not args.upload:
        print(f"apply plan: would upload {len(payload.splitlines())} lines to {HOST}:config.ini "
              f"({skipped} redacted lines left out, so the card keeps those values). Nothing sent.")
        return 0
    if os.environ.get("CI"):
        print("apply refuses to run under CI", file=sys.stderr)
        return 2
    if not args.i_understand_this_reconfigures_the_ups:
        print("apply --upload needs --i-understand-this-reconfigures-the-ups", file=sys.stderr)
        return 2
    creds = credentials()
    with tempfile.TemporaryDirectory(prefix="apcctl-") as d:
        src = os.path.join(d, "config.ini")
        Path(src).write_text(payload)
        _scp(creds, src, "CARD:config.ini")
    print("uploaded; the card applies it within a minute. Run 'apcctl.py drift' afterwards.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    s = sub.add_parser("sanitize")
    s.add_argument("input")
    s.add_argument("output", nargs="?")
    s.set_defaults(fn=cmd_sanitize)
    s = sub.add_parser("export")
    s.add_argument("--write", action="store_true")
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("drift")
    s.add_argument("--report")
    s.set_defaults(fn=cmd_drift)
    sub.add_parser("pin-hostkey").set_defaults(fn=cmd_pin_hostkey)
    s = sub.add_parser("apply")
    s.add_argument("--plan", action="store_true")
    s.add_argument("--upload", action="store_true")
    s.add_argument("--i-understand-this-reconfigures-the-ups", action="store_true")
    s.set_defaults(fn=cmd_apply)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ApcError as e:
        print(f"apcctl: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
