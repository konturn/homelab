#!/usr/bin/env python3
"""Changed-only host deploys (see docs/DEPLOY.md).

  deploy_scope.py select --playbook router [--base SHA] [--head SHA] [FILE...]
      Print which roles a change needs and why (no host access). FILEs, or
      the diff base..head, are the changed paths.
  deploy_scope.py run --playbook router [-- extra ansible-playbook args]
      CI deploy: work out the scope against the commit last deployed to the
      host, run ansible-playbook for it, and record the new commit on the
      host when the run succeeds.

The base of the diff is the commit recorded on the host by the last
successful deploy of this playbook (MARKER_DIR/<playbook>.sha), not
CI_COMMIT_BEFORE_SHA: deploys skipped by the resource group or a failed
deploy would otherwise drop changes. The diff is a tree diff, so any commit
works as a base (merges, force pushes).

Full deploy when: DEPLOY_FULL is set; the pipeline was not started by a push
(schedule, web, api, ...) unless DEPLOY_SCOPE=changed; the marker is missing
or unreadable; the base commit cannot be fetched; a changed path is a shared
file (inventory, CI, fetch-vault-secrets, base/validate, this directory, the
playbook itself...) or matches no rule in scope.yml.

Unreachable host: before anything else, `run` opens a TCP connection to
port 22 of every host the playbook targets. If one does not answer, it
prints why and exits EXIT_UNREACHABLE (75) without running Ansible or
touching any marker, so the next deploy after the host returns diffs from
its last successful deploy and applies everything it missed. CI jobs for
hosts that may be offline list 75 under allow_failure:exit_codes, so an
offline host shows as a warning instead of failing the pipeline.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCOPE_FILE = HERE / "scope.yml"
ANSIBLE_DIR = REPO / "ansible"
MARKER_DIR = "/var/lib/homelab-deploy"
ALWAYS_ROLES = ("fetch-vault-secrets",)
# EX_TEMPFAIL: a target host does not accept connections on port 22.
EXIT_UNREACHABLE = 75

FULL = "FULL"


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def match(pattern: str, path: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return fnmatch.fnmatchcase(path, pattern)


def playbook_roles(playbook: str, ansible_dir: Path = ANSIBLE_DIR) -> List[str]:
    """Role names listed in ansible/<playbook>.yml, in order."""
    plays = yaml.safe_load((ansible_dir / f"{playbook}.yml").read_text()) or []
    roles: List[str] = []
    for play in plays:
        for r in play.get("roles") or []:
            name = r if isinstance(r, str) else r.get("role") or r.get("name")
            if name not in roles:
                roles.append(name)
    return roles


def load_rules(playbook: str, scope: Optional[dict] = None,
               roles: Optional[List[str]] = None) -> List[Tuple[str, object, str]]:
    """Ordered (pattern, decision, why) list. decision: FULL or a role list."""
    scope = scope if scope is not None else yaml.safe_load(SCOPE_FILE.read_text())
    if playbook not in scope["playbooks"]:
        raise SystemExit(f"no scope rules for playbook {playbook!r} in {SCOPE_FILE}")
    roles = roles if roles is not None else playbook_roles(playbook)
    rules: List[Tuple[str, object, str]] = []
    for p in scope["shared"]["full"]:
        rules.append((p, FULL, "shared file"))
    rules.append((f"ansible/{playbook}.yml", FULL, "the playbook itself"))
    for p in scope["shared"]["none"]:
        rules.append((p, [], "no effect on host deploys"))
    for r in roles:
        rules.append((f"ansible/roles/{r}/**", [r], f"role {r}"))
    for rule in scope["playbooks"][playbook]:
        for p in rule["match"]:
            rules.append((p, list(rule["roles"]), f"scope.yml {playbook} rule"))
    rules.append(("ansible/roles/**", [], f"role not used by {playbook}.yml"))
    return rules


def select(playbook: str, changed: Sequence[str], rules=None, roles=None):
    """Return (decision, explanation). decision: FULL or a sorted role list
    (empty = nothing to deploy). explanation: [(path, decision, why)]."""
    roles = roles if roles is not None else playbook_roles(playbook)
    rules = rules if rules is not None else load_rules(playbook, roles=roles)
    explain = []
    selected: set = set()
    full = False
    for path in changed:
        for pattern, decision, why in rules:
            if match(pattern, path):
                break
        else:
            decision, why = FULL, "no rule matches (unmapped path)"
        explain.append((path, decision, why))
        if decision == FULL:
            full = True
        else:
            unknown = [r for r in decision if r not in roles]
            if unknown:  # a rule names a role the playbook does not have
                full = True
                explain[-1] = (path, FULL, f"rule names roles not in {playbook}.yml: {unknown}")
            selected.update(decision)
    if full:
        return FULL, explain
    ordered = [r for r in roles if r in selected]
    return ordered, explain


def tags_for(roles: Sequence[str]) -> List[str]:
    """Tags to pass to ansible-playbook (role name == tag; always-roles run anyway)."""
    return [r for r in roles if r not in ALWAYS_ROLES]


# --------------------------------------------------------------------------
# Git and hosts
# --------------------------------------------------------------------------

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(REPO), *args], capture_output=True,
                          text=True, check=check)


def changed_files(base: str, head: str) -> List[str]:
    out = git("diff", "--name-only", "--no-renames", base, head).stdout
    return sorted(p for p in out.splitlines() if p)


def ensure_commit(sha: str) -> bool:
    if git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0:
        return True
    git("fetch", "-q", "--depth=1", "origin", sha, check=False)
    return git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def playbook_hosts(playbook: str, inventory: Path) -> List[dict]:
    """[{name, host, user}] for every host the playbook's plays target."""
    plays = yaml.safe_load((ANSIBLE_DIR / f"{playbook}.yml").read_text()) or []
    inv = json.loads(subprocess.run(
        ["ansible-inventory", "-i", str(inventory), "--list"],
        capture_output=True, text=True, check=True).stdout)
    hostvars = inv.get("_meta", {}).get("hostvars", {})

    def expand(name: str) -> List[str]:
        if name in hostvars:
            return [name]
        group = inv.get(name, {})
        out = list(group.get("hosts", []))
        for child in group.get("children", []):
            out += expand(child)
        return out

    names: List[str] = []
    for play in plays:
        for pattern in str(play.get("hosts", "")).split(","):
            for h in expand(pattern.strip()):
                if h not in names:
                    names.append(h)
    return [{"name": h,
             "host": hostvars[h].get("ansible_host", h),
             "user": hostvars[h].get("ansible_user", "root")} for h in names]


# Pinned host keys, checked strictly (docs/DEPLOY.md, "SSH host keys").
KNOWN_HOSTS = Path(__file__).resolve().parents[2] / "ansible" / "known_hosts"


def ssh(host: dict, key: str, command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
         "-o", "LogLevel=ERROR", f"{host['user']}@{host['host']}", command],
        capture_output=True, text=True, timeout=60)


def reachable(host: dict, port: int = 22, timeout: float = 10.0) -> Tuple[bool, str]:
    """(True, "") if a TCP connection to host:port opens, else (False, reason)."""
    try:
        with socket.create_connection((host["host"], port), timeout=timeout):
            return True, ""
    except OSError as e:
        return False, str(e) or e.__class__.__name__


def read_marker(host: dict, key: str, playbook: str) -> Optional[str]:
    try:
        r = ssh(host, key, f"cat {MARKER_DIR}/{playbook}.sha 2>/dev/null || true")
    except subprocess.TimeoutExpired:
        return None
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def write_marker(host: dict, key: str, playbook: str, sha: str) -> bool:
    f = f"{MARKER_DIR}/{playbook}.sha"
    cmd = (f"mkdir -p {MARKER_DIR} && printf '%s\\n' {shlex.quote(sha)} > {f}.tmp "
           f"&& mv {f}.tmp {f}")
    try:
        return ssh(host, key, cmd).returncode == 0
    except subprocess.TimeoutExpired:
        return False


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def describe(decision) -> str:
    if decision == FULL:
        return "FULL"
    return ", ".join(decision) if decision else "(no effect)"


def print_explanation(explain, limit: int = 200) -> None:
    for path, decision, why in explain[:limit]:
        print(f"  {path}\n      -> {describe(decision)}  [{why}]")
    if len(explain) > limit:
        print(f"  ... and {len(explain) - limit} more paths")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def forced_full_reason(env) -> Optional[str]:
    if truthy(env.get("DEPLOY_FULL")):
        return "DEPLOY_FULL is set"
    source = env.get("CI_PIPELINE_SOURCE", "")
    if source and source != "push" and env.get("DEPLOY_SCOPE") != "changed":
        return f"pipeline source is {source!r} (only push pipelines deploy changed-only)"
    return None


def cmd_select(args) -> int:
    if args.files:
        changed = args.files
    else:
        changed = changed_files(args.base, args.head or "HEAD")
    decision, explain = select(args.playbook, changed)
    print_explanation(explain)
    print(f"{args.playbook}: {describe(decision)}")
    return 0


def cmd_run(args) -> int:
    env = os.environ
    pb = args.playbook
    head = env.get("CI_COMMIT_SHA") or git("rev-parse", "HEAD").stdout.strip()
    key = args.private_key
    inventory = ANSIBLE_DIR / "inventory.yml"
    hosts = playbook_hosts(pb, inventory)

    print(f"=== deploy scope: {pb}.yml at {head[:12]} on {', '.join(h['name'] for h in hosts)}")
    down = []
    for h in hosts:
        ok, why = reachable(h)
        if not ok:
            down.append(f"{h['name']} ({h['host']}:22: {why})")
    if down:
        print(f"=== host unreachable: {'; '.join(down)}")
        print("=== nothing deployed and no marker changed; the next deploy after the "
              "host is back diffs from its last successful deploy")
        return EXIT_UNREACHABLE
    decision, explain, reason = None, [], forced_full_reason(env)
    if reason is None:
        changed: set = set()
        for h in hosts:
            base = read_marker(h, key, pb)
            if base is None:
                reason = f"no deploy marker on {h['name']} ({MARKER_DIR}/{pb}.sha)"
                break
            if not ensure_commit(base):
                reason = f"last deployed commit {base[:12]} on {h['name']} cannot be fetched"
                break
            print(f"last successful deploy on {h['name']}: {base[:12]}")
            changed.update(changed_files(base, head))
        if reason is None:
            decision, explain = select(pb, sorted(changed))
            if decision == FULL:
                reason = "a changed path needs a full deploy (see below)"
    if reason is not None:
        decision = FULL
    print_explanation(explain)
    if decision == FULL:
        print(f"=== FULL deploy of {pb}.yml: {reason}")
        tags = None
    elif not decision:
        print(f"=== nothing to deploy for {pb}.yml: no changed path affects it")
        tags = []
    else:
        tags = tags_for(decision)
        print(f"=== changed-only deploy of {pb}.yml: roles {', '.join(decision)}"
              f" (+ always: {', '.join(ALWAYS_ROLES)}, pre_tasks)")
    sys.stdout.flush()

    if tags == []:
        rc = 0
    else:
        cmd = ["ansible-playbook", "-i", str(inventory), "-u", "root",
               "--private-key", key, str(ANSIBLE_DIR / f"{pb}.yml"), *args.ansible_args]
        if tags:
            cmd += ["--tags", ",".join(tags)]
        print("$ " + " ".join(shlex.quote(c) for c in cmd))
        sys.stdout.flush()
        rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"=== deploy failed (rc={rc}); deploy marker not updated, the next "
              "deploy diffs from the last successful one")
        return rc
    for h in hosts:
        if not write_marker(h, key, pb, head):
            # Not fatal: the next deploy diffs from the older marker (a
            # superset of changes) or runs full if there is none.
            print(f"WARN: could not record {head[:12]} as deployed on {h['name']}")
    print(f"=== recorded {head[:12]} as last successful {pb} deploy")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--playbook", required=True)
    s.add_argument("--base", default="HEAD~1")
    s.add_argument("--head")
    s.add_argument("files", nargs="*")
    s.set_defaults(fn=cmd_select)
    r = sub.add_parser("run")
    r.add_argument("--playbook", required=True)
    r.add_argument("--private-key", default="tmp")
    r.add_argument("ansible_args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)
    args = p.parse_args(argv)
    if getattr(args, "ansible_args", None) and args.ansible_args[0] == "--":
        args.ansible_args = args.ansible_args[1:]
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
