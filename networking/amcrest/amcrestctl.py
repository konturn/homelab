#!/usr/bin/env python3
"""Keep the settings that matter on Amcrest (Dahua-based) cameras as code.

The desired state is one YAML file per camera in this directory (for now
``back-camera.yaml``): the camera's address, the Vault path of its
password, and the managed settings as the camera reports them through
``configManager.cgi?action=getConfig``. Only the settings listed in MANAGED
are captured: time (NTP, time zone, DST), cloud and P2P, outbound push
targets (FTP/SFTP/RTMP/alarm/register servers, syslog), network mode and the
local services. Python stdlib only. See docs/amcrest.md.

Subcommands
  export   read the live settings and write them to the YAML file (or stdout)
  drift    compare the live settings with the YAML file; exit 2 on drift
  check    offline: the YAML parses and every key is a managed setting
  plan     print the setConfig requests that would restore the file (dry
           run; nothing is ever sent, there is no apply)

Every request is a read (getConfig, magicBox get*). Credentials: the
AMCREST_USER / AMCREST_PASSWORD environment variables, else the password
field of the file's vault_path (VAULT_ADDR plus VAULT_TOKEN, or VAULT_ID_TOKEN
for a JWT login with VAULT_JWT_ROLE, default "ci-deploy"). The camera locks
logins after a few failures, so a rejected password is never retried.

Exit codes: 0 ok / no differences, 1 error, 2 drift (drift) or pending
changes (plan --detailed-exitcode).
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "back-camera.yaml")
SCHEMA = "1"
CONFIG_PATH = "/cgi-bin/configManager.cgi"

# --------------------------------------------------------------------------
# Managed settings: group -> key patterns. "[*]" matches any array index.
# Keys are as the camera reports them without the "table.All." prefix.
# Never add anything carrying a secret, a serial number or a hardware
# address (the repo is mirrored publicly): T2UServer.Key/Username/UUID,
# passwords, MachineName/Bonjour.Name (= serial), PhysicalAddress.
# --------------------------------------------------------------------------
_DST = [f"Locales.{edge}.{f}" for edge in ("DSTStart", "DSTEnd")
        for f in ("Month", "Week", "Day", "Hour", "Minute")]
MANAGED: dict[str, list[str]] = {
    # NTP server and time zone; DST rule (US: 2nd Sunday of March to 1st
    # Sunday of November, 02:00, by week day).
    "time": ["NTP.Enable", "NTP.Address", "NTP.Port", "NTP.TimeZone", "NTP.TimeZoneDesc",
             "NTP.UpdatePeriod", "Locales.DSTEnable", "Locales.WeekEnable", *_DST],
    # P2P (Amcrest View app), DDNS, UPnP port mapping, app push, email,
    # cloud firmware auto-check.
    "cloud": ["T2UServer.Enable", "DDNS[*].Enable", "UPnP.Enable",
              "MobilePhoneApplication.PushNotificationEnable", "Email.Enable",
              "Email.HealthReport.Enable", "_DHCloudUpgradeRecord_.AutoCheck"],
    # Places the camera would push to on its own. All off / 0.0.0.0.
    "outbound": ["DVRIP.RegisterServer.Enable", "DVRIP.RegisterServer.Servers[*].Address",
                 "NAS[*].Enable", "NAS[*].Address", "SFTP[*].Enable", "SFTP[*].Address",
                 "RTMP_NVR.Enable", "RTMP_NVR.Address", "VSP_AMCREST.IPAddress",
                 "AlarmServer.Enable", "AlarmServer.Address", "Syslog.Enable", "Syslog.Server"],
    # Addressing mode only: the address itself comes from the DHCP reservation.
    "network": ["Network.eth0.DhcpEnable", "IPv6.Enable"],
    # Local services and how they authenticate.
    "services": ["Web.Port", "Https.Enable", "RTSP.Enable", "RTSP.Port", "DVRIP.TCPPort",
                 "Telnet.Enable", "SSHD.Enable", "Bonjour.Enable", "DeviceDiscovery.Enable",
                 "StreamAuthority.HttpLoginCheck", "StreamAuthority.RtspLoginCheck",
                 "StreamAuthority.OnvifLoginCheck", "PwdReset.Enable",
                 "RemoteDebugCtrl.Debug", "RemoteDebugCtrl.Log"],
}
# Not drift-checked (exported, shown by plan): none yet.
NO_DRIFT: set[str] = set()


def _pattern(p: str) -> re.Pattern:
    return re.compile("^" + re.escape(p).replace(r"\[\*\]", r"\[\d+\]") + "$")


_PATTERNS = [(g, p, _pattern(p)) for g, ps in MANAGED.items() for p in ps]


def group_of(key: str) -> str | None:
    for g, _, rx in _PATTERNS:
        if rx.match(key):
            return g
    return None


# --------------------------------------------------------------------------
# Parsing the camera's answers
# --------------------------------------------------------------------------


def parse_table(text: str) -> dict[str, str]:
    """``table.All.NTP.Address=10.6.0.1`` lines -> {"NTP.Address": "10.6.0.1"}."""
    out = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.startswith("table.") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.split(".", 2)[2] if k.startswith("table.All.") else k.split(".", 1)[1]
        out[k] = v
    if not out:
        raise RuntimeError("the camera returned no configuration (%r)" % text.strip()[:80])
    return out


def managed(cfg: dict[str, str]) -> dict[str, dict[str, str]]:
    """Keep only managed keys, grouped."""
    out: dict[str, dict[str, str]] = {}
    for k, v in cfg.items():
        g = group_of(k)
        if g:
            out.setdefault(g, {})[k] = v
    return out


def parse_kv(text: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# --------------------------------------------------------------------------
# HTTP with digest auth: exactly one authenticated try per request
# --------------------------------------------------------------------------


def _digest_header(chal: str, user: str, password: str, method: str, uri: str, nc: int) -> str:
    f = dict(re.findall(r'(\w+)="?([^",]*)"?', chal))
    realm, nonce = f.get("realm", ""), f.get("nonce", "")
    algo = f.get("algorithm", "MD5")
    if algo.upper() != "MD5":
        raise RuntimeError("unsupported digest algorithm %s" % algo)
    h = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
    ha1, ha2 = h(f"{user}:{realm}:{password}"), h(f"{method}:{uri}")
    cnonce = hashlib.md5(os.urandom(8)).hexdigest()[:16]
    ncs = "%08x" % nc
    if "auth" in f.get("qop", "").split(","):
        resp = h(f"{ha1}:{nonce}:{ncs}:{cnonce}:auth:{ha2}")
        extra = f', qop=auth, nc={ncs}, cnonce="{cnonce}"'
    else:
        resp, extra = h(f"{ha1}:{nonce}:{ha2}"), ""
    hdr = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
           f'response="{resp}", algorithm=MD5{extra}')
    if f.get("opaque"):
        hdr += f', opaque="{f["opaque"]}"'
    return hdr


class _NoRedirect(urllib.request.HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


class Camera:
    def __init__(self, host: str, user: str, password: str, timeout: float = 20):
        self.base = host if "://" in host else "http://" + host
        self.user, self.password, self.timeout = user, password, timeout
        self.nc = 0
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _open(self, path: str, auth: str | None = None):
        req = urllib.request.Request(self.base + path, headers={"Authorization": auth} if auth else {})
        return self.opener.open(req, timeout=self.timeout)

    def get(self, path: str) -> str:
        with self._open(path) as r:
            if r.status == 200:
                return r.read().decode("utf-8", "replace")
            if r.status != 401:
                raise RuntimeError("GET %s: HTTP %s" % (path.split("?")[0], r.status))
            chal = r.headers.get("WWW-Authenticate", "")
        if not chal.lower().startswith("digest"):
            raise RuntimeError("camera did not offer digest auth")
        self.nc += 1
        with self._open(path, _digest_header(chal, self.user, self.password, "GET", path, self.nc)) as r:
            if r.status == 401:
                # One attempt only: the camera locks the account after a few.
                raise RuntimeError("login rejected (HTTP 401); not retrying. Check the password in Vault.")
            if r.status != 200:
                raise RuntimeError("GET %s: HTTP %s" % (path.split("?")[0], r.status))
            return r.read().decode("utf-8", "replace")

    def config(self) -> dict[str, str]:
        return parse_table(self.get(CONFIG_PATH + "?action=getConfig&name=All"))

    def device(self) -> dict[str, str]:
        info = {}
        t = parse_kv(self.get("/cgi-bin/magicBox.cgi?action=getDeviceType"))
        v = parse_kv(self.get("/cgi-bin/magicBox.cgi?action=getSoftwareVersion"))
        if t.get("type"):
            info["type"] = t["type"].strip()
        if v.get("version"):
            info["firmware"] = v["version"].strip()
        return info


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def vault_password(path: str) -> str:
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        raise RuntimeError("VAULT_ADDR is not set and AMCREST_PASSWORD is not in the environment")
    ctx = ssl.create_default_context(cafile=os.environ.get("VAULT_CACERT") or None)
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true"):
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    token = os.environ.get("VAULT_TOKEN")
    if not token:
        jwt = os.environ.get("VAULT_ID_TOKEN")
        if not jwt:
            raise RuntimeError("neither VAULT_TOKEN nor VAULT_ID_TOKEN is set")
        body = json.dumps({"role": os.environ.get("VAULT_JWT_ROLE", "ci-deploy"), "jwt": jwt}).encode()
        req = urllib.request.Request(f"{addr}/v1/auth/jwt/login", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                token = json.load(r)["auth"]["client_token"]
        except (urllib.error.URLError, KeyError, ValueError) as e:
            raise RuntimeError(f"Vault JWT login failed: {type(e).__name__}") from None
    mount, _, rest = path.partition("/")
    req = urllib.request.Request(f"{addr}/v1/{mount}/data/{rest}", headers={"X-Vault-Token": token})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            pw = json.load(r)["data"]["data"].get("password")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Vault read of {path} failed: HTTP {e.code}") from None
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise RuntimeError(f"Vault read of {path} failed: {type(e).__name__}") from None
    if not pw:
        raise RuntimeError(f"Vault secret {path} has no 'password' field")
    return pw


def connect(desired: dict, host: str | None) -> Camera:
    host = host or os.environ.get("AMCREST_HOST") or desired.get("host")
    if not host:
        raise RuntimeError("no host: set host in the file, --host or AMCREST_HOST")
    user = os.environ.get("AMCREST_USER") or desired.get("username") or "admin"
    pw = os.environ.get("AMCREST_PASSWORD")
    if not pw:
        if not desired.get("vault_path"):
            raise RuntimeError("no vault_path in the file and AMCREST_PASSWORD is not set")
        pw = vault_password(desired["vault_path"])
    return Camera(host, user, pw)


# --------------------------------------------------------------------------
# YAML (the small subset these files use: nested maps of quoted strings)
# --------------------------------------------------------------------------

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOP_ORDER = ["schema", "name", "host", "username", "vault_path", "device", "settings"]


def _key(k: str) -> str:
    return k if _PLAIN_KEY.match(k) and k not in ("true", "false", "null", "yes", "no", "on", "off") \
        else json.dumps(k)


def _sort_key(k):
    if k in TOP_ORDER:
        return (0, TOP_ORDER.index(k), "")
    if k in MANAGED:
        return (1, list(MANAGED).index(k), "")
    return (2, 0, k)


def dump_yaml(d: dict, indent: int = 0) -> str:
    out = []
    for k in sorted(d, key=_sort_key):
        v, pad = d[k], "  " * indent
        if isinstance(v, dict):
            out.append("%s%s:" % (pad, _key(k)))
            out.append(dump_yaml(v, indent + 1) if v else pad + "  {}")
        else:
            out.append("%s%s: %s" % (pad, _key(k), json.dumps(str(v))))
    return "\n".join(x for x in out if x != "")


def load_yaml(text: str) -> dict:
    root: dict = {}
    stack = [(-1, root)]
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        ind = len(line) - len(line.lstrip(" "))
        body = line.strip()
        m = re.match(r'^("(?:[^"\\]|\\.)*"|[^:"]+):(?:\s+(.*))?$', body)
        if not m:
            raise ValueError("line %d: cannot parse %r" % (n, line))
        key = json.loads(m.group(1)) if m.group(1).startswith('"') else m.group(1)
        val = m.group(2)
        while stack and stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]
        if val is None:
            parent[key] = {}
            stack.append((ind, parent[key]))
        elif val == "{}":
            parent[key] = {}
        elif val.startswith('"'):
            parent[key] = json.loads(val)
        else:
            parent[key] = val
    return root


HEADER = """\
# Amcrest camera settings, exported by amcrestctl.py (see docs/amcrest.md).
# Only the settings listed in amcrestctl.MANAGED: time/NTP/DST, cloud and
# P2P, outbound push targets, network mode, local services. The password is
# read from vault_path at run time. device is information only.
"""


def read_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return load_yaml(f.read())


def write_file(path: str, d: dict):
    text = HEADER + dump_yaml(d) + "\n"
    if path == "-":
        sys.stdout.write(text)
        return
    with open(path, "w") as f:
        f.write(text)


# --------------------------------------------------------------------------
# Compare / plan
# --------------------------------------------------------------------------


def flat_settings(d: dict) -> dict[str, str]:
    return {k: v for g in (d.get("settings") or {}).values() for k, v in g.items()}


def compare(desired: dict, live: dict[str, str], for_drift: bool = True):
    """-> (changed [(key, want, have)], new [(key, have)], unread [(key, want)])."""
    want = flat_settings(desired)
    have = {k: v for g in managed(live).values() for k, v in g.items()}
    skip = NO_DRIFT if for_drift else set()
    changed = [(k, want[k], have[k]) for k in sorted(want) if k in have and have[k] != want[k] and k not in skip]
    new = [(k, have[k]) for k in sorted(have) if k not in want and k not in skip]
    unread = [(k, want[k]) for k in sorted(want) if k not in have]
    return changed, new, unread


def set_request(key: str, value: str) -> str:
    return CONFIG_PATH + "?action=setConfig&" + urllib.parse.urlencode({key: value}, safe="[]._")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_export(a) -> int:
    old = read_file(a.file)
    cam = connect(old, a.host)
    cfg = cam.config()
    new = {"schema": SCHEMA}
    for k in ("name", "host", "username", "vault_path"):
        if old.get(k):
            new[k] = old[k]
    if a.host:
        new["host"] = a.host
    new["device"] = cam.device()
    new["settings"] = managed(cfg)
    write_file(a.output or a.file, new)
    print("Exported %d managed settings from %s." % (len(flat_settings(new)), cam.base), file=sys.stderr)
    return 0


def drift_one(path: str, host: str | None) -> int:
    desired = read_file(path)
    if not flat_settings(desired):
        print("ERROR    %s is missing or has no settings" % path)
        return 1
    cam = connect(desired, host)
    live = cam.config()
    name = desired.get("name") or os.path.basename(path)
    print("== %s (%s)" % (name, cam.base))
    changed, new, unread = compare(desired, live)
    for k, want, have in changed:
        print("DRIFT    %s [%s]: file %r, live %r" % (k, group_of(k), want, have))
    for k, have in new:
        print("NEW      %s [%s]: live %r (not in the file)" % (k, group_of(k), have))
    for k, want in unread:
        print("UNREAD   %s: file %r, not reported live" % (k, want))
    n = len(changed) + len(new)
    if n:
        print("%d difference(s) between the camera and %s." % (n, os.path.basename(path)))
        print("Intended? Run `amcrestctl.py --file %s export` and commit. If not, `plan` shows "
              "the setConfig requests that restore it." % os.path.basename(path))
        return 2
    print("No drift (%d settings)." % len(flat_settings(desired)))
    return 0


def cmd_drift(a) -> int:
    files = sorted(glob.glob(os.path.join(HERE, "*.yaml"))) if a.all else [a.file]
    rc = 0
    for f in files:
        try:
            r = drift_one(f, None if a.all else a.host)
        except (OSError, RuntimeError, ValueError) as ex:
            print("ERROR    %s: %s" % (os.path.basename(f), ex))
            r = 1
        # an error (1) outranks drift (2): the check did not complete
        if r == 1:
            rc = 1
        elif r == 2 and rc == 0:
            rc = 2
    return rc


def cmd_plan(a) -> int:
    desired = read_file(a.file)
    if not flat_settings(desired):
        print("ERROR: %s is missing or has no settings" % a.file)
        return 1
    cam = connect(desired, a.host)
    changed, new, unread = compare(desired, cam.config(), for_drift=False)
    for k, want, have in changed:
        print("SET      %s  (live %r)" % (set_request(k, want), have))
    for k, have in new:
        print("NOTE     %s = %r on the camera, not in the file (left alone)" % (k, have))
    for k, want in unread:
        print("SKIP     %s: not reported live" % k)
    print("Dry run: nothing was sent (amcrestctl never writes to the camera).")
    return 2 if changed and a.detailed_exitcode else 0


def cmd_check(a) -> int:
    errors = []
    files = sorted(glob.glob(os.path.join(HERE, "*.yaml"))) if a.all else [a.file]
    for f in files:
        d = read_file(f)
        base = os.path.basename(f)
        if d.get("schema") != SCHEMA:
            errors.append("%s: schema is %r, want %r" % (base, d.get("schema"), SCHEMA))
        for k in ("host", "vault_path"):
            if not d.get(k):
                errors.append("%s: no %s" % (base, k))
        unknown_top = set(d) - set(TOP_ORDER)
        if unknown_top:
            errors.append("%s: unknown top-level keys %s" % (base, sorted(unknown_top)))
        settings = d.get("settings") or {}
        for g, kv in settings.items():
            if g not in MANAGED:
                errors.append("%s: unknown group %s" % (base, g))
                continue
            for k in kv:
                if group_of(k) != g:
                    errors.append("%s: %s is not a managed %s setting" % (base, k, g))
        if not errors:
            print("%s: %d settings in %d groups." % (base, len(flat_settings(d)), len(settings)))
    for e in errors:
        print("ERROR    " + e)
    return 1 if errors or not files else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_FILE, help="desired-state YAML (default back-camera.yaml)")
    ap.add_argument("--host", help="override the file's host (or AMCREST_HOST)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("-o", "--output", help="write here instead of --file ('-' = stdout)")
    c = sub.add_parser("check")
    c.add_argument("--all", action="store_true", help="every *.yaml in this directory")
    d = sub.add_parser("drift")
    d.add_argument("--all", action="store_true", help="every *.yaml in this directory")
    p = sub.add_parser("plan")
    p.add_argument("--detailed-exitcode", action="store_true")
    a = ap.parse_args(argv)
    try:
        return {"export": cmd_export, "check": cmd_check, "drift": cmd_drift, "plan": cmd_plan}[a.cmd](a)
    except (OSError, RuntimeError, ValueError) as ex:
        print("ERROR: %s" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
