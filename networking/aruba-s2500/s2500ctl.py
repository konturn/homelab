#!/usr/bin/env python3
"""Read-only inventory and (manual-only) firmware for the Aruba S2500 core switch.

The S2500 (ArubaOS-MAS 7.4) carries the router's uplinks and both NETGEAR
switches: rebooting it takes the whole LAN down, and a bad image can need the
serial console. Nothing here writes to the switch except `firmware upgrade`,
which refuses to run unless S2500_FIRMWARE_CONFIRM=I-AM-HOME is set and it is
outside the backup window. See README.md.

Subcommands
  inventory              show version / show image version / show boot (read-only)
  firmware status        running version, both partitions, boot partition
  firmware check-latest  newest known ArubaOS-MAS 7.4 release, staged images
  firmware upgrade       load the declared image into the other partition, boot it,
                         verify (manual only, never scheduled)

Credentials: S2500_HOST / S2500_USER / S2500_PASS (/ S2500_ENABLE) or Vault
homelab/network/aruba-s2500 (host, username, password, optional enable_secret),
with VAULT_ADDR plus VAULT_TOKEN or VAULT_ID_TOKEN (CI, role ci-deploy). Without
credentials, status falls back to SNMP sysDescr (running version only).

Exit codes: 0 ok / nothing to do, 1 failed, 2 newer release (check-latest
--detailed-exitcode), 3 cannot check / refused by a guard, 4 no credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIRMWARE_FILE = HERE / "firmware.yml"
KNOWN_HOSTS = HERE / "known_hosts"
VAULT_PATH = "homelab/network/aruba-s2500"
DEFAULT_HOST = os.environ.get("S2500_HOST", "10.100.0.1")
DEFAULT_IMAGE_DIR = "/cache/aruba-s2500-firmware"         # in CI
ROUTER_IMAGE_DIR = "/cache/gitlab-runner/aruba-s2500-firmware"
BACKUP_WINDOW = "02:15-04:00"
CONFIRM = "I-AM-HOME"
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
# ArubaOS-MAS 7.4 is the last release train for the S1500/S2500/S3500. HPE lists
# no newer release; 7.4.1.12 is the newest one known (checked 2026-11-06).
LAST_KNOWN_RELEASE = "7.4.1.12"

EXIT_NEWER, EXIT_REFUSED, EXIT_NO_CREDS = 2, 3, 4


class S2500Error(Exception):
    pass


# --------------------------------------------------------------------------- versions

_VER_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)\.(\d+)(?:\s*\((\d+)\)|[_-](\d+))?")


@dataclass(frozen=True)
class Version:
    release: str
    build: Optional[int] = None

    @property
    def key(self) -> Tuple[int, ...]:
        return tuple(int(x) for x in self.release.split(".")) + (self.build or 0,)

    def same_release(self, other: "Version") -> bool:
        return self.release == other.release and (self.build is None or other.build is None or self.build == other.build)

    def __str__(self) -> str:
        return self.release + (f"_{self.build}" if self.build is not None else "")


def parse_version(text: Optional[str]) -> Optional[Version]:
    """'ArubaOS 7.4.1.12 (72393)' / '7.4.1.12_72393' / 'Version 7.4.1.12 (72393)'."""
    m = _VER_RE.search(text or "")
    if not m:
        return None
    b = m.group(5) or m.group(6)
    return Version(".".join(m.group(i) for i in range(1, 5)), int(b) if b else None)


_IMAGE_RE = re.compile(r"^ArubaOS_MAS_(\d+\.\d+\.\d+\.\d+)_(\d+)$")


def parse_image_name(name: str) -> Optional[Version]:
    m = _IMAGE_RE.match(os.path.basename(name))
    return Version(m.group(1), int(m.group(2))) if m else None


# --------------------------------------------------------------------------- CLI output parsers

def _lines(text: str) -> List[str]:
    out = []
    for ln in text.replace("\r", "").split("\n"):
        if re.match(r"^\([^)]*\) ?(\([^)]*\) ?)?[#>]", ln):   # prompt (with the echoed command)
            continue
        out.append(ln.rstrip())
    return out


def parse_show_version(text: str) -> Tuple[Optional[Version], Optional[str]]:
    """'ArubaOS (MODEL: ArubaS2500-48P-US), Version 7.4.1.12 (72393)' -> (Version, model)."""
    m = re.search(r"Version\s+(\d+\.\d+\.\d+\.\d+(?:\s*\(\d+\))?)", text)
    model = re.search(r"MODEL:\s*([\w-]+)", text)
    return (parse_version(m.group(1)) if m else None), (model.group(1) if model else None)


def parse_image_version(text: str) -> List[dict]:
    """`show image version`: one entry per partition, as printed by ArubaOS 7.x:

        Partition               : 0:0 (/dev/ud1) **Default boot**
        Software Version        : ArubaOS 7.4.1.12 (Digitally Signed - Production Build)
        Build number            : 72393
    """
    parts: List[dict] = []
    cur: Optional[dict] = None
    for ln in _lines(text):
        m = re.match(r"^\s*Partition\s*:\s*(\d+(?::\d+)?)(.*)$", ln)
        if m:
            cur = {"partition": m.group(1), "default_boot": "default boot" in m.group(2).lower(),
                   "version": None, "build": None, "empty": False}
            parts.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r"^\s*Software Version\s*:\s*(.*)$", ln)
        if m:
            cur["version_text"] = m.group(1).strip()
            v = parse_version(m.group(1))
            cur["version"] = v.release if v else None
            if not v:
                cur["empty"] = True
            continue
        m = re.match(r"^\s*Build number\s*:\s*(\d+)", ln)
        if m:
            cur["build"] = int(m.group(1))
        if re.search(r"No image|Image not present|empty", ln, re.I):
            cur["empty"] = True
    for p in parts:
        p["parsed"] = Version(p["version"], p["build"]) if p["version"] else None
        p["number"] = int(p["partition"].split(":")[-1])
    if not parts:
        raise S2500Error("show image version: no partitions in the output")
    return parts


def parse_show_boot(text: str) -> Optional[int]:
    """'Boot Partition: PARTITION 0' -> 0."""
    m = re.search(r"Boot Partition\s*:\s*PARTITION\s*(\d+)", text, re.I)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- firmware.yml

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def load_firmware(path: Path = FIRMWARE_FILE) -> dict:
    import yaml
    data = yaml.safe_load(Path(path).read_text())
    if not isinstance(data, dict):
        raise S2500Error(f"{path}: not a mapping")
    errs = []
    v = parse_version(str(data.get("version") or ""))
    if not v or v.build is None or str(v) != data.get("version"):
        errs.append("version must look like 7.4.1.12_72393")
    if data.get("image") is not None:
        iv = parse_image_name(str(data["image"]))
        if not iv or (v and str(iv) != str(v)):
            errs.append(f"image {data['image']!r} must be ArubaOS_MAS_<version>_<build> matching version")
    if data.get("sha256") is not None and not _HEX64.match(str(data["sha256"])):
        errs.append("sha256 must be 64 lowercase hex digits or null")
    if errs:
        raise S2500Error(f"{path}: " + "; ".join(errs))
    return {"model": data.get("model", "S2500"), "version": data["version"], "parsed": v,
            "image": data.get("image"), "sha256": data.get("sha256")}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- credentials

def _vault_request(url: str, token: Optional[str] = None, data: Optional[dict] = None) -> dict:
    ctx = ssl.create_default_context()
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
        ctx = ssl._create_unverified_context()  # noqa: S323 (matches the other CI jobs)
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 method="POST" if data else "GET")
    if token:
        req.add_header("X-Vault-Token", token)
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.load(r)


def vault_kv_read(path: str) -> Optional[dict]:
    addr = (os.environ.get("VAULT_ADDR") or "").rstrip("/")
    if not addr:
        return None
    tok = os.environ.get("VAULT_TOKEN")
    if not tok and os.environ.get("VAULT_ID_TOKEN"):
        tok = _vault_request(addr + "/v1/auth/jwt/login", data={
            "role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": os.environ["VAULT_ID_TOKEN"]})["auth"]["client_token"]
    if not tok:
        return None
    mount, _, rest = path.partition("/")
    try:
        return _vault_request(f"{addr}/v1/{mount}/data/{rest}", token=tok)["data"]["data"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def credentials(read=vault_kv_read) -> Optional[dict]:
    """{host, username, password, enable_secret} or None when nothing is configured."""
    env = {k: os.environ.get(v) for k, v in (("host", "S2500_HOST"), ("username", "S2500_USER"),
                                             ("password", "S2500_PASS"), ("enable_secret", "S2500_ENABLE"))}
    if env["username"] and env["password"]:
        env["host"] = env["host"] or DEFAULT_HOST
        return env
    data = read(VAULT_PATH)
    if not data or not data.get("username") or not data.get("password"):
        return None
    return {"host": data.get("host") or DEFAULT_HOST, "username": data["username"], "password": data["password"],
            "enable_secret": data.get("enable_secret")}


NO_CREDS = (f"no S2500 credentials: put host, username, password (and enable_secret if `enable` asks for one) "
            f"in Vault {VAULT_PATH}")


# --------------------------------------------------------------------------- SSH session

PROMPT_RE = re.compile(r"(?:^|\n)\([^)\n]+\) ?(?:\([^)\n]*\) ?)?[#>] ?$")
ERROR_RE = re.compile(r"% ?Invalid|Invalid input|Parse error|% ?Incomplete|^Error|failed|not permitted", re.I | re.M)


class Session:
    """SSH CLI session. The host key must be in known_hosts (pinned; the switch's
    OpenSSH 5.8 offers only ssh-rsa host keys and CBC ciphers, which paramiko accepts)."""

    def __init__(self, host, user, password, enable_secret=None, known_hosts=KNOWN_HOSTS, timeout=20):
        import paramiko  # lazily, so offline use needs no dependency
        self.client = paramiko.SSHClient()
        known = {}
        with open(known_hosts) as f:
            for line in f:
                p = line.split()
                if len(p) >= 3 and not p[0].startswith("#"):
                    for h in p[0].split(","):
                        known.setdefault(h, set()).add((p[1], p[2]))

        class Pinned(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                if (key.get_name(), key.get_base64()) not in known.get(hostname, set()):
                    raise paramiko.SSHException(f"host key for {hostname} ({key.get_name()}) is not in {known_hosts}")

        self.client.set_missing_host_key_policy(Pinned())
        self.client.connect(host, username=user, password=password, look_for_keys=False, allow_agent=False,
                            timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
        self.shell = self.client.invoke_shell(width=250, height=1000)
        first = self._read(30)
        if first.rstrip().endswith(">"):
            self.shell.send("enable\n")
            out = self._read(15, extra=r"[Pp]assword:\s*$")
            if re.search(r"[Pp]assword:\s*$", out):
                # ArubaOS password prompts end on "\r"; a "\n" becomes part of the secret.
                self.shell.send((enable_secret or "") + "\r")
                out = self._read(15)
            if not out.rstrip().endswith("#"):
                raise S2500Error("enable mode refused (enable_secret in Vault?)")
        self.cmd("no paging")

    def _read(self, timeout, extra: Optional[str] = None) -> str:
        buf, end = "", time.time() + timeout
        while time.time() < end:
            if self.shell.recv_ready():
                d = self.shell.recv(65535).decode(errors="replace")
                buf += d
                if "--More--" in d[-200:]:
                    self.shell.send(" ")
                tail = buf.replace("\r", "").rstrip("\n")
                if PROMPT_RE.search(tail) or (extra and re.search(extra, tail)):
                    time.sleep(0.3)
                    if not self.shell.recv_ready():
                        return buf.replace("\r", "")
            else:
                time.sleep(0.05)
        raise TimeoutError(f"no prompt after {timeout}s")

    def cmd(self, line: str, timeout=60, extra: Optional[str] = None) -> str:
        self.shell.send(line + "\n")
        return self._read(timeout, extra)

    def close(self):
        try:
            self.shell.send("exit\n")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        self.client.close()


def connect(creds: dict) -> Session:
    return Session(creds["host"], creds["username"], creds["password"], creds.get("enable_secret"))


def read_state(sess) -> dict:
    ver, model = parse_show_version(sess.cmd("show version"))
    parts = parse_image_version(sess.cmd("show image version"))
    boot = parse_show_boot(sess.cmd("show boot"))
    return {"running": ver, "model": model, "partitions": parts, "boot": boot}


def snmp_version(host: str = DEFAULT_HOST) -> Tuple[Optional[Version], Optional[str]]:
    try:
        r = subprocess.run(["snmpget", "-v2c", "-c", "public", "-t", "3", "-r", "1", "-Oqv", host, SYS_DESCR],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    return parse_show_version(r.stdout) if r.returncode == 0 else (None, None)


def describe(fw: Optional[dict], st: dict) -> List[str]:
    out = [f"running {st['running'] or '?'} (model {st.get('model') or '?'}), boot partition "
           f"{st['boot'] if st.get('boot') is not None else '?'}"]
    for p in st.get("partitions") or []:
        out.append(f"partition {p['partition']}: {p['parsed'] or 'empty'}"
                   + ("  [default boot]" if p["default_boot"] else ""))
    if fw and st["running"]:
        want = fw["parsed"]
        out.append("declared " + str(want) + (": current" if st["running"].same_release(want) else
                                                ": BEHIND" if st["running"].key < want.key else ": running is newer"))
    return out


# --------------------------------------------------------------------------- decisions

def in_window(window: str, now: Optional[time.struct_time] = None) -> bool:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", window or "")
    if not m:
        raise S2500Error(f"window must look like 02:15-04:00, not {window!r}")
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    return a <= cur < b if a < b else (cur >= a or cur < b)


def guard(env: dict, dry_run: bool, now: Optional[time.struct_time] = None) -> List[str]:
    """Reasons an upgrade must not run now. A dry run needs no confirmation."""
    reasons = []
    if not dry_run and (env.get("S2500_FIRMWARE_CONFIRM") or "") != CONFIRM:
        reasons.append(f"S2500_FIRMWARE_CONFIRM={CONFIRM} is not set. The reboot drops every network service; run "
                       "this only while at home, with the serial console at hand")
    window = env.get("S2500_BACKUP_WINDOW") or BACKUP_WINDOW
    if not dry_run and in_window(window, now):
        reasons.append(f"inside the backup window {window}; backups would lose the network mid-run")
    return reasons


def plan_upgrade(fw: dict, st: dict) -> dict:
    """{"action": noop|upgrade|refuse, "target": partition number, "copy": bool, "reasons": [...]}."""
    want: Version = fw["parsed"]
    run: Optional[Version] = st.get("running")
    if not run:
        return {"action": "refuse", "reasons": ["cannot read the running version"]}
    if run.same_release(want):
        return {"action": "noop", "reasons": []}
    if run.key > want.key:
        return {"action": "refuse", "reasons": [f"the switch runs {run}, newer than the declared {want}"]}
    parts = st.get("partitions") or []
    boot = st.get("boot")
    if boot is None or len(parts) < 2:
        return {"action": "refuse", "reasons": ["cannot tell the boot partition and both partitions apart"]}
    running_parts = [p for p in parts if p["number"] == boot]
    if not running_parts or not (running_parts[0]["parsed"] and running_parts[0]["parsed"].same_release(run)):
        return {"action": "refuse", "reasons": [f"the boot partition {boot} does not hold the running {run}; "
                                                "sort out the partitions by hand"]}
    other = [p for p in parts if p["number"] != boot][0]
    copy = not (other["parsed"] and other["parsed"].same_release(want) and other["parsed"].build == want.build)
    return {"action": "upgrade", "target": other["number"], "old_partition": boot, "copy": copy, "reasons": []}


# --------------------------------------------------------------------------- commands

def cmd_inventory(args) -> int:
    creds = credentials()
    if not creds:
        print(NO_CREDS)
        return EXIT_NO_CREDS
    sess = connect(creds)
    try:
        for c in ("show version", "show image version", "show boot"):
            print(f"#### {c}")
            print("\n".join(_lines(sess.cmd(c))).strip())
    finally:
        sess.close()
    return 0


def cmd_status(args) -> int:
    fw = load_firmware(Path(args.file))
    creds = credentials()
    print(f"== S2500 {creds['host'] if creds else DEFAULT_HOST}: declared {fw['version']}")
    if not creds:
        v, model = snmp_version(DEFAULT_HOST)
        print(f"   {NO_CREDS}")
        if v:
            print(f"   SNMP sysDescr: running {v} (model {model}); partitions need the CLI")
            st = {"running": v, "model": model, "partitions": [], "boot": None}
            print("   " + describe(fw, st)[-1])
        else:
            print("   SNMP sysDescr: no answer")
        return EXIT_NO_CREDS
    sess = connect(creds)
    try:
        st = read_state(sess)
    finally:
        sess.close()
    for ln in describe(fw, st):
        print("   " + ln)
    return 0


def check_latest(fw: dict, image_dir: Path) -> dict:
    want = fw["parsed"]
    res = {"declared": str(want), "last_known": LAST_KNOWN_RELEASE, "staged": [], "newer": False,
           "vendor_error": "the HPE Networking Support Portal has no anonymous release listing (README, "
                           "\"Finding a release\")"}
    try:
        for p in sorted(Path(image_dir).iterdir()):
            v = parse_image_name(p.name)
            if v and p.is_file() and v.key > want.key:
                res["staged"].append({"version": str(v), "path": str(p)})
    except OSError:
        pass
    res["staged"].sort(key=lambda s: parse_version(s["version"]).key, reverse=True)
    if res["staged"]:
        res.update(newer=True, latest=res["staged"][0]["version"])
    elif Version(LAST_KNOWN_RELEASE).key > Version(want.release).key:
        res.update(newer=True, latest=LAST_KNOWN_RELEASE)
    return res


def cmd_check_latest(args) -> int:
    fw = load_firmware(Path(args.file))
    res = check_latest(fw, Path(args.image_dir))
    print(f"== S2500 (ArubaOS-MAS 7.4): declared {res['declared']}")
    print(f"   vendor: CANNOT CHECK: {res['vendor_error']}")
    print(f"   ArubaOS-MAS 7.4 is the last release train for the S2500 (end of life); the newest release known "
          f"is {LAST_KNOWN_RELEASE}, so a newer one is unlikely")
    if res["staged"]:
        print("   staged newer image(s) in %s: %s" % (args.image_dir, ", ".join(s["version"] for s in res["staged"])))
    else:
        print(f"   staged images in {args.image_dir}: none newer than declared")
    print(f"== NEWER RELEASE {res['latest']}" if res["newer"] else "== nothing newer known")
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2) + "\n")
    if res["newer"]:
        return EXIT_NEWER if args.detailed_exitcode else 0
    return EXIT_REFUSED if args.detailed_exitcode else 0


@dataclass
class Timing:
    copy: int = 1200
    down: int = 300
    boot: int = 1200
    poll: int = 15


def run_upgrade(fw: dict, creds: dict, image_dir: Path, tftp_server: str, dry_run: bool, connect_fn: Callable,
                env: Optional[dict] = None, timing: Timing = Timing(), tftp_factory: Optional[Callable] = None,
                sleep=time.sleep, log=print, now: Optional[time.struct_time] = None) -> int:
    env = os.environ if env is None else env
    reasons = guard(env, dry_run, now)
    if reasons:
        for r in reasons:
            log(f"REFUSED: {r}")
        return EXIT_REFUSED
    want: Version = fw["parsed"]
    sess = connect_fn(creds)
    try:
        st = read_state(sess)
    finally:
        sess.close()
    for ln in describe(fw, st):
        log("   " + ln)
    plan = plan_upgrade(fw, st)
    if plan["action"] == "noop":
        log(f"== already running {want}; nothing to do")
        return 0
    if plan["action"] == "refuse":
        for r in plan["reasons"]:
            log(f"REFUSED: {r}")
        return EXIT_REFUSED
    image = None
    if plan["copy"]:
        if not fw.get("image") or not fw.get("sha256"):
            raise S2500Error("firmware.yml declares no image/sha256 for this release")
        image = Path(image_dir) / fw["image"]
        if not image.is_file():
            raise S2500Error(f"image not staged: copy it to {ROUTER_IMAGE_DIR}/{fw['image']} on the router")
        digest = sha256_file(image)
        if digest != fw["sha256"]:
            raise S2500Error(f"{image}: SHA-256 {digest} differs from firmware.yml; not using it")
        log(f"   image {image} verified ({image.stat().st_size} bytes)")
    t = plan["target"]
    steps = ([f"copy tftp: {tftp_server} {fw['image']} system: partition {t}"] if plan["copy"] else []) + \
        [f"boot system partition {t}", "reload"]
    for s in steps:
        log(f"   {'would send' if dry_run else 'step'}: {s}")
    if dry_run:
        log("== dry run: nothing sent. A real run reboots the core switch: the whole LAN is down for ~5-10 min.")
        return 0
    if plan["copy"]:
        if tftp_factory is None:
            sys.path.insert(0, str(REPO / "networking" / "switches" / "netgear"))
            import ngswitch as ng

            def tftp_factory(addr, path):
                return ng.TftpServer(addr, path, log=log)
        with tftp_factory(tftp_server, image):
            sess = connect_fn(creds)
            try:
                out = sess.cmd(steps[0], timeout=timing.copy)
                body = out.split("\n", 1)[-1]
                if ERROR_RE.search(body):
                    log("ERROR: the copy failed: " + " ".join(body.split())[:300])
                    return 1
                st2 = read_state(sess)
            finally:
                sess.close()
        got = [p for p in st2["partitions"] if p["number"] == t]
        if not got or not got[0]["parsed"] or not got[0]["parsed"].same_release(want):
            log(f"ERROR: partition {t} does not hold {want} after the copy; the boot partition is unchanged")
            return 1
        log(f"   partition {t} now holds {got[0]['parsed']}")
    sess = connect_fn(creds)
    try:
        out = sess.cmd(f"boot system partition {t}")
        if ERROR_RE.search(out.split("\n", 1)[-1]):
            log("ERROR: boot system partition was refused; nothing rebooted")
            return 1
        out = sess.cmd("write memory")
        log("   rebooting the core switch now")
        sess.shell.send("reload\n")
        sleep(3)
        buf = sess.shell.recv(65535).decode(errors="replace") if sess.shell.recv_ready() else ""
        if re.search(r"\(y/n\)", buf, re.I):
            sess.shell.send("y\n")
    finally:
        try:
            sess.close()
        except Exception:  # noqa: BLE001
            pass
    end = time.time() + timing.boot
    sleep(timing.down)
    while True:
        try:
            s = connect_fn(creds)
            try:
                after = read_state(s)
            finally:
                s.close()
            break
        except Exception:  # noqa: BLE001 - still booting
            if time.time() > end:
                log(f"!! the switch did not come back within {timing.boot + timing.down}s. Use the serial console; "
                    f"`boot system partition {plan['old_partition']}` then `reload` goes back to {st['running']}.")
                return 1
            sleep(timing.poll)
    for ln in describe(fw, after):
        log("   " + ln)
    if after["running"] and after["running"].same_release(want):
        log(f"== upgraded the S2500 {st['running']} -> {want}")
        return 0
    log(f"!! the switch came back running {after['running']}, not {want}. Pointing the boot back at partition "
        f"{plan['old_partition']}.")
    try:
        s = connect_fn(creds)
        try:
            s.cmd(f"boot system partition {plan['old_partition']}")
            s.cmd("write memory")
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        log(f"!! could not reset the boot partition: {type(e).__name__}: {e}")
    return 1


def cmd_upgrade(args) -> int:
    fw = load_firmware(Path(args.file))
    creds = credentials()
    if not creds:
        print(NO_CREDS)
        return EXIT_NO_CREDS
    if not args.night_lock:
        return run_upgrade(fw, creds, Path(args.image_dir), args.tftp_server, args.dry_run, connect)
    sys.path.insert(0, str(REPO / "networking"))
    import firmware_night
    try:
        with firmware_night.NightLock("aruba-s2500", scheduled=False) as lk:
            rc = run_upgrade(fw, creds, Path(args.image_dir), args.tftp_server, args.dry_run, connect)
            if not args.dry_run and rc in (0, 1):
                lk.used("upgraded" if rc == 0 else "upgrade failed")
            return rc
    except firmware_night.Busy as e:
        print(f"REFUSED: {e}")
        return EXIT_REFUSED


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("inventory", help="show version / image version / boot (read-only)")
    s.set_defaults(fn=cmd_inventory)
    f = sub.add_parser("firmware")
    fsub = f.add_subparsers(dest="fwcmd", required=True)
    image_dir = os.environ.get("S2500_FIRMWARE_DIR", DEFAULT_IMAGE_DIR)
    s = fsub.add_parser("status")
    s.add_argument("--file", default=str(FIRMWARE_FILE))
    s.set_defaults(fn=cmd_status)
    s = fsub.add_parser("check-latest")
    s.add_argument("--file", default=str(FIRMWARE_FILE))
    s.add_argument("--image-dir", default=image_dir)
    s.add_argument("--json")
    s.add_argument("--detailed-exitcode", action="store_true")
    s.set_defaults(fn=cmd_check_latest)
    s = fsub.add_parser("upgrade", help="MANUAL ONLY: reboots the core switch (needs S2500_FIRMWARE_CONFIRM)")
    s.add_argument("--file", default=str(FIRMWARE_FILE))
    s.add_argument("--image-dir", default=image_dir)
    s.add_argument("--tftp-server", default=os.environ.get("S2500_TFTP_SERVER", "10.100.0.2"),
                   help="router address on the switch's management link (default %(default)s)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--night-lock", action="store_true", help="hold the shared firmware lock (CI)")
    s.set_defaults(fn=cmd_upgrade)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except S2500Error as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
