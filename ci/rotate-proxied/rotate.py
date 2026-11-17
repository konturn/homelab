#!/usr/bin/env python3
"""secrets:rotate-proxied: rotate one secret behind the credential proxy.

  rotate.py check NAME            exit 0 if NAME is on the allowlist, 2 if not
  rotate.py run --service NAME    rotate it (CI only: protected main, vault-admin)

NAME "monthly" means this month's service (service_of_the_month, UTC).

One service per run (docs/SECRET_ROTATION.md, "Proxied services"):

  1. Preflight: the name is on the allowlist (never plex), the value in Vault
     works against the service, the compose consumers run the image main
     declares, and a write to the Vault path succeeds (the rotation marker).
  2. Rotate in the service. A value the job chooses (or receives before it
     is applied) goes to Vault as <field>_next first; a value the service
     generates goes to Vault in the next call after the service returns it.
     Either way the value and the marker "applied" land in ONE KV patch.
  3. Update the copies other apps keep (Ombi's Radarr/Sonarr settings, the
     *arr download clients), redeploy telegraf / compose consumers
     (ansible/rotate-consumers.yml), reload jit-proxy.
  4. Verify: the new value works, the old one is rejected, the dependent
     apps' own connection tests pass, telegraf writes fresh points.
  5. Clear the markers.

Retries are idempotent: a run that finds a marker resumes. "applied" means
Vault and the service already agree, so only steps 3-5 run again; "started"
means the service may or may not have taken the new value, and the run
recovers it from <field>_next or from the service's own storage before doing
anything else. Only the GitLab token cannot be recovered that way (the old
token is revoked by the rotate call and the new one is shown once); the
preflight write and a retried Vault write cover that window.

No value is ever printed: every secret is registered with MASK when it is
read or created, all output goes through log(), which replaces registered
values, and subprocess output (ansible) is filtered the same way. Values
reach the router over SSH stdin, never in argv.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import secrets as pysecrets
import shlex
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent.parent
VAULT_ADDR = os.environ.get("VAULT_ADDR", "https://vault.lab.nkontur.com:8200")
KV_MOUNT = "homelab"
ROUTER = "root@10.4.0.1"
LAB = "lab.nkontur.com"
PIHOLE_IP = "10.3.32.2"

# Plex is rotated by hand and only on suspicion (design 8.2): re-linking
# Ombi, Tautulli and the *arr needs a plex.tv sign-in.
REFUSED = {"plex": "Plex is rotated by hand, only on suspicion (docs/SECRET_ROTATION.md)"}


# --------------------------------------------------------------------------
# Masking and output
# --------------------------------------------------------------------------

class Masker:
    """Replaces every registered value in text. Values shorter than 6
    characters are not registered (masking them would garble the log and
    they are not secrets this job handles)."""

    def __init__(self) -> None:
        self._values: set = set()

    def add(self, value) -> None:
        if not isinstance(value, str) or len(value) < 6:
            return
        for v in (value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value),
                  json.dumps(value)[1:-1]):
            if len(v) >= 6:
                self._values.add(v)

    def scrub(self, text: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):
            if v in text:
                text = text.replace(v, "[MASKED]")
        return text

    def __len__(self) -> int:
        return len(self._values)


MASK = Masker()


def secret(value):
    """Register a value as secret and return it."""
    MASK.add(value)
    return value


def log(msg: str = "") -> None:
    print(MASK.scrub(str(msg)), flush=True)


class RotationError(Exception):
    pass


def fail(msg: str):
    raise RotationError(msg)


def stamp(phase: str) -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + " " + phase


def new_password(n: int = 32) -> str:
    """Letters and digits only: the value ends up in URLs (telegraf's NZBGet
    input), compose environment and JSON."""
    alphabet = string.ascii_letters + string.digits
    return secret("".join(pysecrets.choice(alphabet) for _ in range(n)))


def new_hex(nbytes: int = 16) -> str:
    return secret(pysecrets.token_hex(nbytes))


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------

def validate_service(name) -> str:
    """Return the service name if it is on the allowlist, else raise.
    Exact match only: no case folding, no trimming."""
    if not isinstance(name, str) or not name:
        fail("ROTATE_SERVICE is empty; one of: " + ", ".join(SERVICES))
    if name.strip().lower() in REFUSED:
        fail(f"refused: {REFUSED[name.strip().lower()]}")
    if name not in SERVICES:
        fail(f"refused: {name!r} is not on the allowlist (" + ", ".join(SERVICES) + ")")
    return name


# --------------------------------------------------------------------------
# HTTP (stdlib only). Errors carry method, host+path and status, never a
# query string, header or body.
# --------------------------------------------------------------------------

class HttpError(RotationError):
    pass


def _where(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.netloc}{u.path}"


class Response:
    def __init__(self, status: int, body: bytes, headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def json(self):
        try:
            return json.loads(self.body.decode() or "null")
        except ValueError:
            raise HttpError(f"response is not JSON (HTTP {self.status})")


class Http:
    def __init__(self, cookies: bool = False, timeout: float = 30):
        handlers = [urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())] if cookies else []
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout

    def request(self, method: str, url: str, *, headers=None, body=None, json_body=None,
                content_type: Optional[str] = None) -> Response:
        hdrs = dict(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", content_type or "application/json")
        elif body is not None:
            data = body if isinstance(body, bytes) else str(body).encode()
            if content_type:
                hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return Response(r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read() if e.fp else b"", dict(e.headers or {}))
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HttpError(f"{method} {_where(url)}: {type(e).__name__}: {MASK.scrub(str(reason))}") from None


def expect(resp: Response, ok, what: str) -> Response:
    ok = (ok,) if isinstance(ok, int) else tuple(ok)
    if resp.status not in ok:
        fail(f"{what}: HTTP {resp.status}")
    return resp


# --------------------------------------------------------------------------
# Vault (KV v2 under homelab/), vault-admin JWT role
# --------------------------------------------------------------------------

# Vault fields that are not secrets and stay readable in the log (the NZBGet
# username is the service's name).
NOT_SECRET_FIELDS = {"username"}


class Vault:
    def __init__(self, jwt: str, role: str = "vault-admin", addr: str = VAULT_ADDR, http: Optional[Http] = None):
        self.jwt, self.role, self.addr = jwt, role, addr.rstrip("/")
        self.http = http or Http()
        self.token = None

    def login(self) -> None:
        r = self.http.request("POST", f"{self.addr}/v1/auth/jwt/login",
                              json_body={"role": self.role, "jwt": self.jwt})
        tok = (r.json() or {}).get("auth", {}).get("client_token") if r.status == 200 else None
        if not tok:
            fail(f"Vault JWT login ({self.role}) failed: HTTP {r.status}")
        self.token = secret(tok)

    def _call(self, method: str, path: str, **kw) -> Response:
        for attempt in range(2):
            if not self.token:
                self.login()
            r = self.http.request(method, f"{self.addr}/v1/{KV_MOUNT}/data/{path}",
                                  headers={"X-Vault-Token": self.token}, **kw)
            if r.status == 403 and attempt == 0:
                self.token = None  # the 15-minute token expired: log in again
                continue
            return r
        return r

    def read(self, path: str) -> Dict[str, str]:
        r = self._call("GET", path)
        if r.status != 200:
            fail(f"Vault read {path}: HTTP {r.status}")
        data = (r.json() or {}).get("data", {}).get("data") or {}
        for k, v in data.items():
            if k not in NOT_SECRET_FIELDS:
                secret(v)
        return data

    def patch(self, path: str, fields: Dict[str, Optional[str]], tries: int = 5, sleep=time.sleep) -> int:
        """KV v2 patch (JSON merge patch: other fields are kept, None removes
        a field). Retried: a value the service already took must reach
        Vault."""
        for v in fields.values():
            secret(v)
        delay = 2
        last = None
        for attempt in range(tries):
            try:
                r = self._call("PATCH", path, json_body={"data": fields},
                               content_type="application/merge-patch+json")
                if r.status == 200:
                    return (r.json() or {}).get("data", {}).get("version", 0)
                last = f"HTTP {r.status}"
            except HttpError as e:
                last = str(e)
            if attempt < tries - 1:
                log(f"  Vault patch {path} failed ({last}); retrying in {delay}s")
                sleep(delay)
                delay = min(delay * 2, 30)
        fail(f"Vault patch {path} failed: {last}")


# --------------------------------------------------------------------------
# Router over SSH (values only on stdin)
# --------------------------------------------------------------------------

class Router:
    def __init__(self, key: str = "tmp", host: str = ROUTER):
        self.key, self.host = key, host
        self._tunnels: List[subprocess.Popen] = []

    def _ssh(self) -> List[str]:
        return ["ssh", "-i", self.key, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={REPO / 'ansible' / 'known_hosts'}", "-o", "LogLevel=ERROR"]

    def run(self, command: str, stdin: Optional[str] = None, check: bool = True, timeout: int = 300) -> str:
        p = subprocess.run(self._ssh() + [self.host, command], input=(stdin or "").encode(),
                           capture_output=True, timeout=timeout)
        if check and p.returncode != 0:
            err = MASK.scrub(p.stderr.decode(errors="replace")).strip()[-400:]
            fail(f"router command failed (exit {p.returncode}): {err}")
        return p.stdout.decode(errors="replace")

    def tunnel(self, target: str, port: int) -> int:
        """Local port forward to target:port as seen from the router."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        p = subprocess.Popen(self._ssh() + ["-N", "-o", "ExitOnForwardFailure=yes",
                                            "-L", f"127.0.0.1:{local}:{target}:{port}", self.host],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._tunnels.append(p)
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", local), timeout=1):
                    return local
            except OSError:
                if p.poll() is not None:
                    break
                time.sleep(0.2)
        fail(f"SSH tunnel to {target}:{port} did not come up")

    def close(self) -> None:
        for p in self._tunnels:
            p.terminate()


# --------------------------------------------------------------------------
# Context: everything a rotation touches, replaceable in tests
# --------------------------------------------------------------------------

class Ctx:
    def __init__(self, vault: Vault, router: Router, http: Http, sleep=time.sleep,
                 deploy: Optional[Callable] = None):
        self.vault, self.router, self.http, self.sleep = vault, router, http, sleep
        self._deploy = deploy or self._ansible_deploy
        self.notes: List[str] = []
        self.failures: List[str] = []

    def check(self, ok: bool, what: str) -> None:
        log(f"  [{'ok' if ok else 'FAIL'}] {what}")
        if not ok:
            self.failures.append(what)

    def wait(self, what: str, probe: Callable[[], bool], timeout: float = 120, first: float = 2) -> bool:
        """Poll with exponential backoff (CLAUDE.md), capped at 60s."""
        delay, waited = first, 0.0
        while True:
            try:
                if probe():
                    return True
            except RotationError as e:
                log(f"  ({what}: {e})")
            if waited >= timeout:
                return False
            self.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60)

    # -- consumers -------------------------------------------------------
    def deploy(self, telegraf: bool, services: List[str]) -> None:
        if not telegraf and not services:
            return
        self._deploy(telegraf, services)

    def _ansible_deploy(self, telegraf: bool, services: List[str]) -> None:
        extra = json.dumps({"rotate_telegraf": telegraf, "rotate_services": services})
        cmd = ["ansible-playbook", "-i", "ansible/inventory.yml", "-u", "root",
               "--private-key", self.router.key, "ansible/rotate-consumers.yml", "-e", extra]
        log(f"  ansible-playbook ansible/rotate-consumers.yml (telegraf={telegraf}, services={services})")
        env = dict(os.environ, ANSIBLE_NOCOLOR="1")
        p = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        assert p.stdout is not None
        for line in p.stdout:
            log("    " + line.decode(errors="replace").rstrip())
        if p.wait() != 0:
            fail("consumer deploy (ansible/rotate-consumers.yml) failed")

    def reload_proxy(self, resource: str) -> None:
        since = self.router.run("date -u +%Y-%m-%dT%H:%M:%SZ").strip()
        self.router.run("docker kill -s HUP jit-proxy >/dev/null")
        self.sleep(5)
        out = self.router.run(f"docker logs --since {shlex.quote(since)} jit-proxy 2>&1 | "
                              "grep -c credential_load_failed || true")
        n = int((out.strip() or "0").splitlines()[-1])
        self.check(n == 0, f"jit-proxy reloaded its credentials ({n} load failures)")

    def telegraf_fresh(self, measurement: str, since: str, timeout: float = 480) -> bool:
        flux = (f'from(bucket:"metrics") |> range(start: {since}) '
                f'|> filter(fn: (r) => r._measurement == "{measurement}") '
                '|> keep(columns: ["_time"]) |> limit(n: 1)')
        cmd = f"docker exec influxdb influx query --raw {shlex.quote(flux)}"

        def probe() -> bool:
            rows = [ln for ln in self.router.run(cmd).splitlines()
                    if ln.startswith(",") and not ln.startswith(",result")]
            return bool(rows)
        return self.wait(f"telegraf {measurement}", probe, timeout=timeout, first=15)


# --------------------------------------------------------------------------
# Service clients
# --------------------------------------------------------------------------

def arr_url(name: str) -> str:
    return f"https://{name}.{LAB}"


ARR_API = {"radarr": "v3", "sonarr": "v3", "prowlarr": "v1"}


def arr_status(ctx: Ctx, name: str, key: str) -> Optional[bool]:
    r = ctx.http.request("GET", f"{arr_url(name)}/api/{ARR_API[name]}/system/status",
                         headers={"X-Api-Key": key})
    if r.status == 200:
        return True
    if r.status == 401:
        return False
    fail(f"{name} system/status: HTTP {r.status}")


def arr_live_key(ctx: Ctx, name: str) -> Optional[str]:
    out = ctx.router.run(f"sed -n 's:.*<ApiKey>\\(.*\\)</ApiKey>.*:\\1:p' "
                         f"/persistent_data/application/{name}/config.xml")
    key = out.strip()
    return secret(key) if re.fullmatch(r"[0-9a-f]{32}", key) else None


def arr_reset_key(ctx: Ctx, name: str, old: str) -> str:
    expect(ctx.http.request("POST", f"{arr_url(name)}/api/{ARR_API[name]}/command",
                            headers={"X-Api-Key": old}, json_body={"name": "ResetApiKey"}),
           (200, 201), f"{name} ResetApiKey")
    new: List[str] = []

    def changed() -> bool:
        k = arr_live_key(ctx, name)
        if k and k != old:
            new.append(k)
            return True
        return False
    if not ctx.wait(f"{name} new key in config.xml", changed, timeout=60, first=1):
        fail(f"{name}: the key in config.xml did not change after ResetApiKey")
    return new[0]


OMBI_SETTINGS_DB = "/persistent_data/docker/volumes/docker_ombi/_data/OmbiSettings.db"


class Ombi:
    """Ombi has no lab vhost: reached through an SSH tunnel to its address
    on the internal network (as jit-proxy reaches http://ombi:3579)."""

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self._base = None

    @property
    def base(self) -> str:
        if not self._base:
            ip = self.ctx.router.run("docker inspect -f "
                                     "'{{(index .NetworkSettings.Networks \"docker_internal\").IPAddress}}' ombi").strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
                fail("could not find Ombi's address on the internal network")
            self._base = f"http://127.0.0.1:{self.ctx.router.tunnel(ip, 3579)}"
        return self._base

    def call(self, method: str, path: str, key: str, body=None) -> Response:
        return self.ctx.http.request(method, f"{self.base}{path}", headers={"ApiKey": key}, json_body=body)

    def works(self, key: str) -> bool:
        r = self.call("GET", "/api/v1/Settings/about", key)
        if r.status in (200, 401):
            return r.status == 200
        fail(f"Ombi Settings/about: HTTP {r.status}")

    def live_key(self) -> Optional[str]:
        out = self.ctx.router.run(
            "python3 -c 'import sqlite3,json,sys;"
            f"c=sqlite3.connect(\"file:{OMBI_SETTINGS_DB}?mode=ro\",uri=True);"
            "r=c.execute(\"select Content from GlobalSettings where SettingsName=?\",(\"OmbiSettings\",)).fetchone();"
            "sys.stdout.write(json.loads(r[0]).get(\"ApiKey\") or \"\")'")
        key = out.strip()
        return secret(key) if re.fullmatch(r"[0-9a-f]{32}", key) else None


class Deluge:
    URL = f"https://deluge.{LAB}/json"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.http = Http(cookies=True)
        self._id = 0

    def rpc(self, method: str, *params):
        self._id += 1
        r = expect(self.http.request("POST", self.URL, json_body={"method": method, "params": list(params), "id": self._id}),
                   200, f"deluge {method}")
        body = r.json() or {}
        if body.get("error"):
            fail(f"deluge {method}: {MASK.scrub(str(body['error'].get('message', 'error')))}")
        return body.get("result")


def nzbget_rpc(ctx: Ctx, user: str, password: str, method: str, params=None) -> Response:
    import base64
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    secret(auth)
    return ctx.http.request("POST", f"https://nzbget.{LAB}/jsonrpc",
                            headers={"Authorization": f"Basic {auth}"},
                            json_body={"method": method, "params": params or [], "id": 1})


PIHOLE = f"https://pihole.{LAB}"


def pihole_login(ctx: Ctx, password: str) -> Optional[str]:
    r = ctx.http.request("POST", f"{PIHOLE}/api/auth", json_body={"password": password})
    if r.status == 401:
        return None
    sess = (expect(r, 200, "pihole login").json() or {}).get("session") or {}
    if not sess.get("valid"):
        return None
    return secret(sess.get("sid") or "") or ""


def pihole_logout(ctx: Ctx, sid: str) -> None:
    if sid:
        ctx.http.request("DELETE", f"{PIHOLE}/api/auth", headers={"X-FTL-SID": sid})


def pihole_works(ctx: Ctx, password: str) -> bool:
    sid = pihole_login(ctx, password)
    if sid is None:
        return False
    pihole_logout(ctx, sid)
    return True


GITLAB = f"https://gitlab.{LAB}/api/v4"


def gitlab_self(ctx: Ctx, token: str) -> Optional[dict]:
    r = ctx.http.request("GET", f"{GITLAB}/personal_access_tokens/self", headers={"PRIVATE-TOKEN": token})
    if r.status == 401:
        return None
    return expect(r, 200, "GitLab personal_access_tokens/self").json()


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------

class Secret:
    """One field to rotate. kind:
      chosen       the job has the new value before the service takes it
      generated    the service generates it (recovered from its storage)
      vault_first  config as code: Vault, then the consumer deploy applies it
    """

    def __init__(self, field: str, kind: str):
        self.field, self.kind = field, kind

    @property
    def marker(self) -> str:
        return f"{self.field}_rotation"

    @property
    def pending(self) -> str:
        return f"{self.field}_next"


class Service:
    name = ""
    path = ""
    secrets: List[Secret] = []
    resource: Optional[str] = None       # jit-proxy resource
    telegraf: List[str] = []            # measurements that prove the new value
    compose: List[str] = []             # containers recreated from compose

    # -- per secret ---------------------------------------------------------
    def works(self, ctx: Ctx, s: Secret, value: str, data: dict) -> bool:
        raise NotImplementedError

    def apply(self, ctx: Ctx, s: Secret, old: str, new: str, data: dict) -> None:
        """chosen: make the service accept `new`."""
        raise NotImplementedError

    def choose(self, ctx: Ctx, s: Secret, data: dict) -> str:
        return new_password()

    def generate(self, ctx: Ctx, s: Secret, old: str, data: dict) -> str:
        """generated: have the service replace `old`; return the new value."""
        raise NotImplementedError

    def live(self, ctx: Ctx, s: Secret, data: dict) -> Optional[str]:
        """generated: the value the service holds now, from its storage."""
        return None

    # -- after all secrets ----------------------------------------------------
    def links(self, ctx: Ctx, data: dict) -> None:
        """Update and test the copies other apps keep."""

    def verify_extra(self, ctx: Ctx, data: dict) -> None:
        pass


class Tautulli(Service):
    name, path, resource = "tautulli", "docker/tautulli", "tautulli"
    secrets = [Secret("api_key", "chosen")]
    telegraf = ["tautulli_activity"]
    CONFIG = "/persistent_data/docker/volumes/docker_tautulli_config/_data/config.ini"

    def works(self, ctx, s, value, data):
        q = urllib.parse.urlencode({"apikey": value, "cmd": "get_server_friendly_name"})
        r = ctx.http.request("GET", f"https://tautulli.{LAB}/api/v2?{q}")
        if r.status in (200, 401):
            return r.status == 200 and ((r.json() or {}).get("response") or {}).get("result") == "success"
        fail(f"tautulli api/v2: HTTP {r.status}")

    def choose(self, ctx, s, data):
        return new_hex(16)

    def apply(self, ctx, s, old, new, data):
        # Tautulli has no API to set its key, and its settings page needs the
        # web login. It writes config.ini on shutdown, so: stop, edit, start.
        edit = ("import os,re,sys;p=sys.argv[1];k=sys.stdin.readline().strip();"
                "assert re.fullmatch('[0-9a-f]{32}',k);s=open(p).read();"
                "t,n=re.subn('(?m)^api_key = .*$','api_key = '+k,s);assert n==1;"
                "open(p+'.rot','w').write(t);st=os.stat(p);os.chown(p+'.rot',st.st_uid,st.st_gid);"
                "os.chmod(p+'.rot',st.st_mode);os.replace(p+'.rot',p)")
        ctx.router.run("set -e; trap 'docker start tautulli >/dev/null' EXIT; "
                       "docker stop -t 30 tautulli >/dev/null; "
                       f"python3 -c {shlex.quote(edit)} {shlex.quote(self.CONFIG)}", stdin=new + "\n")
        if not ctx.wait("tautulli back up with the new key", lambda: self.works(ctx, s, new, data), timeout=120, first=3):
            fail("tautulli does not accept the new key after restart")


class Arr(Service):
    secrets = [Secret("api_key", "generated")]

    def works(self, ctx, s, value, data):
        return arr_status(ctx, self.name, value)

    def generate(self, ctx, s, old, data):
        return arr_reset_key(ctx, self.name, old)

    def live(self, ctx, s, data):
        return arr_live_key(ctx, self.name)


class Prowlarr(Arr):
    # No other app holds this key: Prowlarr has no applications, and the
    # *arr indexers point at Jackett and NZBgeek (checked 2026-11-16).
    name, path, resource = "prowlarr", "docker/prowlarr", "prowlarr"


class OmbiSvc(Service):
    name, path, resource = "ombi", "docker/ombi", "ombi"
    secrets = [Secret("api_key", "generated")]

    def _o(self, ctx) -> Ombi:
        if not hasattr(ctx, "_ombi"):
            ctx._ombi = Ombi(ctx)
        return ctx._ombi

    def works(self, ctx, s, value, data):
        return self._o(ctx).works(value)

    def generate(self, ctx, s, old, data):
        r = expect(self._o(ctx).call("POST", "/api/v1/Settings/Ombi/resetApi", old), 200, "Ombi resetApi")
        new = r.json()
        if not isinstance(new, str) or not re.fullmatch(r"[0-9a-f]{32}", new):
            fail("Ombi resetApi returned no key")
        return secret(new)

    def live(self, ctx, s, data):
        return self._o(ctx).live_key()


class ArrWithOmbi(Arr):
    """Radarr / Sonarr: Ombi keeps a copy of the key."""

    def links(self, ctx, data):
        new = data["api_key"]
        ombi_key = secret(ctx.vault.read("docker/ombi")["api_key"])
        o = OmbiSvc()._o(ctx)
        r = expect(o.call("GET", f"/api/v1/Settings/{self.name}", ombi_key), 200, f"Ombi get {self.name} settings")
        settings = r.json()
        inner = settings.get(self.name) if self.name == "radarr" else settings
        if not inner or not inner.get("enabled"):
            ctx.notes.append(f"Ombi's {self.name} integration is disabled; nothing to update")
            return
        if inner.get("ip") != self.name:
            fail(f"Ombi's {self.name} settings point at {inner.get('ip')!r}, expected {self.name!r}")
        inner["apiKey"] = new
        ok = expect(o.call("POST", f"/api/v1/Settings/{self.name}", ombi_key, settings), 200,
                    f"Ombi save {self.name} settings").json()
        ctx.check(ok is True, f"Ombi saved the new {self.name} key")
        res = expect(o.call("POST", f"/api/v1/tester/{self.name}", ombi_key, inner), 200,
                     f"Ombi {self.name} test").json() or {}
        ctx.check(bool(res.get("isValid")), f"Ombi's {self.name} connection test")


class Radarr(ArrWithOmbi):
    name, path, resource = "radarr", "docker/radarr", "radarr"
    telegraf = ["radarr_status"]


class Sonarr(ArrWithOmbi):
    name, path, resource = "sonarr", "docker/sonarr", "sonarr"
    telegraf = ["sonarr_status"]


def client_test_result(r: Response):
    """(passed, detail) for an *arr download client test response. Only a
    clean pass counts: every Radarr/Sonarr client test passes with no
    warnings (checked 2026-11-17), so any flagged setting is a failure."""
    if r.status == 200:
        return True, "HTTP 200"
    if r.status != 400:
        return False, f"HTTP {r.status}"
    try:
        failures = r.json() or []
    except RotationError:
        return False, "HTTP 400"
    if not isinstance(failures, list) or not failures:
        return False, "HTTP 400"
    props = [str(f.get("propertyName") or "") for f in failures if isinstance(f, dict)]
    return False, "HTTP 400: " + ", ".join(sorted(set(p or "(connection)" for p in props)))


def update_arr_download_clients(ctx: Ctx, implementation: str, host: str, fields: Dict[str, str]) -> None:
    """Set fields (password) on every Radarr/Sonarr download client of that
    implementation and host, save, and run the client's own test.
    (Lidarr and Prowlarr have no download clients, checked 2026-11-16.)"""
    found = 0
    for arr in ("radarr", "sonarr"):
        key = secret(ctx.vault.read(f"docker/{arr}")["api_key"])
        base = f"{arr_url(arr)}/api/v3/downloadclient"
        clients = expect(ctx.http.request("GET", base, headers={"X-Api-Key": key}), 200,
                         f"{arr} downloadclient list").json() or []
        for c in clients:
            vals = {f.get("name"): f.get("value") for f in c.get("fields", [])}
            if c.get("implementation") != implementation or vals.get("host") != host:
                continue
            found += 1
            for f in c["fields"]:
                if f.get("name") in fields:
                    f["value"] = fields[f["name"]]
            expect(ctx.http.request("PUT", f"{base}/{c['id']}?forceSave=true", headers={"X-Api-Key": key},
                                    json_body=c), (200, 202), f"{arr} save download client {c.get('name')}")
            r = ctx.http.request("POST", f"{base}/test", headers={"X-Api-Key": key}, json_body=c)
            ok, detail = client_test_result(r)
            ctx.check(ok, f"{arr} download client '{c.get('name')}' test ({detail})")
    if not found:
        ctx.notes.append(f"no Radarr/Sonarr download client uses {host}")


class Nzbget(Service):
    name, path, resource = "nzbget", "docker/nzbget", "nzbget"
    secrets = [Secret("password", "chosen")]
    telegraf = ["nzbget"]
    CONFIG = "/persistent_data/application/nzbget/nzbget.conf"

    def works(self, ctx, s, value, data):
        r = nzbget_rpc(ctx, data["username"], value, "version")
        if r.status in (200, 401):
            return r.status == 200
        fail(f"nzbget version: HTTP {r.status}")

    def apply(self, ctx, s, old, new, data):
        # The web UI saves the whole option list (saveconfig drops options it
        # is not sent); changing one line and asking NZBGet to reload is the
        # smaller change.
        edit = ("import os,re,sys;p=sys.argv[1];k=sys.stdin.readline().strip();"
                "assert re.fullmatch('[A-Za-z0-9]{16,}',k);s=open(p).read();"
                "t,n=re.subn('(?m)^ControlPassword=.*$','ControlPassword='+k,s);assert n==1;"
                "open(p+'.rot','w').write(t);st=os.stat(p);os.chown(p+'.rot',st.st_uid,st.st_gid);"
                "os.chmod(p+'.rot',st.st_mode);os.replace(p+'.rot',p)")
        ctx.router.run(f"python3 -c {shlex.quote(edit)} {shlex.quote(self.CONFIG)}", stdin=new + "\n")
        r = nzbget_rpc(ctx, data["username"], old, "reload")
        expect(r, 200, "nzbget reload")
        if not ctx.wait("nzbget back with the new password", lambda: self.works(ctx, s, new, data), timeout=120, first=3):
            fail("nzbget does not accept the new password after reload")

    def links(self, ctx, data):
        update_arr_download_clients(ctx, "Nzbget", f"nzbget.{LAB}", {"password": data["password"]})


class DelugeSvc(Service):
    name, path, resource = "deluge", "docker/deluge", "deluge"
    secrets = [Secret("password", "chosen")]
    telegraf = ["deluge"]

    def works(self, ctx, s, value, data):
        return Deluge(ctx).rpc("auth.login", value) is True

    def apply(self, ctx, s, old, new, data):
        d = Deluge(ctx)
        if d.rpc("auth.login", old) is not True:
            fail("deluge: login with the current password failed")
        if d.rpc("auth.change_password", old, new) is not True:
            fail("deluge: auth.change_password refused")

    def links(self, ctx, data):
        update_arr_download_clients(ctx, "Deluge", f"deluge.{LAB}", {"password": data["password"]})


class Pihole(Service):
    """Two secrets: the app password (api_token; jit-proxy and telegraf) set
    through the API, then the web password (password), which compose sets
    from Vault (FTLCONF_webserver_api_password, read-only in the UI): Vault,
    then recreate the container. DNS is down while it restarts."""
    name, path, resource = "pihole", "infrastructure/pihole", "pihole"
    secrets = [Secret("api_token", "chosen"), Secret("password", "vault_first")]
    telegraf = ["pihole"]
    compose = ["pihole"]

    def works(self, ctx, s, value, data):
        return pihole_works(ctx, value)

    def choose(self, ctx, s, data):
        if s.field == "password":
            return new_password()
        sid = pihole_login(ctx, data["password"])
        if not sid:
            fail("pihole: login with the web password from Vault failed")
        try:
            app = (expect(ctx.http.request("GET", f"{PIHOLE}/api/auth/app", headers={"X-FTL-SID": sid}),
                          200, "pihole auth/app").json() or {}).get("app") or {}
        finally:
            pihole_logout(ctx, sid)
        if not app.get("password") or not app.get("hash"):
            fail("pihole auth/app returned no app password")
        secret(app["password"])
        self._hash = secret(app["hash"])
        return app["password"]

    def apply(self, ctx, s, old, new, data):
        sid = pihole_login(ctx, data["password"])
        if not sid:
            fail("pihole: login with the web password from Vault failed")
        try:
            expect(ctx.http.request("PATCH", f"{PIHOLE}/api/config", headers={"X-FTL-SID": sid},
                                    json_body={"config": {"webserver": {"api": {"app_pwhash": self._hash}}}}),
                   200, "pihole set app password")
        finally:
            pihole_logout(ctx, sid)

    def verify_extra(self, ctx, data):
        out = ctx.router.run(f"dig +short +time=3 +tries=2 @{PIHOLE_IP} gitlab.{LAB} A; "
                             f"dig +short +time=3 +tries=2 @{PIHOLE_IP} one.one.one.one A", check=False)
        ctx.check(len([ln for ln in out.split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ln)]) >= 2,
                  "Pi-hole answers DNS (lab and external name)")


class GitlabAdminToken(Service):
    """The root PAT jit-approval-svc uses (GITLAB_ADMIN_TOKEN and
    GITLAB_AGENT_ADMIN_TOKEN). /self/rotate revokes the old token at once;
    the new one is written to Vault in the next call and jit-approval-svc is
    recreated right after. The expiry date is kept."""
    name, path = "gitlab-admin-token", "docker/jit-approval-svc"
    secrets = [Secret("gitlab_admin_token", "generated")]
    compose = ["jit-approval-svc"]

    def works(self, ctx, s, value, data):
        info = gitlab_self(ctx, value)
        return bool(info and info.get("active") and not info.get("revoked"))

    def generate(self, ctx, s, old, data):
        gitlab_self(ctx, old) or fail("GitLab token is not valid")
        # A year from now (GitLab's limit is 365 days): the monthly schedule
        # comes back to this token every len(MONTHLY_ORDER) months, well
        # before then. Keeping the old date would let it lapse in between.
        expires = (dt.date.today() + dt.timedelta(days=364)).isoformat()
        body = urllib.parse.urlencode({"expires_at": expires})
        r = expect(ctx.http.request("POST", f"{GITLAB}/personal_access_tokens/self/rotate",
                                    headers={"PRIVATE-TOKEN": old}, body=body,
                                    content_type="application/x-www-form-urlencoded"),
                   (200, 201), "GitLab token rotate")
        tok = (r.json() or {}).get("token")
        if not tok:
            fail("GitLab rotate returned no token")
        log(f"  new token expires {expires}")
        return secret(tok)


SERVICES: Dict[str, Callable[[], Service]] = {
    "tautulli": Tautulli,
    "prowlarr": Prowlarr,
    "ombi": OmbiSvc,
    "radarr": Radarr,
    "sonarr": Sonarr,
    "nzbget": Nzbget,
    "deluge": DelugeSvc,
    "pihole": Pihole,
    "gitlab-admin-token": GitlabAdminToken,
}

# Monthly schedule (ROTATE_SERVICE=monthly): one service per month, in this
# order, starting with MONTHLY_START (year, month). Every allowlisted service,
# nothing else (test_rotate.py checks). The GitLab token goes first: its
# expiry date must be renewed before it lapses (GitlabAdminToken.generate).
MONTHLY = "monthly"
MONTHLY_START = (2026, 12)
MONTHLY_ORDER = ("gitlab-admin-token", "tautulli", "prowlarr", "ombi", "radarr", "sonarr",
                 "nzbget", "deluge", "pihole")


def service_of_the_month(today: Optional[dt.date] = None) -> str:
    d = today or dt.datetime.now(dt.timezone.utc).date()
    n = (d.year - MONTHLY_START[0]) * 12 + (d.month - MONTHLY_START[1])
    return MONTHLY_ORDER[n % len(MONTHLY_ORDER)]


# --------------------------------------------------------------------------
# The rotation
# --------------------------------------------------------------------------

def compose_image(service: str) -> Optional[str]:
    text = (REPO / "docker" / "docker-compose.yml").read_text()
    m = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  [A-Za-z0-9_.-]+:\n|\Z)", text)
    if not m:
        return None
    im = re.search(r"(?m)^    image:\s*(\S+)", m.group(1))
    return im.group(1) if im else None


def preflight_compose(ctx: Ctx, svc: Service) -> None:
    """A compose consumer is recreated with main's compose file: refuse when
    the running container is not on the image main declares (an undeployed
    change would ride along)."""
    for c in svc.compose:
        want = compose_image(c)
        have = ctx.router.run(f"docker inspect -f '{{{{.Config.Image}}}}' {shlex.quote(c)}").strip()
        if not want or "{{" in want:
            fail(f"cannot read {c}'s image from docker/docker-compose.yml")
        if have != want:
            fail(f"{c} runs {have}, main declares {want}: deploy main first (router:deploy)")


def rotate(svc: Service, ctx: Ctx) -> int:
    log(f"=== secrets:rotate-proxied: {svc.name} (Vault {KV_MOUNT}/{svc.path}) ===")
    data = ctx.vault.read(svc.path)
    old: Dict[str, Optional[str]] = {}
    resumed = False

    preflight_compose(ctx, svc)

    for s in svc.secrets:
        cur = data.get(s.field)
        if not cur:
            fail(f"{svc.path} has no field {s.field}")
        phase = (data.get(s.marker) or "").split(" ")[-1]
        log(f"--- {s.field} ({s.kind}){': resuming, marker ' + phase if phase else ''}")

        if phase == "applied":
            # Vault and the service agreed when the marker was set; the
            # consumers and checks below run again.
            resumed = True
            old[s.field] = None
            continue

        if phase == "started":
            if svc.works(ctx, s, cur, data):
                log("  the service still accepts the value in Vault: the previous run stopped before changing it")
            else:
                cand = data.get(s.pending) or (svc.live(ctx, s, data) if s.kind == "generated" else None)
                if cand and svc.works(ctx, s, cand, data):
                    ctx.vault.patch(svc.path, {s.field: cand, s.pending: None, s.marker: stamp("applied")})
                    data[s.field] = cand
                    data.pop(s.pending, None)
                    log("  recovered the value the service holds and wrote it to Vault")
                    resumed = True
                    old[s.field] = None
                    continue
                fail(f"{s.field}: the service rejects the value in Vault and it could not be recovered; "
                     "see docs/SECRET_ROTATION.md, 'Recovering an interrupted rotation'")
        elif not svc.works(ctx, s, cur, data):
            fail(f"{s.field}: the service rejects the value in Vault before any change; not rotating")

        # Preflight write: proves the Vault write works before the service changes.
        ctx.vault.patch(svc.path, {s.marker: stamp("started")})
        old[s.field] = cur

        if s.kind == "chosen":
            new = svc.choose(ctx, s, data)
            ctx.vault.patch(svc.path, {s.pending: new})
            svc.apply(ctx, s, cur, new, data)
            ctx.vault.patch(svc.path, {s.field: new, s.pending: None, s.marker: stamp("applied")})
        elif s.kind == "generated":
            new = svc.generate(ctx, s, cur, data)
            ctx.vault.patch(svc.path, {s.field: new, s.marker: stamp("applied")}, tries=8)
        elif s.kind == "vault_first":
            new = svc.choose(ctx, s, data)
            ctx.vault.patch(svc.path, {s.field: new, s.marker: stamp("applied")})
        else:
            fail(f"unknown kind {s.kind}")
        data[s.field] = new
        log(f"  {s.field}: rotated, Vault updated")

    # -- consumers ------------------------------------------------------------
    log("--- consumers")
    svc.links(ctx, data)
    since = ctx.router.run("date -u +%Y-%m-%dT%H:%M:%SZ").strip()
    ctx.deploy(bool(svc.telegraf), list(svc.compose))
    if svc.resource:
        ctx.reload_proxy(svc.resource)

    # -- verification -----------------------------------------------------------
    log("--- verification")
    for s in svc.secrets:
        ok = ctx.wait(f"{s.field} accepted", lambda s=s: svc.works(ctx, s, data[s.field], data), timeout=90)
        ctx.check(ok, f"{svc.name} accepts the new {s.field}")
        if old.get(s.field):
            ctx.check(not svc.works(ctx, s, old[s.field], data), f"{svc.name} rejects the old {s.field}")
        else:
            ctx.notes.append(f"{s.field}: resumed run, old value not known here, rejection not re-checked")
    svc.verify_extra(ctx, data)
    for m in svc.telegraf:
        ctx.check(ctx.telegraf_fresh(m, since), f"telegraf wrote '{m}' points after the redeploy")

    if ctx.failures:
        log(f"=== {svc.name}: {len(ctx.failures)} check(s) failed; markers kept, re-run resumes ===")
        for f in ctx.failures:
            log(f"  - {f}")
        return 1

    clear: Dict[str, Optional[str]] = {}
    for s in svc.secrets:
        clear[s.marker] = None
        clear[s.pending] = None
    ctx.vault.patch(svc.path, clear)
    log(f"=== {svc.name}: rotated{' (resumed run)' if resumed else ''} and verified ===")
    for n in ctx.notes:
        log(f"  note: {n}")
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("service")
    r = sub.add_parser("run")
    r.add_argument("--service", default=os.environ.get("ROTATE_SERVICE", ""))
    r.add_argument("--private-key", default="tmp")
    a = p.parse_args(argv)
    os.umask(0o077)
    try:
        service = a.service
        if service == MONTHLY:
            service = service_of_the_month()
            log(f"monthly: {service} is this month's service")
        name = validate_service(service)
        if a.cmd == "check":
            log(f"{name}: allowed")
            return 0
        if os.environ.get("CI_COMMIT_REF_PROTECTED") != "true" or os.environ.get("CI_COMMIT_BRANCH") != "main":
            fail("runs only in a pipeline on protected main")
        jwt = os.environ.get("VAULT_ID_TOKEN") or fail("VAULT_ID_TOKEN is not set")
        secret(jwt)
        router = Router(key=a.private_key)
        ctx = Ctx(Vault(jwt), router, Http())
        try:
            return rotate(SERVICES[name](), ctx)
        finally:
            router.close()
    except RotationError as e:
        log(f"ERROR: {e}")
        return 2 if "refused" in str(e) or "ROTATE_SERVICE" in str(e) else 1
    except Exception as e:  # never let a traceback carry a value
        log(f"ERROR: {type(e).__name__}: {MASK.scrub(str(e))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
