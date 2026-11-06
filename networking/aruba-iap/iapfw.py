#!/usr/bin/env python3
"""Firmware for the Aruba Instant cluster: the ``iapctl.py firmware`` subcommands.

  status        running and backup partition per AP, cluster upgrade status
  check-latest  newest release on the declared branch (see README, "Finding a release")
  bump          declare a new release in firmware.yml (and KNOWN_LATEST)
  upgrade       upgrade the whole cluster to the declared release, verify, drift

Instant runs one version per cluster, so ``upgrade`` always upgrades every AP:
the virtual controller downloads the image from a TFTP server on the router
(started only for the transfer, serving only that file), hands it to the other
APs, and all of them reboot together. Expect the Wi-Fi to be down for about
5-10 minutes.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIRMWARE_FILE = HERE / "firmware.yml"
KNOWN_LATEST_PATH = REPO / "ci" / "reports" / "firmware.py"
NETGEAR_DIR = REPO / "networking" / "switches" / "netgear"
DEFAULT_IMAGE_DIR = "/cache/aruba-iap-firmware"   # in CI; the router path is /cache/gitlab-runner/aruba-iap-firmware
ROUTER_IMAGE_DIR = "/cache/gitlab-runner/aruba-iap-firmware"
VAULT_PORTAL_PATH = "homelab/network/hpe-portal"
MODEL = "AP-515"

# Exit codes (as ngswitch): 0 ok / nothing to do, 1 failed, 2 newer release found
# (check-latest --detailed-exitcode), 3 upgraded but drift / cannot check.
EXIT_NEWER, EXIT_DRIFT, EXIT_CANNOT_CHECK = 2, 3, 3


class FwError(Exception):
    pass


class CannotCheck(Exception):
    pass


# --------------------------------------------------------------------------- versions

_VER_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)\.(\d+)(?:[_-](\d+))?(?:\s+(LSR|SSR|ESR|LTS))?")


@dataclass(frozen=True)
class Version:
    release: str             # "8.13.1.0"
    build: Optional[int]     # 93688
    train: Optional[str] = None  # "LSR"

    @property
    def key(self) -> Tuple[int, ...]:
        return tuple(int(x) for x in self.release.split(".")) + (self.build or 0,)

    @property
    def branch(self) -> str:
        return ".".join(self.release.split(".")[:2])

    def __str__(self) -> str:
        return self.release + (f"_{self.build}" if self.build is not None else "")

    def same_release(self, other: "Version") -> bool:
        """Equal release and, when both know it, equal build."""
        if self.release != other.release:
            return False
        return self.build is None or other.build is None or self.build == other.build


def parse_version(text: Optional[str]) -> Optional[Version]:
    """'8.13.1.0_93688 LSR (Digitally Signed ...)' / 'Version 8.13.1.0 LSR' / '8.13.1.0_93688'."""
    m = _VER_RE.search(text or "")
    if not m:
        return None
    return Version(".".join(m.group(i) for i in range(1, 5)), int(m.group(5)) if m.group(5) else None, m.group(6))


def require_version(text: str) -> Version:
    v = parse_version(text)
    if not v or str(v) != text.strip() and v.release != text.strip():
        raise FwError(f"not an Instant version: {text!r} (expected e.g. 8.13.1.0_93688)")
    return v


# HPE names Instant images ArubaInstant_<class>_<version>_<build>, no extension.
_IMAGE_RE = re.compile(r"^(?:ArubaInstant|[A-Za-z]+_Instant|HPE_Aruba_Networking_Instant)_([A-Za-z]+)_(\d+\.\d+\.\d+\.\d+)_(\d+)$")


def parse_image_name(name: str) -> Optional[Tuple[str, Version]]:
    m = _IMAGE_RE.match(os.path.basename(name))
    if not m:
        return None
    return m.group(1), Version(m.group(2), int(m.group(3)))


def image_name(image_class: str, v: Version) -> str:
    return f"ArubaInstant_{image_class}_{v.release}_{v.build}"


# --------------------------------------------------------------------------- firmware.yml

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_FW_KEYS = ("version", "image", "sha256")


def load_firmware(path: Path = FIRMWARE_FILE) -> dict:
    import yaml
    data = yaml.safe_load(Path(path).read_text())
    return validate_firmware(data, str(path))


def validate_firmware(data, origin: str = "firmware.yml") -> dict:
    errs: List[str] = []
    if not isinstance(data, dict):
        raise FwError(f"{origin}: not a mapping")
    fw = {k: data.get(k) for k in ("model", "image_class", "branch", "version", "image", "sha256", "aps")}
    for k in ("model", "image_class", "branch", "version", "image"):
        if not isinstance(fw[k], str) or not fw[k]:
            errs.append(f"{k} must be a non-empty string")
    if errs:
        raise FwError(f"{origin}: " + "; ".join(errs))
    v = parse_version(fw["version"])
    if not v or v.build is None or str(v) != fw["version"]:
        errs.append(f"version {fw['version']!r} must look like 8.13.1.0_93688")
    elif v.branch != fw["branch"]:
        errs.append(f"version {fw['version']} is not on branch {fw['branch']}")
    img = parse_image_name(fw["image"])
    if not img:
        errs.append(f"image {fw['image']!r} is not an Instant image name (ArubaInstant_<class>_<version>_<build>)")
    elif v and (img[0] != fw["image_class"] or str(img[1]) != fw["version"]):
        errs.append(f"image {fw['image']} does not match image_class {fw['image_class']} / version {fw['version']}")
    if fw["sha256"] is not None and not (isinstance(fw["sha256"], str) and _HEX64.match(fw["sha256"])):
        errs.append("sha256 must be 64 lowercase hex digits, or null for a release that was never downloaded")
    if not isinstance(fw["aps"], int) or fw["aps"] < 1:
        errs.append("aps must be the number of APs in the cluster")
    if errs:
        raise FwError(f"{origin}: " + "; ".join(errs))
    fw["parsed"] = v
    return fw


def replace_firmware(text: str, new: Dict[str, Optional[str]]) -> str:
    """Rewrite version / image / sha256 in firmware.yml, keeping the other lines (and
    dropping a comment at the end of a rewritten line, which described the old value)."""
    for k in _FW_KEYS:
        val = new[k]
        rendered = "null" if val is None else f'"{val}"'
        text, n = re.subn(rf"(?m)^({k}:)[ \t]*[^#\n]*?([ \t]*#.*)?$",
                          lambda m: f"{m.group(1)} {rendered}", text, count=1)
        if n != 1:
            raise FwError(f"firmware.yml has no {k}: line")
    return text


# --------------------------------------------------------------------------- CLI output parsers

def _strip_echo(text: str) -> List[str]:
    lines = text.replace("\r", "").split("\n")
    out = []
    for ln in lines:
        if re.match(r"^\S+( \(.*\))? ?#", ln):   # prompt, with or without the echoed command
            continue
        if ln.strip().startswith("show "):
            continue
        out.append(ln.rstrip())
    return out


def parse_table(lines: List[str], start: int = 0) -> Tuple[List[Dict[str, str]], int]:
    """A fixed-width CLI table: header line, a line of dash runs, rows until a blank line.

    Column spans come from the dash runs, so values with single spaces stay whole.
    Returns (rows, index after the table)."""
    for i in range(start, len(lines) - 1):
        dash = lines[i + 1]
        if lines[i].strip() and re.fullmatch(r"\s*-+(\s+-+)*\s*", dash or "") and len(dash.split()) > 1:
            spans = [(m.start(), m.end()) for m in re.finditer(r"-+", dash)]
            # a column runs from its dash run to the next one
            cols = [(a, spans[j + 1][0] if j + 1 < len(spans) else None) for j, (a, _) in enumerate(spans)]
            names = [lines[i][a:b].strip() for a, b in cols]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip():
                if re.match(r"^[A-Za-z][\w ./()-]*\s+:", lines[j]):   # "Auto reboot    :enable" ends the table
                    break
                rows.append({n: lines[j][a:b].strip() if a < len(lines[j]) else "" for n, (a, b) in zip(names, cols)})
                j += 1
            return rows, j
    return [], len(lines)


def _key_values(lines: List[str]) -> Dict[str, str]:
    out = {}
    for ln in lines:
        m = re.match(r"^\s*([A-Za-z][\w ./()-]*?)\s*:\s*(.*)$", ln)
        if m and not re.match(r"^[0-9a-f]{2}$", m.group(1)):
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def parse_image_version(text: str) -> dict:
    """`show image version` on one AP: running (primary) and backup partition."""
    kv = _key_values(_strip_echo(text))
    run = parse_version(kv.get("Primary Partition Build Version"))
    bak = parse_version(kv.get("Backup Partition Build Version"))
    if not run:
        raise FwError("show image version: no primary partition version in the output")
    classes = []
    lines = _strip_echo(text)
    for i, ln in enumerate(lines):
        if ln.strip() == "Class" and i + 1 < len(lines) and set(lines[i + 1].strip()) == {"-"}:
            classes = [x.strip() for x in lines[i + 2:] if x.strip()]
            break
    return {"running": run, "backup": bak,
            "primary_partition": kv.get("Primary Partition"), "backup_partition": kv.get("Backup Partition"),
            "classes": classes}


def parse_upgrade_info(text: str) -> dict:
    lines = _strip_echo(text)
    rows, end = parse_table(lines)
    aps = [{"mac": r.get("Mac", ""), "ip": r.get("IP Address", ""), "class": r.get("AP Class", ""),
            "status": r.get("Status", ""), "image_info": r.get("Image Info", ""),
            "error": r.get("Error Detail", "")} for r in rows]
    settings = _key_values(lines[end:])
    return {"aps": aps, "settings": settings,
            "in_process": settings.get("Upgrade in process", "").lower() not in ("no", ""),
            "auto_reboot": settings.get("Auto reboot", "").lower() == "enable"}


def parse_aps(text: str) -> List[dict]:
    rows, _ = parse_table(_strip_echo(text))
    out = []
    for r in rows:
        ip = r.get("IP Address", "")
        out.append({"name": r.get("Name", ""), "ip": ip.rstrip("*"), "conductor": ip.endswith("*"),
                    "mode": r.get("Mode", ""), "type": r.get("Type", "")})
    if not out:
        raise FwError("show aps: no APs in the output")
    return out


def upgrade_failures(info: dict) -> List[str]:
    bad = []
    for a in info["aps"]:
        st, err = a["status"].lower(), (a["error"] or "none").lower()
        if "fail" in st or "error" in st or err not in ("none", "", "-"):
            bad.append(f"{a['ip']}: {a['status']} ({a['error']})")
    return bad


# --------------------------------------------------------------------------- live state

def gather(vc_host: str, user: str, password: str, connect: Callable, log=lambda m: None) -> dict:
    """Read the cluster: `show aps` and `show upgrade info` on the virtual controller,
    `show image version` on each AP over its own address (known_hosts lists them)."""
    sess = connect(vc_host, user, password)
    try:
        aps = parse_aps(sess.cmd("show aps"))
        upg = parse_upgrade_info(sess.cmd("show upgrade info"))
    finally:
        sess.close()
    by_ip = {a["ip"]: a for a in upg["aps"]}
    for ap in aps:
        try:
            s = connect(ap["ip"], user, password)
            try:
                img = parse_image_version(s.cmd("show image version"))
            finally:
                s.close()
            ap.update(running=img["running"], backup=img["backup"], classes=img["classes"])
        except Exception as e:  # noqa: BLE001 - reported per AP; decisions treat it as unknown
            ap.update(running=None, backup=None, classes=[], error=f"{type(e).__name__}: {e}")
            log(f"   {ap['name']} ({ap['ip']}): cannot read image version: {type(e).__name__}: {e}")
        u = by_ip.get(ap["ip"], {})
        ap.update(upgrade_status=u.get("status"), image_class=u.get("class"), upgrade_error=u.get("error"))
    return {"aps": aps, "upgrade": upg}


def describe_state(fw: dict, state: dict) -> List[str]:
    want = fw["parsed"]
    out = []
    for ap in state["aps"]:
        run, bak = ap.get("running"), ap.get("backup")
        flag = ("[current]" if run and run.same_release(want) else
                "[BEHIND]" if run and run.key < want.key else
                "[NEWER THAN DECLARED]" if run else "[UNKNOWN]")
        run_s = (str(run) + (" " + run.train if run.train else "")) if run else "?"
        out.append(f"{ap['name']:<10} {ap['ip']:<13} {'conductor' if ap['conductor'] else '':<9} "
                   f"running {run_s:<20} backup {str(bak) if bak else '?':<16} "
                   f"class {ap.get('image_class') or '?'}  {flag}")
    u = state["upgrade"]
    statuses = sorted({a["status"] for a in u["aps"]}) or ["?"]
    out.append(f"upgrade status: {', '.join(statuses)} ({len(u['aps'])} AP(s)); upgrade in process: "
               f"{'yes' if u['in_process'] else 'no'}; auto reboot: {'on' if u['auto_reboot'] else 'OFF'}")
    return out


# --------------------------------------------------------------------------- decisions

def plan_upgrade(fw: dict, state: dict) -> dict:
    """What `upgrade` has to do: {"action": "noop"|"upgrade"|"refuse", "reasons": [...], "behind": [...]}."""
    want: Version = fw["parsed"]
    aps = state["aps"]
    reasons = []
    unknown = [a["name"] for a in aps if not a.get("running")]
    if unknown:
        reasons.append("cannot read the running version of " + ", ".join(unknown))
    behind = [a["name"] for a in aps if a.get("running") and not a["running"].same_release(want)]
    newer = [a["name"] for a in aps if a.get("running") and a["running"].key > want.key]
    if not reasons and not behind and len(aps) >= fw["aps"]:
        return {"action": "noop", "reasons": [], "behind": []}
    if len(aps) < fw["aps"]:
        reasons.append(f"{len(aps)} AP(s) in the cluster, firmware.yml expects {fw['aps']}")
    if newer:
        reasons.append(f"{', '.join(newer)} already run a newer release than {want}; not downgrading")
    classes = {a.get("image_class") for a in aps if a.get("image_class")}
    if classes and classes != {fw["image_class"]}:
        reasons.append(f"cluster AP class(es) {sorted(classes)} differ from the image class {fw['image_class']}")
    if state["upgrade"]["in_process"]:
        reasons.append("an upgrade is already in process on the cluster")
    if not state["upgrade"]["auto_reboot"]:
        reasons.append("auto reboot is off on the cluster (show upgrade info); the APs would not boot the new image")
    if reasons:
        return {"action": "refuse", "reasons": reasons, "behind": behind}
    return {"action": "upgrade", "reasons": [], "behind": behind}


def after_upgrade(fw: dict, before: dict, after: dict) -> dict:
    """Judge the cluster after the reboot.

    result: ok (every AP runs the declared release), fell-back (every AP is back on
    its old release: the image did not take), mixed (some on each), missing (an AP
    did not come back or cannot be read). rollback lists the APs that run the new
    release and hold their previous release on the backup partition."""
    want: Version = fw["parsed"]
    old = {a["name"]: a.get("running") for a in before["aps"]}
    now = {a["name"]: a for a in after["aps"]}
    missing = [n for n in old if n not in now or not now[n].get("running")]
    on_new = [n for n, a in now.items() if a.get("running") and a["running"].same_release(want)]
    on_old = [n for n, a in now.items() if a.get("running") and not a["running"].same_release(want)]
    rollback = [n for n in on_new if old.get(n) and now[n].get("backup") and now[n]["backup"].same_release(old[n])]
    if missing:
        result = "missing"
    elif not on_old:
        result = "ok"
    elif not on_new:
        result = "fell-back"
    else:
        result = "mixed"
    return {"result": result, "missing": missing, "on_new": on_new, "on_old": on_old, "rollback": rollback}


# --------------------------------------------------------------------------- finding a release

def staged_images(directory: Path, fw: dict) -> List[Tuple[Version, Path]]:
    """Images in the drop directory on the declared branch and class that are newer than declared."""
    want: Version = fw["parsed"]
    out = []
    try:
        entries = sorted(Path(directory).iterdir())
    except OSError:
        return []
    for p in entries:
        parsed = parse_image_name(p.name)
        if not parsed or not p.is_file():
            continue
        cls, v = parsed
        if cls == fw["image_class"] and v.branch == fw["branch"] and v.key > want.key:
            out.append((v, p))
    return sorted(out, key=lambda x: x[0].key, reverse=True)


def portal_latest(fw: dict, vault_read: Callable[[str], Optional[dict]]) -> Version:
    """Newest release on the HPE Networking Support Portal, or CannotCheck.

    The portal has no anonymous listing: its software pages are an app backed by
    a GraphQL API that answers 401 without an HPE SSO session, the release-note
    PDFs on the techdocs site answer 403 to scripts, and downloads require
    accepting the license terms per file. Listing it with an account is not
    implemented (see README, "Finding a release"); this never logs in."""
    try:
        creds = vault_read(VAULT_PORTAL_PATH)
    except Exception as e:  # noqa: BLE001
        raise CannotCheck(f"cannot read Vault {VAULT_PORTAL_PATH}: {type(e).__name__}") from None
    if not creds:
        raise CannotCheck(f"the HPE Networking Support Portal has no anonymous release listing, and there are "
                          f"no portal credentials in Vault ({VAULT_PORTAL_PATH})")
    raise CannotCheck("portal credentials are in Vault, but listing releases through the portal's private API "
                      "is not implemented yet (README, \"Finding a release\")")


def check_latest(fw: dict, image_dir: Path, vault_read: Callable[[str], Optional[dict]]) -> dict:
    want: Version = fw["parsed"]
    res = {"model": fw["model"], "declared": str(want), "branch": fw["branch"], "image_dir": str(image_dir),
           "vendor": None, "vendor_error": None, "staged": [], "latest": str(want), "newer": False, "source": None}
    try:
        v = portal_latest(fw, vault_read)
        res["vendor"] = str(v)
        if v.key > want.key:
            res.update(latest=str(v), newer=True, source="portal")
    except CannotCheck as e:
        res["vendor_error"] = str(e)
    staged = staged_images(image_dir, fw)
    res["staged"] = [{"version": str(v), "path": str(p)} for v, p in staged]
    if staged and staged[0][0].key > parse_version(res["latest"]).key:
        res.update(latest=str(staged[0][0]), newer=True, source="staged", image=str(staged[0][1]))
    return res


def format_check(res: dict) -> List[str]:
    out = [f"== {res['model']} (Instant, branch {res['branch']}): declared {res['declared']}"]
    if res["vendor_error"]:
        out.append(f"   vendor: CANNOT CHECK: {res['vendor_error']}")
    else:
        out.append(f"   vendor: newest on the branch {res['vendor']}")
    if res["staged"]:
        out.append("   staged newer image(s) in %s: %s" % (res["image_dir"], ", ".join(s["version"] for s in res["staged"])))
    else:
        out.append(f"   staged images in {res['image_dir']}: none newer than declared")
    if res["newer"]:
        out.append(f"== NEWER RELEASE {res['latest']} ({res['source']})")
    elif res["vendor_error"]:
        out.append("== cannot check for a newer release (nothing newer staged either); see README, \"Finding a release\"")
    else:
        out.append("== up to date")
    return out


# --------------------------------------------------------------------------- bump

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plausible_image(path: Path) -> None:
    size = path.stat().st_size
    if size < 8 << 20 or size > 512 << 20:
        raise FwError(f"{path.name} is {size} bytes, not an Instant image")


def bump(fw_path: Path, version: Optional[str] = None, image: Optional[Path] = None,
         sha256: Optional[str] = None, known_latest: Optional[Path] = KNOWN_LATEST_PATH, log=print) -> dict:
    """Declare a newer release. With --image the hash is computed from the file
    (and checked against --sha256 if given); without, --version and --sha256 come
    from the portal's download page and the image has to be staged before the rollout."""
    text = Path(fw_path).read_text()
    import yaml
    fw = validate_firmware(yaml.safe_load(text), str(fw_path))
    if image:
        parsed = parse_image_name(image.name)
        if not parsed:
            raise FwError(f"{image.name} is not an Instant image name (ArubaInstant_<class>_<version>_<build>)")
        cls, v = parsed
        if cls != fw["image_class"]:
            raise FwError(f"{image.name} is a {cls} image; this cluster needs {fw['image_class']}")
        if version and str(require_version(version)) != str(v):
            raise FwError(f"--version {version} does not match the image name {image.name}")
        plausible_image(image)
        digest = sha256_file(image)
        if sha256 and sha256.lower() != digest:
            raise FwError(f"{image.name}: SHA-256 {digest} differs from the expected {sha256}")
        sha256 = digest
    else:
        if not version or not sha256:
            raise FwError("give --image, or --version together with --sha256")
        v = require_version(version)
        if v.build is None:
            raise FwError("--version needs the build number, e.g. 8.13.1.1_94000")
        if not _HEX64.match(sha256.lower()):
            raise FwError("--sha256 must be 64 hex digits")
        sha256 = sha256.lower()
    if v.branch != fw["branch"]:
        raise FwError(f"{v} is not on the declared branch {fw['branch']}; change branch: by hand to move branches")
    if v.key <= fw["parsed"].key:
        raise FwError(f"{v} is not newer than the declared {fw['version']}")
    new = {"version": str(v), "image": image_name(fw["image_class"], v), "sha256": sha256}
    out = replace_firmware(text, new)
    validate_firmware(yaml.safe_load(out), str(fw_path))
    Path(fw_path).write_text(out)
    log(f"{Path(fw_path).name}: firmware {fw['version']} -> {new['version']} (image {new['image']}, sha256 {sha256})")
    if known_latest and Path(known_latest).exists():
        sys.path.insert(0, str(NETGEAR_DIR))
        import ngswitch as ng
        kl = Path(known_latest)
        kl.write_text(ng.replace_known_latest(kl.read_text(), fw["model"], v.release, time.strftime("%Y-%m-%d")))
        log(f"{kl.name}: KNOWN_LATEST[{fw['model']}] = {v.release}")
    return {"old": fw["version"], "new": new}


# --------------------------------------------------------------------------- upgrade

def _stamp(t0: float) -> Callable[[str], None]:
    def log(msg: str) -> None:
        print(f"[{int(time.time() - t0):>4}s] {msg}", flush=True)
    return log


def verify_image(fw: dict, image_dir: Path) -> Path:
    path = Path(image_dir) / fw["image"]
    if not path.is_file():
        raise FwError(f"image {fw['image']} is not staged: download it from the HPE Networking Support Portal and "
                      f"copy it to {ROUTER_IMAGE_DIR}/{fw['image']} on the router (README, \"Staging an image\")")
    if not fw["sha256"]:
        raise FwError("firmware.yml declares no sha256 for this release; declare it with `firmware bump`")
    plausible_image(path)
    digest = sha256_file(path)
    if digest != fw["sha256"]:
        raise FwError(f"{path}: SHA-256 {digest} differs from firmware.yml ({fw['sha256']}); not using it")
    return path


@dataclass
class Timing:
    download: int = 1200     # TFTP transfer and distribution to the members
    down: int = 300          # until the controller stops answering after the download
    boot: int = 900          # until the controller answers again
    settle: int = 600        # until every AP is back with a readable version
    poll: int = 15


def _send_upgrade(sess, url: str, log) -> str:
    """Send upgrade-image; answer a y/n confirmation if the CLI asks for one."""
    sess.shell.send(f"upgrade-image {url}\n")
    buf, end = "", time.time() + 60
    while time.time() < end:
        if sess.shell.recv_ready():
            buf += sess.shell.recv(65535).decode(errors="replace")
            tail = buf.replace("\r", "").rstrip()
            if re.search(r"\(y/n\)\s*:?\s*$", tail, re.I):
                log("   the CLI asks for confirmation; answering y")
                sess.shell.send("y\n")
                continue
            if re.search(r"(?:^|\n)[\w.\-]+(?: \([^)]*\))? ?# ?$", tail):
                return buf.replace("\r", "")
        else:
            time.sleep(0.1)
    return buf.replace("\r", "")


def run_upgrade(fw: dict, image_dir: Path, tftp_server: Optional[str], dry_run: bool, creds: Tuple[str, str, str],
                connect: Callable, drift: Callable[[], int], backup: Optional[Callable[[], Optional[str]]] = None,
                url: Optional[str] = None, timing: Timing = Timing(), tftp_factory: Optional[Callable] = None,
                sleep=time.sleep, log=None) -> int:
    """Bring the cluster to the declared release. Safe to re-run: it stops when every
    AP already runs it. See README, "firmware upgrade", for each step."""
    log = log or _stamp(time.time())
    host, user, pw = creds
    want: Version = fw["parsed"]
    log(f"== Instant cluster {host}: declared {want} ({fw['image_class']})")
    state = gather(host, user, pw, connect, log)
    for ln in describe_state(fw, state):
        log("   " + ln)
    plan = plan_upgrade(fw, state)
    if plan["action"] == "noop":
        log(f"== already running {want} on all {len(state['aps'])} APs; nothing to do")
        return 0
    if plan["action"] == "refuse":
        for r in plan["reasons"]:
            log(f"   refused: {r}")
        log("ERROR: not upgrading")
        return 1
    log(f"   behind: {', '.join(plan['behind'])}")
    if failures := upgrade_failures(state["upgrade"]):
        log("   note: earlier upgrade errors on the cluster: " + "; ".join(failures))
    image = verify_image(fw, image_dir) if not url else None
    if image:
        log(f"   image {image} verified (SHA-256 {fw['sha256'][:16]}..., {image.stat().st_size} bytes)")
    rc = drift()
    if rc != 0:
        log("ERROR: the cluster configuration differs from running-config.conf; not upgrading (fix the drift first)")
        return 1
    if backup:
        where = backup()
        if where:
            log(f"   configuration backup: {where}")
    if not url:
        if not tftp_server:
            raise FwError("--tftp-server <router IPv4 the APs can reach> is required (or --url)")
        url = f"tftp://{tftp_server}/{fw['image']}"
    log(f"   upgrade command: upgrade-image {url}  (the whole cluster reboots; Wi-Fi down ~5-10 min)")
    if dry_run:
        log("== dry run: would upgrade every AP now; nothing sent")
        return 0

    t_start = time.time()
    if tftp_factory is None and image:
        sys.path.insert(0, str(NETGEAR_DIR))
        import ngswitch as ng

        def tftp_factory(addr, path):
            return ng.TftpServer(addr, path, log=log)
    import contextlib
    server_cm = tftp_factory(tftp_server, image) if image else contextlib.nullcontext()
    with server_cm as srv:
        sess = connect(host, user, pw)
        try:
            out = _send_upgrade(sess, url, log)
            body = out.split("\n", 1)[-1]
            if re.search(r"% ?Parse error|Invalid input|ERROR|% ?Invalid|% ?Incomplete|not allowed|fail", body, re.I):
                log("ERROR: the controller rejected the upgrade: " + " ".join(body.split())[:300])
                return 1
            log("   upgrade started; watching show upgrade info")
            last, end = None, time.time() + timing.download
            while time.time() < end:
                sleep(timing.poll)
                try:
                    info = parse_upgrade_info(sess.cmd("show upgrade info", timeout=60))
                except Exception as e:  # noqa: BLE001 - the controller reboots: session gone
                    log(f"   controller session ended ({type(e).__name__}); the APs are rebooting")
                    break
                cur = ", ".join(f"{a['ip']} {a['status']}" for a in info["aps"])
                if cur != last:
                    log(f"   {cur}" + (f"; TFTP sent {srv.sent} bytes" if srv is not None else ""))
                    last = cur
                bad = upgrade_failures(info)
                if bad:
                    log("ERROR: upgrade failed before any reboot: " + "; ".join(bad))
                    log("   the APs keep running the old release; nothing to roll back")
                    return 1
            else:
                log(f"ERROR: the controller still answers after {timing.download}s; the upgrade did not reboot the "
                    "cluster. Check show upgrade info.")
                return 1
        finally:
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
    log("   waiting for the virtual controller to come back")
    end = time.time() + timing.boot
    while True:
        sleep(timing.poll)
        try:
            s = connect(host, user, pw)
            s.close()
            break
        except Exception:  # noqa: BLE001 - still booting
            if time.time() > end:
                log(f"ERROR: the virtual controller {host} did not come back within {timing.boot}s. "
                    "Check the APs on site (power, LEDs); Instant boots the old partition if the new image fails.")
                return 1
    log("   controller answers; waiting for every AP")
    end = time.time() + timing.settle
    after, verdict = None, None
    while True:
        try:
            after = gather(host, user, pw, connect, lambda m: None)
            verdict = after_upgrade(fw, state, after)
        except Exception as e:  # noqa: BLE001 - members still booting
            log(f"   not readable yet: {type(e).__name__}")
        if verdict and verdict["result"] == "ok" and not after["upgrade"]["in_process"]:
            break
        if time.time() > end:
            break
        sleep(timing.poll)
    if verdict is None:
        log(f"!! the cluster could not be read within {timing.settle}s after the controller came back; check it on site")
        return 1
    for ln in describe_state(fw, after):
        log("   " + ln)
    took = int(time.time() - t_start)
    if verdict["result"] == "ok":
        log(f"== upgraded {len(after['aps'])} APs to {want} (Wi-Fi outage about {took // 60} min)")
        rc = drift()
        if rc != 0:
            log("!! drift after the upgrade: the new release reports configuration that differs from "
                "running-config.conf. The cluster keeps the new release; review `iapctl.py plan`, export if the "
                "difference is new defaults, or roll back with `switch-partition-reboot all` on the controller.")
            return EXIT_DRIFT
        return 0
    if verdict["result"] == "fell-back":
        log(f"!! every AP came back running its old release: the new image did not take. DID NOT UPGRADE.")
        return 1
    if verdict["result"] == "missing":
        log(f"!! {', '.join(verdict['missing'])} did not come back (or cannot be read) within {timing.settle}s. "
            "Not rolling back blind: check the AP on site.")
        return 1
    # mixed: one version per cluster is required, go back to the old release on the upgraded APs
    log(f"!! mixed versions: {', '.join(verdict['on_new'])} on {want}, {', '.join(verdict['on_old'])} on the old "
        "release. ROLLING BACK the upgraded APs to their backup partition.")
    if not verdict["rollback"]:
        log("!! no upgraded AP holds its previous release on the backup partition; cannot roll back")
        return 1
    for name in verdict["rollback"]:
        ap = next(a for a in after["aps"] if a["name"] == name)
        try:
            s = connect(ap["ip"], user, pw)
            try:
                log(f"   {name}: switch-partition-reboot (boots {ap['backup']})")
                s.shell.send("switch-partition-reboot\n")
                sleep(5)
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            log(f"!! {name}: rollback command failed: {type(e).__name__}: {e}")
    end = time.time() + timing.boot + timing.settle
    while True:
        sleep(timing.poll)
        try:
            again = gather(host, user, pw, connect, lambda m: None)
            if all(a.get("running") and not a["running"].same_release(want) for a in again["aps"]) \
                    and len(again["aps"]) >= len(state["aps"]):
                log("   after rollback: " + "; ".join(f"{a['name']} {a['running']}" for a in again["aps"]))
                break
        except Exception:  # noqa: BLE001
            pass
        if time.time() > end:
            log("!! after rollback the cluster is still not on one release; check it on site")
            return 1
    log("!! rolled back; the upgrade FAILED")
    return 1
