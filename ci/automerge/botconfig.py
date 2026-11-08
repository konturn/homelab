"""The bot-config class: low-risk configuration MRs by moltbot (OpenClaw).

moltbot is an LLM agent that reads outside input (Telegram, web pages, logs),
so it can be prompt-injected. Nothing here trusts the bot: an MR qualifies
only if every changed file is on an allowlist AND every change in it has the
shape that file type allows, checked mechanically on the diff. See
docs/AUTOMERGE.md, "Bot-config class".

Pure functions on plain data, like policy.py; automerge.py fetches the data.

Allowed shapes:
  compose    docker-compose files: `deploy.resources.{limits,reservations}.
             {memory,cpus}` values of an existing service (new value within
             [min_ratio, max_ratio] of the old one), and log-level env values
             (`*LOG_LEVEL` / `*LOGLEVEL`) set to a known level. Comment lines
             outside block scalars may be added, changed or removed (no `{`
             or `}`: the file is a Jinja template). Nothing else.
  alerts     Grafana alert-rule files: per existing rule, only `for`,
             `annotations.summary`/`description`, `labels.severity` and
             threshold `evaluator.params` values. No rule added or removed.
  dashboard  Grafana dashboard JSON: any edit that keeps the dashboard uid
             and uses no datasource the file did not already use.
  docs       Markdown under docs/ (minus the excluded files) and README*.md
             at the top level or under docs/.
Never: a path on the hard never-list or `bot_config.never`, a new, deleted,
renamed or mode-changed file, a script, or a denied service.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import policy
from policy import PolicyError

# Never eligible, whatever the policy file says (checked before any allowlist).
HARD_NEVER = [
    "networking/**", "**/iptables*", "**/nftables*", "ansible/**", ".gitlab-ci.yml", "ci/**",
    "renovate.json", "CODEOWNERS", "CLAUDE.md", "**/CLAUDE.md", "docker/vault/**", "terraform/**",
    "docker/openclaw*", "docker/openclaw*/**", "**/*openclaw*", "**/*moltbot*",
    "docker/jit-approval-svc/**", "docker/nginx/**", "docker/promtail/**", "docker/telegraf/**",
    "base/**", "scripts/**", "**/*.env", "**/*.env.*", "**/.env*", "**/*secret*", "**/*vault*",
    "**/*.j2", "docker/grafana/provisioning/alerting/contactpoints*",
    "docker/grafana/provisioning/alerting/policies*", "docker/grafana/provisioning/datasources/**",
]
SCRIPT_EXT = re.compile(r"\.(sh|bash|zsh|py|pyc|js|mjs|cjs|ts|go|pl|rb|lua|php|ps1|bat|exe|so)$", re.I)
LOG_LEVELS = {"trace", "debug", "info", "notice", "warn", "warning", "error", "err", "critical", "crit",
              "fatal", "panic", "off"}
SEVERITIES = {"critical", "warning", "info"}
GRAFANA_BUILTIN_DS = {"grafana", "-- Grafana --", "-- Dashboard --", "-- Mixed --", "__expr__", "dashboard"}
KINDS = ("compose", "alerts", "dashboard", "docs")


def _match(path: str, pattern: str) -> bool:
    if pattern.endswith("/**") and path.startswith(pattern[:-2]):
        return True
    if fnmatch.fnmatchcase(path, pattern):
        return True
    # `**/x` also matches x at the top level
    return pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])


def _any(path: str, patterns: List[str]) -> bool:
    return any(_match(path, p) for p in patterns)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def config(allowlist: dict) -> dict:
    """Validated `bot_config` section. Raises PolicyError when malformed."""
    sec = allowlist.get("bot_config")
    if sec is None:
        return {"enabled": False, "authors": [], "max_per_24h": 0, "never": list(HARD_NEVER),
                "denied_services": [], "paths": {k: [] for k in KINDS}, "docs_exclude": [],
                "min_ratio": 0.5, "max_ratio": 2.0}
    if not isinstance(sec, dict):
        raise PolicyError("`bot_config` must be a mapping")

    def strlist(key: str, value) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
            raise PolicyError(f"bot_config.{key} must be a list of strings")
        return list(value)

    paths = sec.get("paths") or {}
    if not isinstance(paths, dict) or set(paths) - set(KINDS):
        raise PolicyError(f"bot_config.paths must map {list(KINDS)} to path patterns")
    out = {
        "enabled": sec.get("enabled") is True,
        "authors": strlist("authors", sec.get("authors")),
        "never": list(HARD_NEVER) + strlist("never", sec.get("never")),
        "denied_services": strlist("denied_services", sec.get("denied_services")),
        "paths": {k: strlist(f"paths.{k}", paths.get(k)) for k in KINDS},
        "docs_exclude": strlist("docs_exclude", sec.get("docs_exclude")),
    }
    cap = sec.get("max_per_24h", 6)
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
        raise PolicyError("bot_config.max_per_24h must be an integer >= 0")
    out["max_per_24h"] = cap
    ratio = sec.get("resource_ratio") or [0.5, 2.0]
    if (not isinstance(ratio, list) or len(ratio) != 2
            or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in ratio)
            or not 0 < ratio[0] <= 1 <= ratio[1] <= 4):
        raise PolicyError("bot_config.resource_ratio must be [low, high] with 0 < low <= 1 <= high <= 4")
    out["min_ratio"], out["max_ratio"] = float(ratio[0]), float(ratio[1])
    for k in ("compose",):
        for p in out["paths"][k]:
            if not policy.COMPOSE_GLOB.match(p):
                raise PolicyError(f"bot_config.paths.compose: {p} is not a compose file")
    return out


def classify(path: str, cfg: dict) -> Tuple[Optional[str], Optional[str]]:
    """(kind, None) for an allowed path, (None, reason) otherwise. The
    never-lists win over every allowlist."""
    if _any(path, cfg["never"]):
        return None, f"{path} is on the bot-config never-list"
    if SCRIPT_EXT.search(path):
        return None, f"{path} is a script"
    for kind in KINDS:
        if path in cfg["paths"][kind] or _any(path, cfg["paths"][kind]):
            if kind == "docs" and _any(path, cfg["docs_exclude"]):
                return None, f"{path} is excluded from the bot-config docs"
            return kind, None
    return None, f"{path} is not on the bot-config allowlist"


# ---------------------------------------------------------------------------
# Compose: resource limits and log levels
# ---------------------------------------------------------------------------

# Jinja only acts on `{{`, `{%` and `{#`: a comment without braces is inert.
_SAFE_COMMENT = re.compile(r"^\s*#[^{}]*$")
# `key: |`, `- |` (a sequence item, e.g. the script of `command: [sh, -c, ...]`),
# with an optional anchor or tag before the indicator (`key: &a >-`).
_BLOCK_START = re.compile(r"(?::\s*|^\s*-\s+)(?:[&!]\S*\s+)*[|>][-+0-9]*\s*(#.*)?$")
_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([bkmgt]?)(?:i?b)?$", re.I)
_LOG_KEY = re.compile(r"^[A-Z0-9_]*LOG_?LEVEL$")
_ENV_LIST = re.compile(r"^(\s*)-\s+([A-Za-z0-9_]+)=(.*?)\s*$")
_ENV_MAP = re.compile(r"^(\s*)([A-Za-z0-9_]+):\s+(.*?)\s*$")


def _block_lines(lines: List[str]) -> Set[int]:
    """Indices of lines inside YAML block scalars (`key: |`, `- >-`, ...)."""
    out: Set[int] = set()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() and not line.lstrip().startswith("#") and _BLOCK_START.search(line):
            indent = len(line) - len(line.lstrip(" "))
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                    break
                out.add(j)
                j += 1
            i = j
            continue
        i += 1
    return out


def _significant(lines: List[str]) -> List[int]:
    """Line indices that are not blank and not comments (block content counts)."""
    blocks = _block_lines(lines)
    return [i for i, l in enumerate(lines) if i in blocks or (l.strip() and not l.lstrip().startswith("#"))]


def _strip_comment(value: str) -> Tuple[str, str]:
    """(value without quotes, trailing ` # comment` or '')."""
    m = re.search(r"\s+#", value)
    v, comment = (value[:m.start()], value[m.start():]) if m else (value, "")
    v = v.strip()
    # one matching pair only: `'info` or `debug"` is not a quoted level (and
    # an unterminated quote makes the whole file invalid YAML)
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        v = v[1:-1]
    return v, comment


def _mem_bytes(value: str) -> Optional[float]:
    m = _MEM_RE.match(value)
    if not m:
        return None
    mult = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2).lower()]
    return float(m.group(1)) * mult


def _cpus(value: str) -> Optional[float]:
    try:
        f = float(value)
    except ValueError:
        return None
    return f if f > 0 else None


def _key_path(i: int, info, parent) -> List[str]:
    path = []
    while i is not None:
        if info[i] is None:
            return []
        path.append(info[i][1])
        i = parent[i]
    return list(reversed(path))


@dataclass
class ConfigChange:
    file: str
    service: str
    line: int
    what: str            # e.g. "deploy.resources.limits.memory 128M -> 192M"

    def as_dict(self) -> dict:
        return {"file": self.file, "service": self.service, "line": self.line, "what": self.what}


def diff_compose_config(base: str, head: str, path: str, cfg: dict, allowlist: dict) -> List[ConfigChange]:
    """Raise PolicyError unless every change is an allowed resource or
    log-level value edit (or a safe comment edit)."""
    b_lines, h_lines = base.split("\n"), head.split("\n")
    # Every new or changed line, comments and trailing comments included: no
    # Jinja braces, no line break the checks below would not see.
    b_set = set(b_lines)
    for i, l in enumerate(h_lines):
        if l not in b_set:
            policy.check_changed_line(l, f"{path} line {i + 1}", jinja=True)
    b_blocks, h_blocks = _block_lines(b_lines), _block_lines(h_lines)
    # comments: any added or changed comment must be safe and outside blocks
    b_comments = {l for i, l in enumerate(b_lines) if i not in b_blocks and l.lstrip().startswith("#")}
    for i, l in enumerate(h_lines):
        if i in h_blocks or not l.lstrip().startswith("#") or l in b_comments:
            continue
        if not _SAFE_COMMENT.match(l):
            raise PolicyError(f"{path} line {i + 1}: comment contains '{{' or '}}' (Jinja)")
    b_sig, h_sig = _significant(b_lines), _significant(h_lines)
    if len(b_sig) != len(h_sig):
        raise PolicyError(f"{path}: lines other than comments were added or removed")
    _, h_info, h_parent = policy._ci_structure(head)
    _, b_info, b_parent = policy._ci_structure(base)
    h_owner, _ = policy.compose_line_services(head)
    denied = set(cfg["denied_services"]) | set(policy.denylist(allowlist)["services"])
    changes: List[ConfigChange] = []
    for bi, hi in zip(b_sig, h_sig):
        bl, hl = b_lines[bi], h_lines[hi]
        if bl == hl:
            continue
        where = f"{path} line {hi + 1}"
        if bi in b_blocks or hi in h_blocks:
            raise PolicyError(f"{where}: change inside a block scalar")
        svc = h_owner[hi]
        if not svc:
            raise PolicyError(f"{where}: not inside a service")
        if svc in denied or "openclaw" in svc or "moltbot" in svc:
            raise PolicyError(f"{where}: service {svc} is denied to the bot-config class")
        kp, bkp = _key_path(hi, h_info, h_parent), _key_path(bi, b_info, b_parent)
        bind, hind = len(bl) - len(bl.lstrip(" ")), len(hl) - len(hl.lstrip(" "))
        res = ["services", svc, "deploy", "resources"]
        if (kp == bkp and len(kp) == 6 and kp[:4] == res and kp[4] in ("limits", "reservations")
                and kp[5] in ("memory", "cpus") and bind == hind):
            old, oc = _strip_comment(b_info[bi][2])
            new, nc = _strip_comment(h_info[hi][2])
            if nc and not _SAFE_COMMENT.match(nc.strip()):
                raise PolicyError(f"{where}: inline comment contains '{{' or '}}'")
            conv = _mem_bytes if kp[5] == "memory" else _cpus
            ov, nv = conv(old), conv(new)
            if ov is None or nv is None or not ov:
                raise PolicyError(f"{where}: {kp[5]} value {old!r} -> {new!r} is not a literal size")
            ratio = nv / ov
            if not cfg["min_ratio"] <= ratio <= cfg["max_ratio"]:
                raise PolicyError(f"{where}: {svc} {kp[5]} {old} -> {new} is {ratio:.2f}x the old value "
                                  f"(allowed {cfg['min_ratio']}x-{cfg['max_ratio']}x)")
            changes.append(ConfigChange(path, svc, hi + 1, f"deploy.resources.{kp[4]}.{kp[5]} {old} -> {new}"))
            continue
        # log level in `environment:` (list `- KEY=value` or map `KEY: value`)
        env = None
        for rx in (_ENV_LIST, _ENV_MAP):
            mb, mh = rx.match(bl), rx.match(hl)
            if mb and mh:
                env = (mb, mh)
                break
        if env and env[0].group(1) == env[1].group(1) and env[0].group(2) == env[1].group(2):
            key = env[1].group(2)
            parent = h_parent[hi]
            pkey = h_info[parent][1] if parent is not None and h_info[parent] else None
            new, _ = _strip_comment(env[1].group(3))
            old, _ = _strip_comment(env[0].group(3))
            if pkey == "environment" and _LOG_KEY.match(key) and new.lower() in LOG_LEVELS:
                changes.append(ConfigChange(path, svc, hi + 1, f"environment {key} {old} -> {new}"))
                continue
        raise PolicyError(f"{where} ({svc}): only resource limit and log-level values may change")
    return changes


# ---------------------------------------------------------------------------
# Grafana alert rules
# ---------------------------------------------------------------------------

def _diff_paths(a, b, path=()) -> List[Tuple[tuple, object, object]]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append((path + (k,), a.get(k), b.get(k)))
            else:
                out += _diff_paths(a[k], b[k], path + (k,))
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [(path, a, b)]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff_paths(x, y, path + (i,))
        return out
    return [] if (a == b and type(a) is type(b)) else [(path, a, b)]


_DURATION = re.compile(r"^(\d+[smhd])+$")


def diff_alert_rules(base: str, head: str, path: str) -> List[ConfigChange]:
    import yaml
    try:
        b, h = yaml.safe_load(base), yaml.safe_load(head)
    except yaml.YAMLError as e:
        raise PolicyError(f"{path}: not valid YAML ({e.__class__.__name__})")
    changes = []
    for p, old, new in _diff_paths(b, h):
        where = f"{path} {'.'.join(map(str, p))}"
        if len(p) < 5 or p[0] != "groups" or p[2] != "rules":
            raise PolicyError(f"{where}: only existing rules may change")
        rule = h["groups"][p[1]]["rules"][p[3]]
        brule = b["groups"][p[1]]["rules"][p[3]] if isinstance(b["groups"][p[1]].get("rules"), list) else {}
        uid = rule.get("uid") if isinstance(rule, dict) else None
        if not uid or not isinstance(brule, dict) or brule.get("uid") != uid:
            raise PolicyError(f"{where}: rules were added, removed or reordered")
        tail = p[4:]
        ok = False
        if tail == ("for",):
            ok = isinstance(new, str) and bool(_DURATION.match(new))
        elif tail in (("annotations", "summary"), ("annotations", "description")):
            ok = new is None or isinstance(new, str)
        elif tail == ("labels", "severity"):
            ok = new in SEVERITIES
        elif (len(tail) == 8 and tail[0] == "data" and tail[2] == "model" and tail[3] == "conditions"
              and tail[5] == "evaluator" and tail[6] == "params"):
            model = rule["data"][tail[1]]["model"]
            ok = (model.get("type") in ("threshold", "classic_conditions")
                  and isinstance(new, (int, float)) and not isinstance(new, bool)
                  and isinstance(old, (int, float)) and not isinstance(old, bool))
        if not ok:
            raise PolicyError(f"{where} ({uid}): only for, summary, description, severity and "
                              "threshold values may change")
        changes.append(ConfigChange(path, uid, 0, f"{'.'.join(map(str, tail))}: {old!r} -> {new!r}"))
    return changes


def alert_rule_uids(text: str) -> List[str]:
    import yaml
    doc = yaml.safe_load(text) or {}
    return [r["uid"] for g in doc.get("groups") or [] for r in g.get("rules") or [] if r.get("uid")]


# ---------------------------------------------------------------------------
# Grafana dashboards
# ---------------------------------------------------------------------------

def _datasources(node, out: Set[str]) -> Set[str]:
    if isinstance(node, dict):
        ds = node.get("datasource")
        if isinstance(ds, dict) and ds.get("uid") is not None:
            out.add(str(ds["uid"]))
        elif isinstance(ds, str):
            out.add(ds)
        for v in node.values():
            _datasources(v, out)
    elif isinstance(node, list):
        for v in node:
            _datasources(v, out)
    return out


def diff_dashboard(base: str, head: str, path: str) -> List[ConfigChange]:
    try:
        b, h = json.loads(base), json.loads(head)
    except ValueError:
        raise PolicyError(f"{path}: not valid JSON")
    if not isinstance(h, dict) or not isinstance(b, dict) or not h.get("uid") or h.get("uid") != b.get("uid"):
        raise PolicyError(f"{path}: the dashboard uid must stay the same")
    new = _datasources(h, set()) - _datasources(b, set()) - GRAFANA_BUILTIN_DS
    if new:
        raise PolicyError(f"{path}: uses datasources the dashboard did not use before: {', '.join(sorted(new))}")
    return [ConfigChange(path, h["uid"], 0, "dashboard edit")]


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

def diff_docs(base: str, head: str, path: str) -> List[ConfigChange]:
    if "\x00" in head:
        raise PolicyError(f"{path}: binary content")
    return [ConfigChange(path, "", 0, "docs edit")]


# ---------------------------------------------------------------------------
# Whole MR
# ---------------------------------------------------------------------------

@dataclass
class BotConfigResult:
    reasons: List[str] = field(default_factory=list)
    changes: List[ConfigChange] = field(default_factory=list)
    kinds: Dict[str, str] = field(default_factory=dict)       # path -> kind

    @property
    def compose_services(self) -> List[Tuple[str, str]]:
        return sorted({(c.file, c.service) for c in self.changes if self.kinds.get(c.file) == "compose"})

    @property
    def grafana_files(self) -> List[str]:
        return sorted(p for p, k in self.kinds.items() if k in ("alerts", "dashboard"))


DIFFERS = {"alerts": diff_alert_rules, "dashboard": diff_dashboard, "docs": diff_docs}


def evaluate_files(files: List[policy.FileChange], contents: Dict[str, policy.ComposePair],
                   allowlist: dict) -> BotConfigResult:
    """The path and shape checks (also re-run by plan-deploy on the merge)."""
    r = BotConfigResult()
    try:
        cfg = config(allowlist)
    except PolicyError as e:
        r.reasons.append(f"policy: {e}")
        return r
    if not files:
        r.reasons.append("MR has no file changes")
    for f in files:
        if f.new_file or f.deleted_file or f.renamed_file or f.mode_changed:
            r.reasons.append(f"{f.path}: file added, deleted, renamed or mode-changed")
            continue
        kind, why = classify(f.path, cfg)
        if why:
            r.reasons.append(why)
            continue
        base, head = contents.get(f.path, (None, None))
        if base is None or head is None:
            r.reasons.append(f"could not read both versions of {f.path}")
            continue
        if head.startswith("#!"):
            r.reasons.append(f"{f.path} is a script")
            continue
        r.kinds[f.path] = kind
        try:
            if kind == "compose":
                r.changes += diff_compose_config(base, head, f.path, cfg, allowlist)
            else:
                r.changes += DIFFERS[kind](base, head, f.path)
        except PolicyError as e:
            r.reasons.append(f"bot-config: {e}")
    if not r.reasons and not r.changes:
        r.reasons.append("no change found")
    return r
