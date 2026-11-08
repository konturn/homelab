#!/usr/bin/env python3
"""Throwaway canary containers for critical services (docs/CANARY.md).

Boots the candidate image with the candidate config of one service in an
isolated, throwaway container, waits for the service's own readiness checks,
collects logs and tears everything down. Nothing from production is mounted
read-write, nothing is attached to a production network, and the canary
network has no route anywhere (Docker `--internal`, bridge br-canary, dropped
in INPUT by networking/iptables/rules.v4).

The trusted parts (this file, services.yml) come from the copy that the
`canary` role deployed from main. The candidate (config files, compose
definition, image reference) is read from a git revision and treated as
untrusted data: templates render in a Jinja sandbox, and only an allowlisted
subset of the compose service definition is turned into `docker run` flags.

    canary.py plan --git-dir D --rev SHA --base BASE      # services to test
    canary.py run  --git-dir D --rev SHA --service gitlab  # one canary
    canary.py run  --worktree /path/to/checkout --service gitlab
    canary.py gc                                           # remove leftovers
"""

import argparse
import base64
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import yaml
from jinja2 import ChainableUndefined
from jinja2.sandbox import SandboxedEnvironment

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.environ.get("CANARY_SPEC", os.path.join(HERE, "services.yml"))
RUN_ROOT = os.environ.get("CANARY_RUN_ROOT", "/var/lib/homelab-canary/runs")
LOCK_PATH = os.environ.get("CANARY_LOCK", "/run/homelab-canary.lock")
LABEL = "homelab.canary"
NETWORK = "canary"
BRIDGE = "br-canary"
SUBNET = "172.28.9.0/24"
DUMMY = "canary-dummy"
ROUTER_HOST = "router.lab.nkontur.com"

# Compose keys copied from the candidate service definition. Everything else
# (ports, networks, volumes, devices, container_name, ipv4_address, privileged,
# network_mode, pid, ipc, extra_hosts, sysctls, ...) is dropped: those bind a
# container to the host or to production.
COPIED_KEYS = {
    "image", "cap_add", "cap_drop", "security_opt", "user", "ulimits",
    "environment", "entrypoint", "command", "tmpfs", "read_only", "healthcheck",
    "working_dir", "shm_size",
}
# Capabilities a canary may be given. Anything else in cap_add is dropped
# (and reported): SYS_ADMIN, SYS_MODULE, SYS_PTRACE, DAC_READ_SEARCH, ... are
# container-escape material and no canary service needs them.
ALLOWED_CAPS = {
    "AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "IPC_LOCK",
    "KILL", "MKNOD", "NET_ADMIN", "NET_BIND_SERVICE", "NET_RAW", "SETFCAP",
    "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT", "SYS_RESOURCE",
}
ALLOWED_SECURITY_OPT = {"no-new-privileges=true", "no-new-privileges:true",
                        "no-new-privileges"}


class CanaryError(Exception):
    pass


def log(msg):
    print(f"[canary {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Reading the candidate
# ---------------------------------------------------------------------------

class Source:
    """Files of one revision: a git object in a (bare) repo, or a worktree."""

    def __init__(self, git_dir=None, rev=None, worktree=None):
        self.git_dir, self.rev, self.worktree = git_dir, rev, worktree

    def _git(self, *args):
        return subprocess.run(["git", "--git-dir", self.git_dir, *args],
                              check=True, capture_output=True).stdout

    def read(self, path):
        if self.worktree:
            p = os.path.join(self.worktree, path)
            if not os.path.isfile(p):
                return None
            with open(p, "rb") as f:
                return f.read()
        try:
            return self._git("show", f"{self.rev}:{path}")
        except subprocess.CalledProcessError:
            return None

    def list(self, prefix):
        prefix = prefix.rstrip("/") + "/"
        if self.worktree:
            out = []
            for root, _dirs, files in os.walk(os.path.join(self.worktree, prefix)):
                for name in files:
                    out.append(os.path.relpath(os.path.join(root, name), self.worktree))
            return sorted(out)
        try:
            names = self._git("ls-tree", "-r", "--name-only", self.rev, "--", prefix)
        except subprocess.CalledProcessError:
            return []
        return sorted(n for n in names.decode().splitlines() if n)


class _DummyUndefined(ChainableUndefined):
    """Undefined variables (Vault secrets, mostly) render as a dummy value."""

    def __str__(self):
        return DUMMY

    def __iter__(self):
        return iter(())


class _LenientFilters(dict):
    """Ansible filters the sandbox does not know pass the value through."""

    def __contains__(self, key):
        return True

    def __missing__(self, key):
        return lambda value, *a, **kw: value

    def get(self, key, default=None):
        return self[key]


def make_env():
    env = SandboxedEnvironment(undefined=_DummyUndefined, keep_trailing_newline=True)
    filters = _LenientFilters(env.filters)
    filters.update({
        "to_json": lambda v, **kw: json.dumps(v),
        "to_nice_json": lambda v, **kw: json.dumps(v, indent=4),
        "to_yaml": lambda v, **kw: yaml.safe_dump(v),
        "to_nice_yaml": lambda v, **kw: yaml.safe_dump(v, indent=4),
        "b64encode": lambda v: base64.b64encode(str(v).encode()).decode(),
        "b64decode": lambda v: base64.b64decode(str(v)).decode(errors="replace"),
        "bool": lambda v: str(v).lower() in ("1", "true", "yes", "on"),
        "quote": lambda v: shlex.quote(str(v)),
        "regex_replace": lambda v, pat="", rep="", **kw: re.sub(pat, rep, str(v)),
        "mandatory": lambda v, *a: v,
        "hash": lambda v, algo="sha1": hashlib.new(algo, str(v).encode()).hexdigest(),
    })
    env.filters = filters
    env.globals["lookup"] = lambda *a, **kw: DUMMY
    env.globals["query"] = lambda *a, **kw: []
    return env


def inventory_vars(src, env):
    """all.vars plus the router's host vars from the candidate inventory."""
    raw = src.read("ansible/inventory.yml")
    inv = yaml.safe_load(raw) if raw else {}
    inv = (inv or {}).get("all", {})
    variables = dict(inv.get("vars") or {})
    variables.update((inv.get("hosts") or {}).get(ROUTER_HOST) or {})
    variables["inventory_hostname"] = ROUTER_HOST
    # Inventory values are templates themselves ("{{ docker_persistent_data_path }}/x").
    for _ in range(3):
        variables = _render_tree(variables, env, variables)
    return variables


def _render_tree(value, env, ctx):
    if isinstance(value, str) and ("{{" in value or "{%" in value):
        try:
            return env.from_string(value).render(ctx)
        except Exception:
            return value
    if isinstance(value, dict):
        return {k: _render_tree(v, env, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_tree(v, env, ctx) for v in value]
    return value


class Renderer:
    def __init__(self, src):
        self.src = src
        self.env = make_env()
        self.vars = inventory_vars(src, self.env)
        self._compose = None

    def render_text(self, text):
        return self.env.from_string(text).render(self.vars)

    def read(self, path, render=True):
        data = self.src.read(path)
        if data is None:
            raise CanaryError(f"{path} does not exist at this revision")
        if not render:
            return data
        return self.render_text(data.decode()).encode()

    def compose(self):
        if self._compose is None:
            self._compose = yaml.safe_load(self.read("docker/docker-compose.yml")) or {}
        return self._compose

    def compose_service(self, name):
        svc = (self.compose().get("services") or {}).get(name)
        if svc is None:
            raise CanaryError(f"service {name} is not in docker/docker-compose.yml")
        return svc


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

def load_spec(path=SPEC_PATH):
    with open(path) as f:
        spec = yaml.safe_load(f)
    defaults = spec.get("defaults") or {}
    services = {}
    for name, entry in (spec.get("services") or {}).items():
        merged = dict(defaults)
        merged.update(entry or {})
        merged["name"] = name
        merged.setdefault("compose_service", name)
        services[name] = merged
    return services


def _match(path, pattern):
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatch(path, pattern)


def plan(services, base, cand, changed_paths):
    """Names of the services whose candidate differs from base.

    A service is tested when one of its trigger paths changed, or when its
    service definition in docker-compose.yml changed (an image bump included).
    """
    selected = []
    compose_changed = "docker/docker-compose.yml" in changed_paths
    for name, svc in services.items():
        triggers = list(svc.get("triggers") or [])
        triggers += [f["src"].rstrip("/") + ("/**" if f["src"].endswith("/") else "")
                     for f in svc.get("files") or []]
        if any(_match(p, t) for p in changed_paths for t in triggers):
            selected.append(name)
            continue
        if compose_changed:
            try:
                old = base.compose_service(svc["compose_service"])
            except CanaryError:
                old = None
            new = cand.compose_service(svc["compose_service"])
            if old != new:
                selected.append(name)
    return selected


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def docker(*args, check=True, capture=True, timeout=None):
    r = subprocess.run(["docker", *args], capture_output=capture, text=True,
                       timeout=timeout)
    if check and r.returncode != 0:
        raise CanaryError(f"docker {' '.join(args[:3])} failed: {(r.stderr or '').strip()}")
    return r


def gc():
    """Remove every canary container, network and run directory."""
    ids = docker("ps", "-aq", "--filter", f"label={LABEL}", check=False).stdout.split()
    if ids:
        docker("rm", "-f", "-v", *ids, check=False)
    nets = docker("network", "ls", "-q", "--filter", f"label={LABEL}", check=False).stdout.split()
    for n in nets:
        docker("network", "rm", n, check=False)
    vols = docker("volume", "ls", "-q", "--filter", f"label={LABEL}", check=False).stdout.split()
    if vols:
        docker("volume", "rm", "-f", *vols, check=False)
    if os.path.isdir(RUN_ROOT):
        for d in os.listdir(RUN_ROOT):
            shutil.rmtree(os.path.join(RUN_ROOT, d), ignore_errors=True)
    return len(ids), len(nets)


def image_repo(image):
    ref = image.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        ref = ref[: len(ref) - len(last)] + last.split(":", 1)[0]
    return ref


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def run_flags(compose_svc, spec, notes):
    """docker run flags from the allowlisted part of a compose service."""
    svc = {k: v for k, v in compose_svc.items() if k in COPIED_KEYS}
    dropped = sorted(set(compose_svc) - COPIED_KEYS)
    if dropped:
        notes.append("not copied from compose: " + ", ".join(dropped))
    for key in ("command", "entrypoint"):
        if key in spec:  # spec override; null means "the image default"
            svc.pop(key, None)
            if spec[key] is not None:
                svc[key] = spec[key]
    flags = []
    for cap in _as_list(svc.get("cap_drop")):
        flags += ["--cap-drop", str(cap)]
    for cap in _as_list(svc.get("cap_add")):
        if str(cap).upper() in ALLOWED_CAPS:
            flags += ["--cap-add", str(cap)]
        else:
            notes.append(f"capability {cap} refused (not in ALLOWED_CAPS)")
    for opt in _as_list(svc.get("security_opt")):
        if str(opt) in ALLOWED_SECURITY_OPT:
            flags += ["--security-opt", str(opt)]
        else:
            notes.append(f"security_opt {opt} refused")
    flags += ["--security-opt", "no-new-privileges=true"]
    if svc.get("user") is not None:
        flags += ["--user", str(svc["user"])]
    for name, lim in (svc.get("ulimits") or {}).items():
        if isinstance(lim, dict):
            flags += ["--ulimit", f"{name}={lim.get('soft')}:{lim.get('hard')}"]
        else:
            flags += ["--ulimit", f"{name}={lim}"]
    env = svc.get("environment") or {}
    items = env.items() if isinstance(env, dict) else (
        (e.split("=", 1) + [""])[:2] for e in env)
    for k, v in items:
        flags += ["--env", f"{k}={'' if v is None else v}"]
    for k, v in (spec.get("environment") or {}).items():
        flags += ["--env", f"{k}={v}"]
    for t in _as_list(svc.get("tmpfs")):
        flags += ["--tmpfs", str(t)]
    if svc.get("read_only"):
        flags += ["--read-only"]
    if svc.get("working_dir"):
        flags += ["--workdir", str(svc["working_dir"])]
    if svc.get("shm_size"):
        flags += ["--shm-size", str(svc["shm_size"])]
    hc = svc.get("healthcheck") or {}
    if hc.get("disable"):
        flags += ["--no-healthcheck"]
    elif hc.get("test"):
        test = hc["test"]
        if isinstance(test, list):
            kind, rest = test[0], test[1:]
            cmd = rest[0] if kind == "CMD-SHELL" else shlex.join(rest)
        else:
            cmd = test
        cmd = cmd.replace("$$", "$")
        flags += ["--health-cmd", cmd]
        # Faster than production: a canary wants the first healthy as soon
        # as possible, and the retries/start period are the same.
        flags += ["--health-interval", "15s",
                  "--health-timeout", str(hc.get("timeout", "10s")),
                  "--health-retries", str(hc.get("retries", 3)),
                  "--health-start-period", str(hc.get("start_period", "0s"))]
    entry = svc.get("entrypoint")
    cmd = svc.get("command")
    args = []
    if entry is not None:
        entry = shlex.split(entry) if isinstance(entry, str) else list(entry)
        flags += ["--entrypoint", entry[0]]
        args += entry[1:]
    if cmd is not None:
        args += shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]
    return svc["image"], flags, args


# ---------------------------------------------------------------------------
# Preparing the throwaway state
# ---------------------------------------------------------------------------

def _host_path(mounts, dest):
    best = None
    for m in mounts:
        p = m["path"].rstrip("/")
        if dest == p or dest.startswith(p + "/"):
            if best is None or len(p) > len(best["path"].rstrip("/")):
                best = m
    if best is None:
        raise CanaryError(f"{dest} is not under any canary mount")
    rel = dest[len(best["path"].rstrip("/")):].lstrip("/")
    return os.path.join(best["host"], rel)


def _write(path, data, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, mode)


def _seed_from_image(image, path, host_dir):
    cid = docker("create", "--network", "none", "--label", LABEL + "=1", image,
                 "true").stdout.strip()
    try:
        docker("cp", f"{cid}:{path.rstrip('/')}/.", host_dir + "/", check=False)
    finally:
        docker("rm", "-f", cid, check=False)


def _seed_from_production(entry, host_dir):
    """Read-only copies of the minimal production state a service needs."""
    base = entry["from"]
    for name in entry.get("include") or []:
        src = os.path.join(base, name)
        dst = os.path.join(host_dir, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        elif os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    keys_from = entry.get("secret_keys_from")
    if keys_from:
        # Keys only, every value a dummy: the real secrets.yaml never leaves
        # production.
        keys = []
        if os.path.isfile(os.path.join(base, keys_from)):
            with open(os.path.join(base, keys_from)) as f:
                keys = re.findall(r"^([A-Za-z0-9_]+)\s*:", f.read(), re.M)
        body = "".join(f"{k}: {DUMMY}\n" for k in keys)
        _write(os.path.join(host_dir, keys_from), body.encode())


def _self_signed(dir_, names):
    os.makedirs(dir_, exist_ok=True)
    key = os.path.join(dir_, names.get("key", "privkey.pem"))
    cert = os.path.join(dir_, names.get("cert", "fullchain.pem"))
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-days", "1", "-subj", "/CN=canary.invalid", "-keyout", key,
                    "-out", cert], check=True, capture_output=True)
    for extra in names.get("copies", []):
        shutil.copy(cert, os.path.join(dir_, extra))
    os.chmod(key, 0o644)


def _dhparam(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(["openssl", "genpkey", "-genparam", "-algorithm", "DH",
                    "-pkeyopt", "group:ffdhe2048", "-out", path],
                   check=True, capture_output=True)


NGINX_UPSTREAM = re.compile(
    r"(?:proxy_pass|grpc_pass|fastcgi_pass|uwsgi_pass)\s+(?:[a-z]+://)?(\$?[A-Za-z0-9_.-]+)"
    r"|^\s*server\s+([A-Za-z0-9_.-]+):[0-9]+", re.M)


def stub_hosts(rendered_texts):
    """Upstream names a config resolves at load time, stubbed to 127.0.0.1.

    nginx resolves a literal upstream host when it loads its config; on the
    isolated canary network none of them exist, so without stubs every nginx
    canary dies on "host not found in upstream".
    """
    names = set()
    for text in rendered_texts:
        for m in NGINX_UPSTREAM.finditer(text):
            name = m.group(1) or m.group(2)
            if name and not name.startswith("$") and not re.match(r"^[0-9.]+$", name):
                names.add(name)
    names.discard("localhost")
    return sorted(names)


# ---------------------------------------------------------------------------
# Running one canary
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, svc, cand, run_id):
        self.svc, self.cand, self.run_id = svc, cand, run_id
        self.dir = os.path.join(RUN_ROOT, run_id)
        self.name = f"canary-{svc['name'].replace('_', '-')}-{run_id}"
        self.sidecars = []
        self.notes = []
        self.peak = {}
        self.rendered = {}
        self.cid = None

    # -- setup ---------------------------------------------------------------
    def prepare(self):
        svc = self.svc
        os.makedirs(self.dir, exist_ok=True)
        self.mounts = []
        for i, m in enumerate(svc.get("mounts") or []):
            host = os.path.join(self.dir, f"m{i}")
            os.makedirs(host, exist_ok=True)
            self.mounts.append({**m, "host": host})
        compose_svc = self.cand.compose_service(svc["compose_service"])
        self.image, self.flags, self.args = run_flags(compose_svc, svc, self.notes)
        repos = svc.get("image_repos")
        if repos and image_repo(self.image) not in repos:
            raise CanaryError(f"image {self.image} is not one of {repos} (services.yml image_repos)")
        log(f"image {self.image}")
        for n in self.notes:
            log(n)
        log("pulling the image (skipped when present)")
        if docker("image", "inspect", self.image, check=False).returncode != 0:
            docker("pull", "-q", self.image, timeout=1800)
        for m in self.mounts:
            if m.get("seed") == "image":
                _seed_from_image(self.image, m["path"], m["host"])
                for rel in m.get("seed_exclude") or []:
                    p = os.path.join(m["host"], rel)
                    shutil.rmtree(p) if os.path.isdir(p) else os.path.exists(p) and os.remove(p)
            if m.get("seed_production"):
                _seed_from_production(m["seed_production"], m["host"])
        for f in svc.get("files") or []:
            srcs = [f["src"]]
            if f["src"].endswith("/"):
                srcs = self.cand.src.list(f["src"])
            for s in srcs:
                dest = f["dest"]
                if f["src"].endswith("/"):
                    dest = dest.rstrip("/") + "/" + s[len(f["src"]):]
                data = self.cand.read(s, render=f.get("render", True))
                self.rendered[dest] = data
                _write(_host_path(self.mounts, dest), data,
                       int(str(f.get("mode", "644")), 8))
        gens = []
        for g in svc.get("generate") or []:
            gens += g if isinstance(g, list) else [g]
        for g in gens:
            if g["type"] == "tls":
                _self_signed(_host_path(self.mounts, g["dir"]), g)
            elif g["type"] == "dhparam":
                _dhparam(_host_path(self.mounts, g["path"]))
            elif g["type"] == "dir":
                os.makedirs(_host_path(self.mounts, g["path"]), exist_ok=True)
            else:
                raise CanaryError(f"unknown generate type {g['type']}")
        self.hosts = []
        if svc.get("stub_upstream_hosts"):
            texts = [d.decode(errors="replace") for d in self.rendered.values()]
            self.hosts = stub_hosts(texts)
            log(f"stubbed {len(self.hosts)} upstream host names to 127.0.0.1")

    def network(self):
        docker("network", "create", "--internal", "--subnet", SUBNET,
               "-o", f"com.docker.network.bridge.name={BRIDGE}",
               "--label", f"{LABEL}=1", NETWORK)

    def _base_flags(self, name, memory):
        return ["--name", name, "--hostname", name[:63],
                "--label", f"{LABEL}=1", "--label", f"{LABEL}.run={self.run_id}",
                "--network", NETWORK, "--memory", memory, "--memory-swap", memory,
                "--cpu-shares", str(self.svc.get("cpu_shares", 256)),
                "--pids-limit", str(self.svc.get("pids_limit", 4096)),
                "--restart", "no"]

    def start_sidecars(self):
        for name, sc in (self.svc.get("sidecars") or {}).items():
            image = sc.get("image")
            if sc.get("image_from_compose"):
                image = self.cand.compose_service(sc["image_from_compose"])["image"]
            host = os.path.join(self.dir, f"sidecar-{name}")
            os.makedirs(host, exist_ok=True)
            flags = self._base_flags(f"{self.name}-{name}", sc.get("memory", "256m"))
            flags += ["--network-alias", name, "--cap-drop", "ALL",
                      "--security-opt", "no-new-privileges=true"]
            for dest, content in (sc.get("files") or {}).items():
                hp = os.path.join(host, dest.strip("/").replace("/", "_"))
                _write(hp, content.encode())
                flags += ["-v", f"{hp}:{dest}:ro"]
            if docker("image", "inspect", image, check=False).returncode != 0:
                docker("pull", "-q", image, timeout=600)
            docker("run", "-d", *flags, image, *(sc.get("args") or []))
            self.sidecars.append((name, f"{self.name}-{name}"))
            log(f"sidecar {name} started ({image})")

    def start(self):
        svc = self.svc
        flags = self._base_flags(self.name, svc.get("memory", "1g"))
        for m in self.mounts:
            flags += ["-v", f"{m['host']}:{m['path']}" + (":ro" if m.get("read_only") else "")]
        for h in self.hosts:
            flags += ["--add-host", f"{h}:127.0.0.1"]
        flags += self.flags
        self.cid = docker("run", "-d", *flags, self.image, *self.args).stdout.strip()
        self.started = time.time()
        log(f"started {self.name}")

    # -- observation ---------------------------------------------------------
    def state(self):
        r = docker("inspect", "-f", "{{json .State}}", self.name, check=False)
        return json.loads(r.stdout) if r.returncode == 0 else {}

    def sample_memory(self):
        for name in [self.name] + [c for _, c in self.sidecars]:
            try:
                cid = docker("inspect", "-f", "{{.Id}}", name).stdout.strip()
                with open(f"/sys/fs/cgroup/docker/{cid}/memory.peak") as f:
                    self.peak[name] = max(self.peak.get(name, 0), int(f.read()))
            except (OSError, ValueError, CanaryError):
                pass

    def exec_(self, cmd, timeout=120, container=None):
        cmd = cmd if isinstance(cmd, list) else ["sh", "-c", cmd]
        try:
            r = subprocess.run(["docker", "exec", container or self.name, *cmd],
                               capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    def _alive(self):
        st = self.state()
        if not st.get("Running"):
            raise CanaryError(f"container exited (code {st.get('ExitCode')}, "
                              f"OOMKilled={st.get('OOMKilled')})")
        return st

    def probes_pass(self, verbose=False):
        st = self._alive()
        health = (st.get("Health") or {}).get("Status")
        if health and health != "healthy":
            return False, f"docker health: {health}"
        for p in self.svc.get("readiness") or []:
            rc, out = self.exec_(p["exec"], timeout=p.get("timeout", 60))
            if rc != 0:
                return False, f"{p['name']}: rc={rc} {out.strip()[-300:]}"
            if verbose:
                log(f"  ok: {p['name']}")
        return True, "ready"

    def wait_ready(self):
        deadline = self.started + int(self.svc.get("timeout", 600))
        delay = 5
        last = None
        while True:
            self.sample_memory()
            ok, why = self.probes_pass()
            if ok:
                log(f"ready after {int(time.time() - self.started)}s")
                return
            if why != last:
                log(f"waiting ({int(time.time() - self.started)}s): {why}")
                last = why
            if time.time() > deadline:
                raise CanaryError(f"not ready within {self.svc.get('timeout')}s: {why}")
            time.sleep(delay)
            delay = min(delay * 2, 30)

    def stable(self):
        """Still up after stable_for seconds: catches crash loops."""
        secs = int(self.svc.get("stable_for", 60))
        if not secs:
            return
        log(f"checking it stays up for {secs}s")
        before = self.state().get("RestartCount", 0)
        end = time.time() + secs
        while time.time() < end:
            time.sleep(min(15, max(1, end - time.time())))
            self.sample_memory()
            self._alive()
        ok, why = self.probes_pass(verbose=True)
        if not ok:
            raise CanaryError(f"ready, then not ready after {secs}s: {why}")
        if self.state().get("RestartCount", 0) != before:
            raise CanaryError("container restarted while being watched")

    def _when(self, cond):
        if not cond:
            return True
        data = self.rendered.get(cond["dest"], b"").decode(errors="replace")
        return re.search(cond["matches"], data, re.M) is not None

    def checks(self):
        for c in self.svc.get("checks") or []:
            if not self._when(c.get("when")):
                log(f"check skipped (condition not met): {c['name']}")
                continue
            log(f"check: {c['name']}")
            if c.get("exec"):
                rc, out = self.exec_(c["exec"], timeout=c.get("timeout", 300))
                for line in out.strip().splitlines()[-40:]:
                    print("    " + line, flush=True)
                if rc != 0:
                    raise CanaryError(f"check failed: {c['name']} (rc={rc})")
            if c.get("traffic"):
                self.exec_(c["traffic"], timeout=c.get("timeout", 300))
            if c.get("sidecar_log"):
                sl = c["sidecar_log"]
                cname = dict(self.sidecars)[sl["sidecar"]]
                end = time.time() + int(c.get("timeout", 300))
                while True:
                    out = docker("logs", cname, check=False).stdout + \
                        docker("logs", cname, check=False).stderr
                    hits = len(re.findall(sl["matches"], out))
                    if hits:
                        log(f"  ok: {hits} match(es) of /{sl['matches']}/ in {sl['sidecar']}")
                        break
                    if time.time() > end:
                        raise CanaryError(f"check failed: {c['name']} (no /{sl['matches']}/ "
                                          f"in {sl['sidecar']} logs)")
                    if c.get("traffic"):
                        self.exec_(c["traffic"], timeout=c.get("timeout", 300))
                    time.sleep(10)

    # -- logs and teardown ---------------------------------------------------
    def collect(self, out_dir, tail):
        os.makedirs(out_dir, exist_ok=True)
        texts = {}
        for label, cname in [("container", self.name)] + \
                [(f"sidecar-{n}", c) for n, c in self.sidecars]:
            r = docker("logs", "--timestamps", cname, check=False)
            texts[label] = (r.stdout or "") + (r.stderr or "")
        for path in self.svc.get("log_files") or []:
            r = docker("exec", self.name, "sh", "-c", f"tail -n 2000 {shlex.quote(path)}",
                       check=False)
            if r.returncode == 0:
                texts[path] = r.stdout
        for label, text in texts.items():
            fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip("/"))
            with open(os.path.join(out_dir, fname + ".log"), "w") as f:
                f.write(text)
            lines = text.splitlines()
            print(f"----- {label} (last {min(tail, len(lines))} of {len(lines)} lines) -----",
                  flush=True)
            for line in lines[-tail:]:
                print(line, flush=True)
        return texts

    def scan(self, texts):
        for pat in self.svc.get("fail_patterns") or []:
            for label, text in texts.items():
                m = re.search(pat, text, re.M)
                if m:
                    line = text[max(0, text.rfind("\n", 0, m.start()) + 1):].split("\n", 1)[0]
                    raise CanaryError(f"fail pattern /{pat}/ in {label}: {line[:300]}")

    def teardown(self):
        names = [self.name] + [c for _, c in self.sidecars]
        docker("rm", "-f", "-v", *names, check=False)
        docker("network", "rm", NETWORK, check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


def run_one(svc, cand, out_dir, tail=200):
    run_id = time.strftime("%Y%m%d%H%M%S")
    r = Run(svc, cand, run_id)
    t0 = time.time()
    ok, reason, texts = False, "", {}

    def on_term(signum, _frame):
        raise CanaryError(f"interrupted (signal {signum})")

    old = {s: signal.signal(s, on_term) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        gc()
        log(f"service {svc['name']} (run {run_id})")
        r.prepare()
        r.network()
        r.start_sidecars()
        r.start()
        r.wait_ready()
        r.stable()
        r.checks()
        ok = True
    except CanaryError as e:
        reason = str(e)
    except Exception as e:  # never leave a canary behind on a bug
        reason = f"{type(e).__name__}: {e}"
    finally:
        try:
            if r.cid:
                r.sample_memory()
                texts = r.collect(out_dir, tail)
                if ok:
                    try:
                        r.scan(texts)
                    except CanaryError as e:
                        ok, reason = False, str(e)
        finally:
            r.teardown()
            for s, h in old.items():
                signal.signal(s, h)
    elapsed = int(time.time() - t0)
    peak = ", ".join(f"{n}: {v / 2**30:.2f} GiB" for n, v in r.peak.items()) or "n/a"
    summary = {"service": svc["name"], "ok": ok, "reason": reason,
               "elapsed_s": elapsed, "peak_memory": {n: v for n, v in r.peak.items()}}
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    log(f"{svc['name']}: {'PASS' if ok else 'FAIL'} in {elapsed}s, peak memory {peak}"
        + ("" if ok else f" -- {reason}"))
    return ok


class _Lock:
    def __enter__(self):
        self.f = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another canary is running; waiting for it")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--git-dir", required=True)
    p.add_argument("--rev", required=True)
    p.add_argument("--base", required=True)
    r = sub.add_parser("run")
    r.add_argument("--git-dir")
    r.add_argument("--rev")
    r.add_argument("--worktree")
    r.add_argument("--service", required=True)
    r.add_argument("--out", default=None, help="directory for the full logs")
    r.add_argument("--tail", type=int, default=200)
    sub.add_parser("gc")
    a = ap.parse_args(argv)
    services = load_spec()
    if a.cmd == "gc":
        with _Lock():
            c, n = gc()
        log(f"removed {c} container(s), {n} network(s)")
        return 0
    if a.cmd == "plan":
        changed = subprocess.run(
            ["git", "--git-dir", a.git_dir, "diff", "--name-only", a.base, a.rev],
            check=True, capture_output=True, text=True).stdout.split()
        base = Renderer(Source(a.git_dir, a.base))
        cand = Renderer(Source(a.git_dir, a.rev))
        print(json.dumps(plan(services, base, cand, changed)))
        return 0
    if a.service not in services:
        print(f"unknown service {a.service}; known: {', '.join(services)}", file=sys.stderr)
        return 2
    src = Source(worktree=a.worktree) if a.worktree else Source(a.git_dir, a.rev)
    out = a.out or tempfile.mkdtemp(prefix=f"canary-{a.service}-")
    with _Lock():
        ok = run_one(services[a.service], Renderer(src), out, a.tail)
    log(f"full logs: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
