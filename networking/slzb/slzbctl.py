#!/usr/bin/env python3
"""Read-only config export and drift check for the SMLIGHT SLZB-MR1 coordinator.

The desired state is ``slzb-mr1.yaml`` in this directory: the coordinator's
settings as its local web UI reads them (``/api2?action=0&page=N``, values
in the ``respValuesArr`` response header), plus the firmware versions from
``/ha_info`` and a few switches from ``/ha_sensors``. Python stdlib only.
See docs/slzb.md.

Subcommands
  export   read the live settings and write them to the YAML file (or stdout)
  drift    compare the live settings with the YAML file; exit 2 on drift
  check    offline: the YAML parses and holds nothing that looks secret

Strictly read-only: the tool sends only GET requests to /api2?action=0
(the UI's "get page" call), /ha_info and /ha_sensors. It has no apply and
never calls the save, restart, update or flash endpoints.

The web API needs a login (HTTP basic auth): SLZB_USER / SLZB_PASSWORD, else
the username and password fields of the file's vault_path (VAULT_ADDR plus
VAULT_TOKEN, or VAULT_ID_TOKEN for a JWT login with VAULT_JWT_ROLE, default
"ci-deploy"). A rejected login is never retried.

Exit codes: 0 ok / no differences, 1 error, 2 drift.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_FILE = os.path.join(HERE, "slzb-mr1.yaml")
SCHEMA = "1"
MAX_PAGE = 30

# Page number -> section name. Pages that answer with settings but are not
# listed here are kept as page_<n>.
PAGES = {
    1: "mode",           # coordinator mode, TCP ports, baud, USB mode, Wi-Fi bridge
    2: "network",        # Ethernet / Wi-Fi addressing
    3: "serial",         # per-radio serial: baud, flow control, packets, multi-client
    4: "security",       # firewall IP, AP password on/off (login and passwords dropped)
    5: "vpn",            # WireGuard endpoint (keys dropped)
    6: "general",        # hostname, send anonymous statistics
    7: "auto_update",    # automatic firmware check
    8: "night_mode",     # LED night-mode hours
    9: "time",           # time zone, NTP servers
    10: "syslog",
    15: "zigbee",        # router-mode channel/power, debug levels (PAN IDs dropped)
    17: "mqtt",          # MQTT discovery
    18: "ddns",
}
# Every page repeats these status fields; none is a setting.
COMMON = {"DEVICE", "DEVICE_NAME", "BUILD_DATE", "current-time", "uptime", "zbStatus", "zbConnT",
          "zbConn2", "lng_0", "H_modeUsb", "H_serial2"}
# Page 0 is the status dashboard: only these are settings.
PAGE0_KEEP = {"ZbAutoUpd", "coordMode", "usbMode", "ipv6", "ethDhcp", "radioMode", "radioMode2",
              "wifiMode", "wifiApOn", "vpnE"}
# /ha_sensors: only these are settings (the rest are temperatures, uptimes, RAM;
# night_mode is left out: it may be the current LED state, not the setting).
SENSORS_KEEP = {"disable_leds", "auto_zigbee", "vpn_enabled"}
# Runtime values that look like settings.
VOLATILE = {"curDatetime", "zbHw", "zbHw2"}
# Never written to the file: credentials, keys, hardware addresses, network
# identifiers (the repo is mirrored publicly).
SECRET = re.compile(r"pass|key|secret|token|psk|login|user|mac|panid", re.I)


def norm(v) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def page_settings(page: int, values: dict) -> dict[str, str]:
    out = {}
    for k, v in values.items():
        if k in COMMON or k in VOLATILE or SECRET.search(k):
            continue
        if page == 0 and k not in PAGE0_KEEP:
            continue
        out[k] = norm(v)
    if page == 9:
        # The time zone is a radio group: the selected POSIX TZ string is a key
        # with the value true. Keep it as one setting.
        tz = [k for k, v in out.items() if v == "true" and k != "use12h"]
        for k in tz:
            del out[k]
        out["timezone"] = ",".join(sorted(tz))
    return out


def is_secret_key(k: str) -> bool:
    return bool(SECRET.search(k))


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Coordinator:
    def __init__(self, host: str, timeout: float = 10, user: str | None = None, password: str | None = None):
        self.base = host if "://" in host else "http://" + host
        self.timeout = timeout
        self.headers = {}
        if user and password:
            tok = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
            self.headers["Authorization"] = "Basic " + tok

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path, method="GET", headers=self.headers)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                # one attempt only; no other user or password is tried
                raise RuntimeError("login rejected (HTTP 401); not retrying. Check the credentials in Vault.") \
                    from None
            raise

    def json(self, path: str) -> dict:
        with self._get(path) as r:
            return json.load(r)

    def page(self, n: int) -> dict | None:
        """The page's respValuesArr, or None when the page does not exist."""
        try:
            with self._get("/api2?action=0&page=%d" % n) as r:
                hdr = r.headers.get("respValuesArr")
                r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 500):
                return None
            raise RuntimeError("page %d: HTTP %s" % (n, e.code)) from None
        if hdr is None:
            return None
        return json.loads(hdr)

    def read(self) -> dict:
        info = self.json("/ha_info").get("Info", {})
        device = {"model": norm(info.get("model", "")), "firmware": norm(info.get("sw_version", "")),
                  "fw_channel": norm(info.get("fw_channel", ""))}
        for r in info.get("radios") or []:
            device["radio%s" % r.get("chip_index")] = "%s %s" % (r.get("zb_hw"), r.get("zb_version"))
        settings = {}
        missing = 0
        for n in range(0, MAX_PAGE + 1):
            vals = self.page(n)
            if vals is None:
                missing += 1
                if missing >= 3 and n > max(PAGES):
                    break
                continue
            s = page_settings(n, vals)
            if s:
                settings["status" if n == 0 else PAGES.get(n, "page_%d" % n)] = s
        sens = self.json("/ha_sensors").get("Sensors", {})
        settings["sensors"] = {k: norm(v) for k, v in sens.items() if k in SENSORS_KEEP}
        if not settings.get("mode"):
            raise RuntimeError("the coordinator returned no settings (page 1 empty)")
        return {"device": device, "settings": settings}


# --------------------------------------------------------------------------
# YAML (nested maps of quoted strings)
# --------------------------------------------------------------------------

_PLAIN_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOP_ORDER = ["schema", "name", "host", "vault_path", "device", "settings"]
SECTION_ORDER = ["status", *PAGES.values(), "sensors"]


def _key(k: str) -> str:
    return k if _PLAIN_KEY.match(k) and k not in ("true", "false", "null", "yes", "no", "on", "off") \
        else json.dumps(k)


def _sort_key(k):
    if k in TOP_ORDER:
        return (0, TOP_ORDER.index(k), "")
    if k in SECTION_ORDER:
        return (1, SECTION_ORDER.index(k), "")
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
        m = re.match(r'^("(?:[^"\\]|\\.)*"|[^:"]+):(?:\s+(.*))?$', line.strip())
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
# SMLIGHT SLZB-MR1 settings, exported by slzbctl.py (see docs/slzb.md).
# Read-only snapshot of what the local web UI reads (/api2 pages, /ha_info,
# /ha_sensors). Credentials, keys, MAC addresses and PAN IDs are left out.
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
# Compare and commands
# --------------------------------------------------------------------------


def flat(d: dict) -> dict[tuple, str]:
    out = {}
    for sec in ("device", "settings"):
        for k, v in (d.get(sec) or {}).items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out[(sec, k, kk)] = vv
            else:
                out[(sec, k)] = v
    return out


def compare(desired: dict, live: dict):
    """-> (changed [(path, want, have)], new [(path, have)], unread [(path, want)])."""
    w, h = flat(desired), flat(live)
    changed = [(p, w[p], h[p]) for p in sorted(w) if p in h and h[p] != w[p]]
    new = [(p, h[p]) for p in sorted(h) if p not in w]
    unread = [(p, w[p]) for p in sorted(w) if p not in h]
    return changed, new, unread


def fmt(p: tuple) -> str:
    return ".".join(p)


def vault_creds(path: str) -> tuple[str, str]:
    addr = os.environ.get("VAULT_ADDR")
    if not addr:
        raise RuntimeError("VAULT_ADDR is not set and SLZB_USER/SLZB_PASSWORD are not in the environment")
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
            data = json.load(r)["data"]["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Vault read of {path} failed: HTTP {e.code}") from None
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise RuntimeError(f"Vault read of {path} failed: {type(e).__name__}") from None
    if not data.get("username") or not data.get("password"):
        raise RuntimeError(f"Vault secret {path} lacks username/password")
    return data["username"], data["password"]


def coordinator(desired: dict, host: str) -> Coordinator:
    user, pw = os.environ.get("SLZB_USER"), os.environ.get("SLZB_PASSWORD")
    if not (user and pw) and desired.get("vault_path"):
        user, pw = vault_creds(desired["vault_path"])
    return Coordinator(host, user=user, password=pw)


def host_of(desired: dict, a) -> str:
    host = a.host or os.environ.get("SLZB_HOST") or desired.get("host")
    if not host:
        raise RuntimeError("no host: set host in the file, --host or SLZB_HOST")
    return host


def cmd_export(a) -> int:
    old = read_file(a.file)
    host = host_of(old, a)
    live = coordinator(old, host).read()
    new = {"schema": SCHEMA, "name": old.get("name") or "slzb-mr1", "host": a.host or old.get("host") or host,
           **({"vault_path": old["vault_path"]} if old.get("vault_path") else {}), **live}
    write_file(a.output or a.file, new)
    print("Exported %d values from %s." % (len(flat(new)), host), file=sys.stderr)
    return 0


def cmd_drift(a) -> int:
    desired = read_file(a.file)
    if not desired.get("settings"):
        print("ERROR    %s is missing or has no settings" % a.file)
        return 1
    host = host_of(desired, a)
    changed, new, unread = compare(desired, coordinator(desired, host).read())
    print("== %s (%s)" % (desired.get("name", "slzb"), host))
    for p, want, have in changed:
        print("DRIFT    %s: file %r, live %r" % (fmt(p), want, have))
    for p, have in new:
        print("NEW      %s: live %r (not in the file)" % (fmt(p), have))
    for p, want in unread:
        print("UNREAD   %s: file %r, not reported live" % (fmt(p), want))
    n = len(changed) + len(new)
    if n:
        print("%d difference(s) between the coordinator and %s." % (n, os.path.basename(a.file)))
        print("Intended (a firmware update, a setting changed in the UI)? Run `slzbctl.py export` and commit.")
        return 2
    print("No drift (%d values)." % len(flat(desired)))
    return 0


def cmd_check(a) -> int:
    d = read_file(a.file)
    errors = []
    if d.get("schema") != SCHEMA:
        errors.append("schema is %r, want %r" % (d.get("schema"), SCHEMA))
    if not d.get("host"):
        errors.append("no host")
    if not (d.get("settings") or {}).get("mode"):
        errors.append("no settings.mode")
    if not d.get("vault_path"):
        errors.append("no vault_path (the web API needs a login)")
    for p in flat(d):
        if any(is_secret_key(x) for x in p[1:]):
            errors.append("%s looks like a credential or identifier; it must not be in the file" % fmt(p))
    for e in errors:
        print("ERROR    " + e)
    if not errors:
        print("%s: %d values." % (os.path.basename(a.file), len(flat(d))))
    return 1 if errors else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_FILE, help="desired-state YAML")
    ap.add_argument("--host", help="override the file's host (or SLZB_HOST)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("-o", "--output", help="write here instead of --file ('-' = stdout)")
    sub.add_parser("drift")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    try:
        return {"export": cmd_export, "drift": cmd_drift, "check": cmd_check}[a.cmd](a)
    except (OSError, RuntimeError, ValueError) as ex:
        print("ERROR: %s" % ex)
        return 1


if __name__ == "__main__":
    sys.exit(main())
