"""Property-based tests (Hypothesis) for the auto-merge policy's security
boundary: which MRs can ever be eligible.

Each test states an invariant that must hold for every input and lets
Hypothesis search for a counterexample among adversarial inputs: path
variants (./, //, ../, case, Unicode lookalikes), new/deleted/renamed/
mode-changed files, crafted compose and CI YAML, arbitrary tags, labels and
identities. Where it can, a property checks the policy against an
independent oracle rather than its own helpers: PyYAML for what a changed
file means, a restatement of Docker's size parser for what a memory value
means.

Runs against the real policy file (ci/automerge-policy.yml) and CODEOWNERS.

Examples per property come from HYPOTHESIS_PROFILE: `ci` (300; the default
when $CI is set), `deep` (5000; the automerge:properties-deep job), `dev`
(100; the local default). A failure prints a `@reproduce_failure(...)`
line to paste above the test.

Run: pip install -r ci/automerge/requirements-test.txt
     python3 -m unittest discover -s ci/automerge -p 'test_properties.py' -v
"""

from __future__ import annotations

import os
import posixpath
import re
import string
import sys
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from hypothesis import HealthCheck, assume, example, given, note, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover
    if os.environ.get("CI"):
        raise           # CI must run these, never skip them
    raise unittest.SkipTest("hypothesis is not installed: pip install -r ci/automerge/requirements-test.txt")

import yaml  # noqa: E402

import automerge  # noqa: E402
import botconfig  # noqa: E402
import policy  # noqa: E402
from policy import FileChange, MajorVersionChange, PolicyError  # noqa: E402

_COMMON = dict(deadline=None, print_blob=True,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much,
                                      HealthCheck.data_too_large])
settings.register_profile("dev", max_examples=100, **_COMMON)
settings.register_profile("ci", max_examples=300, database=None, **_COMMON)
settings.register_profile("deep", max_examples=5000, database=None, **_COMMON)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if os.environ.get("CI") else "dev"))

REPO = Path(__file__).resolve().parents[2]
ALLOW = automerge.load_allowlist()
BC = botconfig.config(ALLOW)
CODEOWNERS = (REPO / "CODEOWNERS").read_text()
C = policy.COMPOSE_PATH
CI = policy.CI_PATH
MOLTBOT = "moltbot"
RENOVATE = ALLOW["ci_images"]["authors"][0]
MERGER = "automerge-bot"
COMPOSE_FILES = set(policy.compose_files(ALLOW))
CI_FILES = set(policy.ci_files(ALLOW))
DENIED_SERVICES = set(policy.denylist(ALLOW)["services"])
DENIED_IMAGES = set(policy.denylist(ALLOW)["images"])
BC_DENIED = set(BC["denied_services"]) | DENIED_SERVICES
DIGESTS = ["sha256:" + c * 64 for c in "0123456789abcdef"]
MISSING = object()


def canonical(path: str) -> str:
    """What a path names once `.`, `..` and repeated slashes are resolved."""
    return posixpath.normpath("/" + path).lstrip("/")


def on_never_list(path: str) -> bool:
    return botconfig._any(path, BC["never"])


def ydiff(a, b, path=()):
    """Leaf differences between two YAML documents: [(path, old, new)]."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in list(a) + [k for k in b if k not in a]:
            if k not in a or k not in b:
                out.append((path + (k,), a.get(k, MISSING), b.get(k, MISSING)))
            else:
                out += ydiff(a[k], b[k], path + (k,))
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += ydiff(x, y, path + (i,))
        return out
    return [] if (type(a) is type(b) and a == b) else [(path, a, b)]


class _CILoader(yaml.SafeLoader):
    pass


_CILoader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))


def load_ci(text):
    return yaml.load(text, Loader=_CILoader)


# Docker's reading of a size (go-units RAMInBytes): binary units, optional
# i/b suffix, truncated to whole bytes. ASCII digits only.
_DOCKER_SIZE = re.compile(r"^(\d+(?:\.\d+)?) ?([kmgtp])?i?b?$", re.I | re.A)
_GO_FLOAT = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$", re.A)


def docker_bytes(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _DOCKER_SIZE.match(str(value))
    if not m:
        return None
    return float(m.group(1)) * 1024 ** ("kmgtp".index(m.group(2).lower()) + 1 if m.group(2) else 0)


def docker_cpus(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return float(value) if _GO_FLOAT.match(str(value).strip()) else None


def changed_lines(base: str, head: str):
    b = set(base.split("\n"))
    return [l for l in head.split("\n") if l not in b]


def assert_no_smuggling(tc, base, head, jinja=True):
    """A changed line carries no hidden line break, and in a Jinja template
    (the compose files; not .gitlab-ci.yml) no brace."""
    for l in changed_lines(base, head):
        if jinja:
            tc.assertNotRegex(l, r"[{}]", "accepted a changed line with a brace (Jinja template)")
        tc.assertEqual(l.splitlines() or [""], [l], "accepted a changed line with a hidden line break")


# ===========================================================================
# Strategies: text
# ===========================================================================

SEG = st.text(alphabet=string.ascii_lowercase + string.digits + "-_", min_size=1, max_size=6)
LOOKALIKE = {"a": "а", "e": "е", "o": "о", "c": "с", "i": "і", "p": "р",
             "s": "ѕ", "x": "х", "/": "∕", ".": "․", "-": "‐"}
# characters that tend to break line-oriented checks
TRICKY = ["\r", "\x85", " ", " ", "\x0b", "\x0c", "\t", "{", "}", "{{", "{%", "{#", "#", ":", " ",
          "'", '"', "|", ">", "&a", "*a", "!!str", "\\", "​", "﻿", "-", "@", "[", "]"]
TEXT = st.lists(st.one_of(st.sampled_from(TRICKY), st.text(string.ascii_letters + string.digits + " ._/=",
                                                             max_size=6)), max_size=6).map("".join)


@st.composite
def instantiate(draw, pattern: str) -> str:
    """A concrete path matching a never-list pattern."""
    parts = pattern.split("/")
    out = []
    for i, part in enumerate(parts):
        if part == "**":
            out += draw(st.lists(SEG, min_size=1 if i == len(parts) - 1 else 0, max_size=2))
        else:
            out.append("".join(draw(SEG) if ch == "*" else ch for ch in part))
    path = "/".join(out)
    if parts[-1] == "**" or parts[-1].endswith("*"):
        path += draw(st.sampled_from(["", ".md", ".json", ".yml"]))
    return path


@st.composite
def path_variant(draw, path: str) -> str:
    """Spellings of a path: some name the same file, some only look like it."""
    kind = draw(st.sampled_from(["same", "dot", "dslash", "docs_dotdot", "dash_dotdot", "deep_dotdot",
                                 "lead_slash", "trail_slash", "upper", "swapcase", "lookalike",
                                 "zero_width", "nfd", "space", "backslash", "md_suffix"]))
    if kind == "dot":
        return "./" + path
    if kind == "dslash":
        return path.replace("/", "//", 1) if "/" in path else "/" + path
    if kind == "docs_dotdot":
        return "docs/../" + path
    if kind == "dash_dotdot":
        return "docker/grafana/provisioning/dashboards/json/../../../../../" + path
    if kind == "deep_dotdot":
        return "docs/" + draw(SEG) + "/../../" + path
    if kind == "lead_slash":
        return "/" + path
    if kind == "trail_slash":
        return path + "/"
    if kind == "upper":
        return path.upper()
    if kind == "swapcase":
        i = draw(st.integers(0, len(path) - 1))
        return path[:i] + path[i].swapcase() + path[i + 1:]
    if kind == "lookalike":
        idx = [i for i, ch in enumerate(path) if ch in LOOKALIKE]
        if not idx:
            return path
        i = draw(st.sampled_from(idx))
        return path[:i] + LOOKALIKE[path[i]] + path[i + 1:]
    if kind == "zero_width":
        i = draw(st.integers(0, len(path)))
        return path[:i] + "​" + path[i:]
    if kind == "nfd":
        return unicodedata.normalize("NFD", path.replace("e", "é", 1))
    if kind == "space":
        return path + " "
    if kind == "backslash":
        return path.replace("/", "\\")
    if kind == "md_suffix":
        return path + ".md"
    return path


NEVER_PATH = st.sampled_from(BC["never"]).flatmap(instantiate)
ALLOWED_PATH = st.sampled_from(sorted(COMPOSE_FILES | CI_FILES) + [
    "docs/notes.md", "docs/runbooks/README.md", "README.md", "docs/AUTOMERGE.md", "docs/jit-approval.md",
    "docker/grafana/provisioning/dashboards/json/ups.json",
    "docker/grafana/provisioning/alerting/infrastructure.yml"])
OTHER_PATH = st.lists(SEG, min_size=1, max_size=3).map("/".join)
ANY_PATH = st.one_of(NEVER_PATH, ALLOWED_PATH, OTHER_PATH).flatmap(path_variant)

# ===========================================================================
# Strategies: versions and image references
# ===========================================================================

# Flavor templates; "#" is a number. Digits stay below 7 so no rendering looks
# like the git-hash build id the policy ignores (_HASH_SUFFIX_RE).
FLAVORS = ["", "-alpine", "-alpine#.#", "-ls#", "-ubuntu", "-ubuntu#.#", "-perl", "-rc#", "-beta.#",
           "-debian-#", "-#", "-cuda#.#-runtime", "-alpine#.#-perl", "-debug", "-slim-bookworm"]
NUM = st.integers(0, 40) | st.integers(0, 99999)


@st.composite
def version(draw, flavor=None):
    """(tag, nums padded to 4, flavor template, flavor numbers)."""
    n = draw(st.integers(2, 4))
    nums = [draw(NUM) for _ in range(n)]
    tmpl = flavor if flavor is not None else draw(st.sampled_from(FLAVORS))
    fnums = [draw(st.integers(0, 99999)) for _ in range(tmpl.count("#"))]
    it = iter(fnums)
    suffix = re.sub("#", lambda _: str(next(it)), tmpl)
    prefix = draw(st.sampled_from(["", "", "v"]))
    tag = prefix + ".".join(map(str, nums)) + suffix
    return tag, tuple(nums + [0] * (4 - n)), tmpl, tuple(fnums)


@st.composite
def version_pair(draw):
    """Two versions, biased towards ones that differ in exactly one place."""
    old = draw(version())
    how = draw(st.sampled_from(["any", "same_flavor", "bump_patch", "bump_minor", "bump_major", "flavor"]))
    if how == "any":
        return old, draw(version())
    tmpl = old[2] if how != "flavor" else draw(st.sampled_from([f for f in FLAVORS if f != old[2]]))
    nums = list(old[1])
    if how == "bump_patch":
        nums[2] += draw(st.integers(1, 5))
    elif how == "bump_minor":
        nums[1] += draw(st.integers(1, 5))
        nums[2] = 0
    elif how == "bump_major":
        nums[0] += draw(st.integers(1, 5))
    elif how in ("same_flavor", "flavor"):
        nums = [draw(NUM) for _ in range(4)]
    fnums = [draw(st.integers(0, 99999)) for _ in range(tmpl.count("#"))]
    it = iter(fnums)
    suffix = re.sub("#", lambda _: str(next(it)), tmpl)
    n = draw(st.integers(3, 4)) if nums[3] == 0 else 4
    prefix = old[0][0] if old[0].startswith("v") else ""
    tag = prefix + ".".join(map(str, nums[:n])) + suffix
    return old, (tag, tuple(nums), tmpl, tuple(fnums))


NON_VERSION_TAGS = ["latest", "stable", "edge", "nightly", "main", "20260101", "2026-01-01", "18993091-ls1",
                    "sha-abcdef0", "1", "v1", "v", "", "1.", ".1.2", "V1.2.3", "1,2,3", "1..2", "release-1.2.3",
                    "x1.2.3", "1-2-3", "١.٢"]
ARBITRARY_TAG = st.one_of(st.sampled_from(NON_VERSION_TAGS), st.text(max_size=20), TEXT,
                          version().map(lambda v: v[0]))

IMAGE_NAMES = ["nginx", "library/postgres", "ghcr.io/home-assistant/home-assistant", "lscr.io/linuxserver/radarr",
               "registry.example.com:5000/team/app", "hashicorp/vault", "gitlab/gitlab-ce", "mariadb",
               "willhallonline/ansible", "example/web"]


def render_ref(name, tag, digest, quote="", comment=""):
    ref = name + (":" + tag if tag else "") + ("@" + digest if digest else "")
    return f"{quote}{ref}{quote}{comment}"


# ===========================================================================
# Strategies: compose files and edits
# ===========================================================================

SERVICES = ["web", "db", "plex", "navidrome", "vault", "gitlab", "nginx", "lab_nginx", "jit-approval-svc",
            "openclaw-gateway", "x-moltbot", "grafana", "mopidy", "radarr", "docker-socket-proxy"]
LEVELS = sorted(botconfig.LOG_LEVELS)
ENV_KEYS = ["LOG_LEVEL", "ND_LOGLEVEL", "APP_LOG_LEVEL", "LOGLEVEL", "TZ", "PUID", "LOG_FORMAT", "DEBUG_LEVEL"]
BASE_MEM = st.sampled_from(["64M", "128M", "256m", "512MiB", "1G", "1.5G", "2g", "3GB", "8G", "768M",
                            "100000k", "268435456", "'512M'", '"2G"'])
BASE_CPUS = st.sampled_from(["0.5", "'2'", '"1.5"', "1", "4.0", "0.25", "2"])
UNITS = ["", "b", "k", "K", "m", "M", "g", "G", "t", "p", "kb", "KB", "MiB", "mib", "Gi", "GiB", "gb", "ib", "bb",
         "i", " M", "  M", "\tM", "M M", "MiBB"]
NUMBER = st.one_of(st.integers(0, 70000).map(str), st.floats(0, 70000, allow_nan=False).map(lambda f: f"{f:.3f}"),
                   st.sampled_from(["0", "00128", "1e3", "inf", "nan", "infinity", "-128", "+128", "1_000",
                                    "0x80", "١٢٨", "1.2.3", ".5", "5.", "1e-300", "1e400", "", " "]))
MEM_VALUE = st.builds(lambda n, u, q: f"{q}{n}{u}{q}", NUMBER, st.sampled_from(UNITS), st.sampled_from(["", "'", '"']))
CPU_VALUE = st.builds(lambda n, q: f"{q}{n}{q}", NUMBER, st.sampled_from(["", "'", '"']))
LEVEL_VALUE = st.one_of(st.sampled_from(LEVELS), st.sampled_from(LEVELS).map(str.upper),
                        st.sampled_from(["chatty", "info; rm -rf /", "'info", 'debug"', "${LEVEL}", "on", "true"]),
                        TEXT)
TRAILING = st.one_of(st.just(""), TEXT.map(lambda t: "  # " + t), st.just("  # tuned"))

EXTRAS = [
    "    command:\n      - /bin/sh\n      - -c\n      - |\n        # tune the cache\n        echo memory: 1G\n"
    "        exec app",
    "    entrypoint: |\n      #!/bin/sh\n      # comment inside a block\n      exec app",
    "    command: &cmd_{name} >-\n      run\n      # folded",
    "    labels:\n      memory: 1G\n      com.example.cpus: \"2\"",
    "    healthcheck:\n      test: [\"CMD\", \"true\"]\n      interval: 30s",
    "    volumes:\n      - ./data:/data  # data dir",
    "    <<: *defaults",
    "    profiles: [\"retired\"]",
    "    restart: unless-stopped",
    "    command: [\"sh\", \"-c\", \"echo one\n      two\"]",
    "    entrypoint: \"run\n      # inside a quoted scalar\n      --flag\"",
]
POOL_LINES = ["    privileged: true", "      memory: 4G", "          memory: 4G", "          cpus: 64",
              "    cap_add: [SYS_ADMIN]", "  evil:", "      - LOG_LEVEL=debug", "    network_mode: host",
              "        limits:", "    image: evil/evil:1.0.0@" + DIGESTS[9], "", "#", "  # ok",
              "    volumes: ['/:/host']", "services:", "x-defaults: &defaults"]


@st.composite
def service_block(draw, name, image_line=None):
    L = [f"  {name}:"]
    L.append(image_line or f"    image: example/{name}:1.2.3@{DIGESTS[0]}")
    if draw(st.booleans()):
        L.append("    # keep in sync with upstream")
    env = draw(st.lists(st.tuples(st.sampled_from(ENV_KEYS), st.sampled_from(LEVELS + ["UTC", "1000", "json"])),
                        max_size=3, unique_by=lambda t: t[0]))
    if env:
        L.append("    environment:")
        if draw(st.booleans()):
            L += [f"      - {k}={v}" for k, v in env]
        else:
            L += [f"      {k}: {v}" for k, v in env]
    if draw(st.booleans()):
        L += ["    deploy:", "      resources:"]
        for sect in draw(st.sampled_from([["limits"], ["reservations"], ["limits", "reservations"]])):
            L.append(f"        {sect}:")
            for k in draw(st.sampled_from([["memory"], ["cpus"], ["memory", "cpus"]])):
                L.append(f"          {k}: {draw(BASE_MEM if k == 'memory' else BASE_CPUS)}")
    for extra in draw(st.lists(st.sampled_from(EXTRAS), max_size=2, unique=True)):
        L += extra.replace("{name}", name).split("\n")
    return L


@st.composite
def compose_file(draw, image_lines=False):
    """A valid (PyYAML-loadable) compose file without Jinja."""
    names = draw(st.lists(st.sampled_from(SERVICES), min_size=1, max_size=4, unique=True))
    L = ["x-defaults: &defaults", "  restart: unless-stopped", "  deploy:", "    resources:", "      limits:",
         "        memory: 256M", "", "services:"]
    for name in names:
        img = draw(image_line(name)) if image_lines else None
        L += draw(service_block(name, img))
    if draw(st.booleans()):
        L += ["", "networks:", "  internal:", "    driver: bridge"]
    text = "\n".join(L) + "\n"
    yaml.safe_load(text)        # the generator itself must produce valid YAML
    return text


def _set_value(line, value, trailing):
    m = re.match(r"^(\s*(?:-\s+)?[A-Za-z0-9_.]+(?::\s+|=))", line)
    return (m.group(1) if m else line + " ") + value + trailing


@st.composite
def edited(draw, base: str, value_ops=True):
    """`base` with 1-3 edits: allowed-shape value changes, comments, and
    structural damage of every kind."""
    lines = base.split("\n")
    for _ in range(draw(st.integers(1, 3))):
        ops = ["comment_insert", "comment_edit", "comment_delete", "insert", "delete", "replace", "reindent",
               "swap", "append"]
        if value_ops:
            ops += ["res", "res", "res", "lvl", "lvl"]
        op = draw(st.sampled_from(ops))
        n = len(lines)
        pos = draw(st.integers(0, n - 1))
        res = [i for i, l in enumerate(lines) if re.match(r"^\s+(memory|cpus):", l)]
        lvl = [i for i, l in enumerate(lines) if re.search(r"LOG_?LEVEL|LEVEL", l)]
        comments = [i for i, l in enumerate(lines) if l.lstrip().startswith("#")]
        if op == "res" and res:
            i = draw(st.sampled_from(res))
            key = "memory" if "memory" in lines[i] else "cpus"
            lines[i] = _set_value(lines[i], draw(MEM_VALUE if key == "memory" else CPU_VALUE), draw(TRAILING))
        elif op == "lvl" and lvl:
            i = draw(st.sampled_from(lvl))
            lines[i] = _set_value(lines[i], draw(LEVEL_VALUE), draw(TRAILING))
        elif op == "comment_insert":
            lines.insert(pos, " " * draw(st.integers(0, 12)) + "#" + draw(TEXT))
        elif op == "comment_edit" and comments:
            i = draw(st.sampled_from(comments))
            lines[i] = lines[i] + draw(TEXT)
        elif op == "comment_delete" and comments:
            del lines[draw(st.sampled_from(comments))]
        elif op == "insert":
            lines.insert(pos, draw(st.one_of(st.sampled_from(POOL_LINES), TEXT)))
        elif op == "append":
            lines[pos] = lines[pos] + draw(TEXT)
        elif op == "delete" and n > 1:
            del lines[pos]
        elif op == "replace":
            lines[pos] = draw(st.one_of(st.sampled_from(POOL_LINES), TEXT))
        elif op == "reindent":
            d = draw(st.sampled_from([-4, -2, -1, 1, 2, 4]))
            lines[pos] = (" " * d + lines[pos]) if d > 0 else lines[pos][min(-d, len(lines[pos]) - len(lines[pos].lstrip(" "))):]
        elif op == "swap" and pos + 1 < n:
            lines[pos], lines[pos + 1] = lines[pos + 1], lines[pos]
    return "\n".join(lines)


@st.composite
def bot_config_pair(draw):
    base = draw(compose_file())
    head = draw(edited(base))
    return base, head


@st.composite
def image_line(draw, service):
    name = draw(st.sampled_from(IMAGE_NAMES))
    tag = draw(version())[0]
    quote = draw(st.sampled_from(["", "", "'", '"']))
    comment = draw(st.sampled_from(["", "", "  # pinned"]))
    return f"    image: {render_ref(name, tag, draw(st.sampled_from(DIGESTS)), quote, comment)}"


@st.composite
def image_pair(draw):
    """A compose file and a head with its image lines bumped (any way),
    plus maybe other edits."""
    base = draw(compose_file(image_lines=True))
    blines = base.split("\n")
    lines = list(blines)
    idx = [i for i, l in enumerate(lines) if re.match(r"^    image: ", l)]
    for i in draw(st.lists(st.sampled_from(idx), min_size=1, max_size=2, unique=True)) if idx else []:
        m = re.match(r"^    image: (['\"]?)([^@'\"#\s]+?)(?::([^:@/'\"#\s]+))?(?:@(sha256:[0-9a-f]+))?\1(.*)$",
                     lines[i])
        if not m:
            continue
        quote, name, tag, digest, rest = m.groups()
        how = draw(st.sampled_from(["tag", "tag", "digest", "name", "nodigest", "notag", "raw", "pair"]))
        new_name, new_tag, new_digest = name, tag, draw(st.sampled_from(DIGESTS))
        if how == "tag":
            new_tag = draw(ARBITRARY_TAG)
        elif how == "pair":
            (otag, *_), (ntag, *_) = draw(version_pair())
            blines[i] = f"    image: {render_ref(name, otag, digest, quote, rest)}"
            new_tag = ntag
        elif how == "name":
            new_name = draw(st.sampled_from(IMAGE_NAMES + [name.upper(), name + "x", LOOKALIKE["a"].join(name.split("a", 1))]))
        elif how == "nodigest":
            new_digest = None
        elif how == "notag":
            new_tag = None
        if how == "raw":
            lines[i] = "    image: " + draw(TEXT)
        else:
            lines[i] = f"    image: {render_ref(new_name, new_tag, new_digest, draw(st.sampled_from(['', quote])), draw(TRAILING))}"
    base, head = "\n".join(blines), "\n".join(lines)
    if draw(st.integers(0, 3)) == 0:
        head = draw(edited(head, value_ops=False))
    return base, head


def evaluate(*, author, files, composes, labels=(), merger=MERGER, target="main"):
    return policy.evaluate_mr(author=author, target_branch=target, labels=list(labels), files=files,
                              composes=composes, allowlist=ALLOW, codeowners_text=CODEOWNERS, merger=merger)


# ===========================================================================
# 1. Paths
# ===========================================================================

class PathProperties(unittest.TestCase):

    @given(NEVER_PATH.flatmap(path_variant))
    @example("docs/../ansible/inventory.md")
    @example("docs/../CLAUDE.md")
    def test_never_list_wins_over_every_allowlist(self, path):
        """No spelling of a never-list path is classified as an allowed kind."""
        kind, why = botconfig.classify(path, BC)
        if kind is not None:
            self.assertFalse(on_never_list(canonical(path)), f"{path!r} names never-list {canonical(path)!r}")
            self.assertFalse(on_never_list(path))

    @given(st.lists(ANY_PATH, min_size=1, max_size=4),
           st.sampled_from([MOLTBOT, RENOVATE]),
           st.booleans())
    def test_eligible_paths(self, paths, author, with_compose):
        """An eligible MR's paths are exactly the listed compose/CI files
        (image classes) or allowlisted, non-never paths (bot-config)."""
        files = [FileChange(path=p) for p in paths]
        composes = {p: ("# docs\n", "# docs, edited\n") for p in paths}
        if with_compose:
            base = f"services:\n  web:\n    image: example/web:1.2.3@{DIGESTS[0]}\n"
            files.append(FileChange(path=C))
            composes[C] = (base, base.replace("1.2.3@" + DIGESTS[0], "1.2.4@" + DIGESTS[1]))
        v = evaluate(author=author, files=files, composes=composes)
        if not v.eligible:
            return
        all_paths = [f.path for f in files]
        if v.bot_config:
            for p in all_paths:
                self.assertIsNotNone(botconfig.classify(p, BC)[0])
                self.assertFalse(on_never_list(canonical(p)), f"bot-config accepted {p!r} -> {canonical(p)!r}")
        else:
            self.assertTrue(set(all_paths) <= COMPOSE_FILES | CI_FILES, all_paths)

    @given(st.sampled_from(["new_file", "deleted_file", "renamed_file", "mode_changed"]),
           st.sampled_from([MOLTBOT, RENOVATE]), ANY_PATH)
    def test_added_deleted_renamed_mode_changed_never_eligible(self, flag, author, old_path):
        base = f"services:\n  web:\n    image: example/web:1.2.3@{DIGESTS[0]}\n    deploy:\n" \
               "      resources:\n        limits:\n          memory: 128M\n"
        for head in (base.replace("1.2.3@" + DIGESTS[0], "1.2.4@" + DIGESTS[1]), base.replace("128M", "192M")):
            fc = FileChange(path=C, old_path=old_path if flag == "renamed_file" else None, **{flag: True})
            v = evaluate(author=author, files=[fc], composes={C: (base, head)})
            self.assertFalse(v.eligible, (flag, head))

    @given(st.integers(31, 80))
    def test_bot_config_file_cap(self, n):
        paths = [f"docs/note{i}.md" for i in range(n)]
        v = evaluate(author=MOLTBOT, files=[FileChange(path=p) for p in paths],
                     composes={p: ("a\n", "b\n") for p in paths})
        self.assertFalse(v.eligible)


# ===========================================================================
# 2. Bot-config compose edits
# ===========================================================================

def _env_entry(value):
    """(key, level) of a list-form `KEY=value` entry or None."""
    if isinstance(value, str) and "=" in value:
        k, _, v = value.partition("=")
        return k, v
    return None


class BotConfigComposeProperties(unittest.TestCase):

    def check_semantics(self, base, head, changes):
        """Accepted head means: only resource values within the ratio and
        log levels changed, on services the class may touch."""
        assert_no_smuggling(self, base, head)
        b = yaml.safe_load(base)
        try:
            h = yaml.safe_load(head)
        except yaml.YAMLError as e:
            self.fail(f"accepted a change that makes the file invalid YAML: {e}")
        lo, hi = BC["min_ratio"], BC["max_ratio"]
        for path, old, new in ydiff(b, h):
            note(f"diff at {path}: {old!r} -> {new!r}")
            self.assertGreaterEqual(len(path), 3, path)
            self.assertEqual(path[0], "services", path)
            svc = path[1]
            self.assertNotIn(svc, BC_DENIED)
            self.assertNotIn("openclaw", svc)
            self.assertNotIn("moltbot", svc)
            self.assertIsNot(old, MISSING, f"accepted an added key {path}")
            self.assertIsNot(new, MISSING, f"accepted a removed key {path}")
            if path[2] == "deploy":
                self.assertEqual(len(path), 6, path)
                self.assertEqual(path[3], "resources")
                self.assertIn(path[4], ("limits", "reservations"))
                self.assertIn(path[5], ("memory", "cpus"))
                conv = docker_bytes if path[5] == "memory" else docker_cpus
                o, n = conv(old), conv(new)
                if o is None or n is None:
                    continue       # Docker refuses the value: a failed deploy, not a bigger limit
                self.assertGreater(o, 0)
                self.assertTrue(lo - 1e-9 <= n / o <= hi + 1e-9, f"{path}: {old!r} -> {new!r} is {n / o}x")
                if path[5] == "memory":
                    self.assertGreaterEqual(int(n), 1, "a 0-byte memory value means no limit")
            elif path[2] == "environment":
                self.assertEqual(len(path), 4, path)
                if isinstance(path[3], int):
                    self.assertIsNotNone(_env_entry(old))
                    (ok, ov), (nk, nv) = _env_entry(old), _env_entry(new)
                    self.assertEqual(ok, nk)
                    key, level = nk, nv.split(" #")[0].strip()
                    # the policy reads one matching pair of quotes as quoting
                    # (in list form Docker passes them on; a junk level, not more)
                    if len(level) >= 2 and level[0] == level[-1] and level[0] in "'\"":
                        level = level[1:-1]
                else:
                    key, level = path[3], "off" if new is False else str(new)
                self.assertRegex(key, r"^[A-Z0-9_]*LOG_?LEVEL$")
                self.assertIn(level.lower(), botconfig.LOG_LEVELS)
            else:
                self.fail(f"accepted a change at {path}")

    @given(bot_config_pair())
    @example(("services:\n  web:\n    image: a/b:1.0.0@" + DIGESTS[0] + "\n    environment:\n      - LOG_LEVEL=info\n",
              "services:\n  web:\n    image: a/b:1.0.0@" + DIGESTS[0] + "\n    environment:\n"
              "      - LOG_LEVEL=debug # {{ lookup('pipe', 'id') }}\n"))
    @example(("services:\n  web:\n    deploy:\n      resources:\n        limits:\n          memory: 128M\n",
              "services:\n  web:\n    deploy:\n      resources:\n        limits:\n"
              "          memory: 192M # x\r    privileged: true\n"))
    @example(("services:\n  web:\n    command:\n      - |\n        # a\n        run\n    deploy:\n      resources:\n"
              "        limits:\n          memory: 128M\n",
              "services:\n  web:\n    command:\n      - |\n        # a\n        # b\n        run\n    deploy:\n"
              "      resources:\n        limits:\n          memory: 192M\n"))
    @example(("services:\n  web:\n    environment:\n      LOG_LEVEL: crit\n",
              "services:\n  web:\n    environment:\n      LOG_LEVEL: 'debug\n"))
    @example(("services:\n  web:\n    entrypoint: \"run\n      # x\n      --flag\"\n    deploy:\n      resources:\n"
              "        limits:\n          memory: 128M\n",
              "services:\n  web:\n    entrypoint: \"run\n      --flag\"\n    deploy:\n      resources:\n"
              "        limits:\n          memory: 192M\n"))
    def test_accepted_compose_edit_changes_only_allowed_values(self, pair):
        base, head = pair
        try:
            changes = botconfig.diff_compose_config(base, head, C, BC, ALLOW)
        except PolicyError:
            return
        self.check_semantics(base, head, changes)

    @given(bot_config_pair(), st.sampled_from([MOLTBOT, MOLTBOT, RENOVATE, "root"]))
    def test_whole_mr_bot_config(self, pair, author):
        base, head = pair
        v = evaluate(author=author, files=[FileChange(path=C)], composes={C: (base, head)})
        if v.eligible and v.bot_config:
            self.check_semantics(base, head, v.bot_changes)

    @given(BASE_MEM, MEM_VALUE)
    @example("64M", "32\tM")
    def test_memory_units_cannot_escape_the_ratio(self, old, new):
        base = f"services:\n  web:\n    deploy:\n      resources:\n        limits:\n          memory: {old}\n"
        head = base.replace(f"memory: {old}", f"memory: {new}")
        assume(head != base)
        try:
            botconfig.diff_compose_config(base, head, C, BC, ALLOW)
        except PolicyError:
            return
        self.check_semantics(base, head, [])

    @given(BASE_CPUS, CPU_VALUE)
    def test_cpus_cannot_escape_the_ratio(self, old, new):
        base = f"services:\n  web:\n    deploy:\n      resources:\n        limits:\n          cpus: {old}\n"
        head = base.replace(f"cpus: {old}", f"cpus: {new}")
        assume(head != base)
        try:
            botconfig.diff_compose_config(base, head, C, BC, ALLOW)
        except PolicyError:
            return
        self.check_semantics(base, head, [])

    @given(st.text(max_size=200), st.text(max_size=200))
    def test_arbitrary_text_raises_only_policy_errors(self, base, head):
        try:
            botconfig.diff_compose_config(base, head, C, BC, ALLOW)
        except PolicyError:
            pass


# ===========================================================================
# 3. classify_tag_change
# ===========================================================================

class TagProperties(unittest.TestCase):

    @given(version_pair(), st.booleans())
    def test_classification_matches_components(self, pair, zero_minor):
        (otag, onums, oflav, ofn), (ntag, nnums, nflav, nfn) = pair
        try:
            kind = policy.classify_tag_change(otag, ntag, zero_minor)
        except PolicyError:
            kind = None
        if kind is None:
            # completeness: a plain same-flavor upgrade within a major is classified
            if (oflav == nflav and onums[0] == nnums[0] and (nnums, nfn) > (onums, ofn)
                    and (onums[0] != 0 or onums[1] == nnums[1] or zero_minor)):
                self.fail(f"{otag} -> {ntag} should be patch/minor")
            return
        self.assertIn(kind, ("patch", "minor"))
        self.assertEqual(oflav, nflav, f"flavor change {otag} -> {ntag} classified {kind}")
        self.assertEqual(onums[0], nnums[0], f"major change {otag} -> {ntag} classified {kind}")
        if onums[0] == 0 and not zero_minor:
            self.assertEqual(onums[1], nnums[1], f"0.x minor change {otag} -> {ntag} classified {kind}")
        self.assertEqual(kind == "minor", onums[1] != nnums[1])
        self.assertGreater((nnums, nfn), (onums, ofn), f"{otag} -> {ntag} is not an upgrade")

    @given(ARBITRARY_TAG, ARBITRARY_TAG, st.booleans())
    def test_never_raises_anything_but_policy_errors(self, old, new, zero_minor):
        try:
            kind = policy.classify_tag_change(old, new, zero_minor)
        except PolicyError:
            return
        self.assertIn(kind, ("patch", "minor"))
        for t in (old, new):
            self.assertRegex(t, r"^v?\d+\.\d+")

    @given(st.sampled_from(NON_VERSION_TAGS), ARBITRARY_TAG, st.booleans(), st.booleans())
    def test_non_version_tags_are_never_patch_or_minor(self, nv, other, nv_first, zero_minor):
        # (Arabic-Indic "١.٢" is a version to the policy's \d; registries allow
        # only ASCII tags, so it is in the list only as a non-ASCII probe.)
        old, new = (nv, other) if nv_first else (other, nv)
        with self.assertRaises(PolicyError):
            policy.classify_tag_change(old, new, zero_minor)


# ===========================================================================
# 4. Image class: diff_compose and the whole-MR gates
# ===========================================================================

def _image_parts(ref):
    if not isinstance(ref, str):
        return None
    m = re.match(r"^([a-z0-9][a-z0-9._/:-]*?)(?::([A-Za-z0-9_.-]+))?(?:@(sha256:[0-9a-f]{64}))?$", ref)
    return m.groups() if m else None


def check_image_semantics(self, base, head, changes, loader=yaml.safe_load, allowed_path=None):
    assert_no_smuggling(self, base, head, jinja=loader is not load_ci)
    b = loader(base)
    try:
        h = loader(head)
    except yaml.YAMLError as e:
        self.fail(f"accepted a change that makes the file invalid YAML: {e}")
    allowed_path = allowed_path or (lambda p: len(p) == 3 and p[0] == "services" and p[2] == "image")
    for path, old, new in ydiff(b, h):
        note(f"diff at {path}: {old!r} -> {new!r}")
        self.assertTrue(allowed_path(path), f"accepted a change at {path}: {old!r} -> {new!r}")
        o, n = _image_parts(old), _image_parts(new)
        self.assertIsNotNone(o, old)
        self.assertIsNotNone(n, new)
        self.assertEqual(o[0], n[0], "image name changed")
        self.assertIsNotNone(n[2], "new image not pinned by digest")
        if o[1] != n[1]:
            self.assertIsNotNone(o[1])
            self.assertIsNotNone(n[1])
            policy.classify_tag_change(o[1], n[1], True)   # must not be major (raises otherwise)


class ImageClassProperties(unittest.TestCase):

    @given(image_pair())
    def test_accepted_compose_diff_is_an_image_bump(self, pair):
        base, head = pair
        try:
            changes = policy.diff_compose(base, head, C, policy.zero_minor_services(ALLOW))
        except PolicyError:
            return
        check_image_semantics(self, base, head, changes)
        for c in changes:
            self.assertEqual(c.old.name, c.new.name)
            self.assertTrue(c.new.digest)
            self.assertIn(c.kind, ("digest", "patch", "minor"))

    @given(image_pair(), st.sampled_from([RENOVATE, MOLTBOT, "root", MERGER]),
           st.lists(st.sampled_from(["freeze", "major", "security", "renovate", "Freeze", "automerge"]),
                    unique=True, max_size=3),
           st.sampled_from([MERGER, MOLTBOT, RENOVATE, None]),
           st.sampled_from(["main", "main", "Main", "develop"]))
    def test_whole_mr_gates(self, pair, author, labels, merger, target):
        base, head = pair
        v = evaluate(author=author, files=[FileChange(path=C)], composes={C: (base, head)}, labels=labels,
                     merger=merger, target=target)
        if not v.eligible:
            return
        self.assertNotEqual(author, merger, "author == merger")
        self.assertEqual(target, "main")
        self.assertNotIn("freeze", labels)
        self.assertNotIn("major", labels)
        self.assertFalse(v.major)
        if v.bot_config:
            self.assertNotIn("security", labels)
            self.assertIn(author, BC["authors"])
            return
        self.assertIn(author, ALLOW["authors"])
        check_image_semantics(self, base, head, v.changes)
        info = policy.compose_service_info(head)
        binfo = policy.compose_service_info(base)
        for c in v.changes:
            self.assertNotIn(c.service, DENIED_SERVICES, "denylisted service eligible")
            self.assertNotIn(c.new.name, DENIED_IMAGES, "denylisted image eligible")
            for i in (info, binfo):
                self.assertFalse((i.get(c.service) or policy.ServiceInfo()).profiles, "profile-only service")

    @given(st.text(max_size=300), st.text(max_size=300))
    def test_arbitrary_text_raises_only_policy_errors(self, base, head):
        try:
            policy.diff_compose(base, head)
        except PolicyError:
            pass
        try:
            policy.diff_ci_images(base, head)
        except PolicyError:
            pass

    @given(st.integers(200, 3000))
    def test_huge_files(self, n):
        base = "services:\n" + "".join(f"  s{i}:\n    image: example/s{i}:1.2.3@{DIGESTS[0]}\n" for i in range(n))
        head = base.replace(f"example/s{n - 1}:1.2.3@{DIGESTS[0]}", f"example/s{n - 1}:1.2.4@{DIGESTS[1]}")
        self.assertEqual([c.service for c in policy.diff_compose(base, head)], [f"s{n - 1}"])
        head2 = head + "  extra:\n    image: x:1.0.0@" + DIGESTS[2] + "\n"
        with self.assertRaises(PolicyError):
            policy.diff_compose(base, head2)


# ===========================================================================
# 5. CI image class
# ===========================================================================

@st.composite
def ci_file(draw):
    def ref():
        return render_ref(draw(st.sampled_from(IMAGE_NAMES)), draw(version())[0], draw(st.sampled_from(DIGESTS)),
                          draw(st.sampled_from(["", "", "'", '"'])), draw(st.sampled_from(["", "  # pinned"])))
    L = ["default:", "  tags:", "    - unprotected", "", "variables:", "  GIT_DEPTH: 5",
         f"  IMAGE: {ref()}", ""]
    if draw(st.booleans()):
        L += [f"image: {ref()}", ""]
    if draw(st.booleans()):
        L += [".tpl: &tpl", f"  name: {ref()}", "  entrypoint: [\"\"]", ""]
        L += ["uses:tpl:", "  image: *tpl", "  script:", "    - run", ""]
    if draw(st.booleans()):
        L += [".other: &other", f"  name: {ref()}", "", "env:job:", "  environment: *other", "  script:",
              "    - run", ""]
    for i in range(draw(st.integers(1, 4))):
        form = draw(st.sampled_from(["plain", "mapping", "services", "environment", "script"]))
        L.append(f"job{i}:")
        if form == "mapping":
            L += ["  image:", f"    name: {ref()}", "    entrypoint: [\"\"]"]
        else:
            L.append(f"  image: {ref()}")
        if form == "services":
            L += ["  services:", f"    - name: {ref()}", "      alias: db"]
        if form == "environment":
            L += ["  environment:", f"    name: {ref()}"]
        L += ["  script:", "    - |", f"      image: {ref()}", "      echo done", ""]
    text = "\n".join(L)
    load_ci(text)
    return text


@st.composite
def ci_pair(draw):
    base = draw(ci_file())
    blines = base.split("\n")
    lines = list(blines)
    idx = [i for i, l in enumerate(lines) if re.search(r"(image|name|IMAGE): \S", l)]
    for i in draw(st.lists(st.sampled_from(idx), min_size=1, max_size=2, unique=True)):
        m = re.match(r"^(\s*(?:-\s+)?\w+: )(['\"]?)([^@'\"#\s]+?)(?::([^:@/'\"#\s]+))?(?:@(sha256:[0-9a-f]+))?\2(.*)$",
                     lines[i])
        if not m:
            continue
        lead, quote, name, tag, _, rest = m.groups()
        how = draw(st.sampled_from(["digest", "digest", "tag", "pair", "pair", "name", "nodigest", "raw"]))
        new_name, new_tag, new_digest = name, tag, draw(st.sampled_from(DIGESTS))
        if how == "tag":
            new_tag = draw(ARBITRARY_TAG)
        elif how == "pair":
            (otag, *_), (ntag, *_) = draw(version_pair())
            blines[i] = f"{lead}{render_ref(name, otag, DIGESTS[0], quote, rest)}"
            new_tag = ntag
        elif how == "name":
            new_name = draw(st.sampled_from(IMAGE_NAMES))
        elif how == "nodigest":
            new_digest = None
        if how == "raw":
            lines[i] = lead + draw(TEXT)
        else:
            lines[i] = f"{lead}{render_ref(new_name, new_tag, new_digest, quote, draw(TRAILING))}"
    base, head = "\n".join(blines), "\n".join(lines)
    if draw(st.integers(0, 3)) == 0:
        head = draw(edited(head, value_ops=False))
    return base, head


def _ci_allowed_path(path):
    if len(path) >= 2 and path[-1] == "image":
        return True
    if len(path) >= 3 and path[-2:] == ("image", "name"):
        return True
    return len(path) == 2 and str(path[0]).startswith(".") and path[1] == "name"


class CiImageProperties(unittest.TestCase):

    @given(ci_pair(), st.sampled_from([RENOVATE, RENOVATE, MOLTBOT]),
           st.lists(st.sampled_from(["freeze", "major", "security"]), unique=True, max_size=2))
    @example(("job0:\n  image: nginx:1.0@" + DIGESTS[0] + "\n  script:\n    - |\n      image: nginx:1.0@" + DIGESTS[0]
              + "\n      echo done\n",
              "job0:\n  image: nginx:1.0@" + DIGESTS[0] + "\n  script:\n    - |\n      image: nginx:1.0@" + DIGESTS[1]
              + "\n      echo done\n"), RENOVATE, [])
    def test_accepted_ci_change_is_an_image_bump(self, pair, author, labels):
        base, head = pair
        v = evaluate(author=author, files=[FileChange(path=CI)], composes={CI: (base, head)}, labels=labels)
        if not v.eligible:
            return
        self.assertTrue(v.ci_image)
        self.assertIn(author, ALLOW["ci_images"]["authors"])
        self.assertNotIn("freeze", labels)
        self.assertNotIn("major", labels)
        for c in v.changes:
            self.assertNotIn(c.new.name, DENIED_IMAGES)
            self.assertNotIn(c.new.name, ALLOW["ci_images"].get("denylist") or {})
        check_image_semantics(self, base, head, v.changes, loader=load_ci,
                                                   allowed_path=_ci_allowed_path)

    @given(ci_pair(), ANY_PATH)
    def test_ci_class_with_any_other_file_never_eligible(self, pair, other):
        assume(other != CI)
        base, head = pair
        v = evaluate(author=RENOVATE, files=[FileChange(path=CI), FileChange(path=other)],
                     composes={CI: (base, head), other: ("a\n", "b\n")})
        self.assertFalse(v.eligible)


if __name__ == "__main__":
    unittest.main()
