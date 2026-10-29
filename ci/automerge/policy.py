"""Auto-merge policy: pure, side-effect-free checks.

Everything in this module operates on plain data (file contents, path lists,
label lists) so it can be unit tested without GitLab or a host. The CLI in
automerge.py fetches that data and calls in here.

The core rule: an MR qualifies only if its compose change is a set of
in-place edits to `image:` lines, each keeping the same image name and moving
only the tag (patch/minor) and/or the digest, for services on the allowlist.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

COMPOSE_PATH = "docker/docker-compose.yml"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):\s*(#.*)?$")
_IMAGE_RE = re.compile(r"^(\s+)image:\s*(\S.*?)\s*$")
# major.minor[.patch[.build]] followed by an optional flavor suffix
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
# trailing git-hash-like build id (e.g. plex "-cb3ebc72d"); ignored for flavor
_HASH_SUFFIX_RE = re.compile(r"-[0-9a-f]{7,40}$")


class PolicyError(Exception):
    """A change is outside the policy. The message is the human-readable reason."""


class MajorVersionChange(PolicyError):
    """A major-version tag change (or a 0.x minor one). Never auto-merged; for a
    stateful service it is also a heavy change (see `is_stateful`)."""

    def __init__(self, msg: str, service: Optional[str] = None, image: Optional[str] = None):
        super().__init__(msg)
        self.service = service
        self.image = image


# ---------------------------------------------------------------------------
# Image references
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImageRef:
    name: str
    tag: Optional[str]
    digest: Optional[str]

    def __str__(self) -> str:
        s = self.name
        if self.tag:
            s += ":" + self.tag
        if self.digest:
            s += "@" + self.digest
        return s


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        q = value[0]
        end = value.find(q, 1)
        if end == -1:
            raise PolicyError(f"unterminated quoted image value: {value!r}")
        rest = value[end + 1:].strip()
        if rest and not rest.startswith("#"):
            raise PolicyError(f"unexpected content after quoted image value: {value!r}")
        return value[1:end]
    # unquoted: drop a trailing comment
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_image_ref(value: str) -> ImageRef:
    raw = _strip_yaml_scalar(value)
    if "{{" in raw or "{%" in raw or "$" in raw:
        raise PolicyError(f"templated image reference is not eligible: {raw!r}")
    name_tag, sep, digest = raw.partition("@")
    if sep and not _DIGEST_RE.match(digest):
        raise PolicyError(f"malformed digest in {raw!r}")
    last = name_tag.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name_tag.rsplit(":", 1)
    else:
        name, tag = name_tag, None
    if not name or not _NAME_RE.match(name):
        raise PolicyError(f"malformed image name in {raw!r}")
    return ImageRef(name=name, tag=tag or None, digest=digest or None)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Version:
    nums: Tuple[int, int, int, int]
    flavor: str            # suffix with digits masked, e.g. "-ls#" or "-alpine#.#-perl"
    suffix_nums: Tuple[int, ...]


def parse_version(tag: str) -> Optional[Version]:
    m = _VERSION_RE.match(tag)
    if not m:
        return None
    nums = tuple(int(g) if g is not None else 0 for g in m.groups()[:4])
    suffix = _HASH_SUFFIX_RE.sub("", m.group(5))
    flavor = re.sub(r"\d+", "#", suffix)
    suffix_nums = tuple(int(x) for x in re.findall(r"\d+", suffix))
    return Version(nums=nums, flavor=flavor, suffix_nums=suffix_nums)  # type: ignore[arg-type]


def classify_tag_change(old_tag: str, new_tag: str) -> str:
    """Return 'patch' or 'minor', or raise PolicyError."""
    old_v, new_v = parse_version(old_tag), parse_version(new_tag)
    if old_v is None or new_v is None:
        raise PolicyError(
            f"tag {old_tag!r} -> {new_tag!r} is not a recognisable version; "
            "only digest-only updates qualify for non-version tags")
    if old_v.flavor != new_v.flavor:
        raise PolicyError(f"tag flavor changed ({old_tag!r} -> {new_tag!r})")
    if (new_v.nums, new_v.suffix_nums) <= (old_v.nums, old_v.suffix_nums):
        raise PolicyError(f"tag {old_tag!r} -> {new_tag!r} is not an upgrade")
    o_major, o_minor = old_v.nums[0], old_v.nums[1]
    n_major, n_minor = new_v.nums[0], new_v.nums[1]
    if n_major != o_major:
        raise MajorVersionChange(f"major version change ({old_tag} -> {new_tag})")
    if o_major == 0 and n_minor != o_minor:
        # semver: in 0.x the minor component is the breaking one
        raise MajorVersionChange(f"0.x minor version change counts as major ({old_tag} -> {new_tag})")
    return "minor" if n_minor != o_minor else "patch"


# ---------------------------------------------------------------------------
# Compose structure
# ---------------------------------------------------------------------------

def compose_line_services(text: str) -> Tuple[List[Optional[str]], Dict[int, str]]:
    """Map each line to the service it belongs to, and find service image lines.

    Returns (service_per_line, image_lines) where image_lines maps a 0-based line
    index to its service, for `image:` keys that are direct children of a
    service under the top-level `services:` mapping.
    """
    lines = text.split("\n")
    owner: List[Optional[str]] = [None] * len(lines)
    images: Dict[int, str] = {}
    in_services = False
    svc_indent: Optional[int] = None
    child_indent: Optional[int] = None
    current: Optional[str] = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            owner[i] = current
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = bool(re.match(r"^services:\s*(#.*)?$", line))
            svc_indent = child_indent = None
            current = None
            continue
        if not in_services:
            continue
        key = _KEY_RE.match(line)
        if svc_indent is None and key:
            svc_indent = indent
        if key and indent == svc_indent:
            current = key.group(2)
            child_indent = None
            owner[i] = current
            continue
        owner[i] = current
        if current is None or svc_indent is None or indent <= svc_indent:
            continue
        if child_indent is None:
            child_indent = indent
        m = _IMAGE_RE.match(line)
        if m and indent == child_indent:
            images[i] = current
    return owner, images


@dataclass
class ImageChange:
    service: str
    line: int              # 1-based
    old: ImageRef
    new: ImageRef
    kind: str              # 'digest' | 'patch' | 'minor'
    old_line: str
    new_line: str

    def as_dict(self) -> dict:
        return {
            "service": self.service, "line": self.line, "kind": self.kind,
            "old": str(self.old), "new": str(self.new),
            "old_line": self.old_line, "new_line": self.new_line,
        }


def _image_value(line: str) -> Tuple[str, str]:
    m = _IMAGE_RE.match(line)
    assert m
    return m.group(1), m.group(2)


def diff_compose(base: str, head: str) -> List[ImageChange]:
    """Compare two versions of the compose file. Raise PolicyError unless every
    difference is an in-place, same-image tag/digest edit of a service image line."""
    b_lines, h_lines = base.split("\n"), head.split("\n")
    if len(b_lines) != len(h_lines):
        raise PolicyError(
            f"compose line count changed ({len(b_lines)} -> {len(h_lines)}); "
            "only in-place image line edits qualify")
    b_owner, b_images = compose_line_services(base)
    h_owner, h_images = compose_line_services(head)
    changes: List[ImageChange] = []
    for i, (bl, hl) in enumerate(zip(b_lines, h_lines)):
        if bl == hl:
            continue
        where = f"line {i + 1}"
        if i not in b_images or i not in h_images:
            svc = h_owner[i] or b_owner[i]
            raise PolicyError(
                f"{where}{' (service ' + svc + ')' if svc else ''} is not a service image line")
        if b_images[i] != h_images[i]:
            raise PolicyError(f"{where}: service structure changed")
        service = h_images[i]
        b_indent, b_val = _image_value(bl)
        h_indent, h_val = _image_value(hl)
        if b_indent != h_indent:
            raise PolicyError(f"{where} ({service}): indentation changed")
        try:
            old, new = parse_image_ref(b_val), parse_image_ref(h_val)
        except PolicyError as e:
            raise PolicyError(f"{where} ({service}): {e}")
        if old.name != new.name:
            raise PolicyError(f"{where} ({service}): image name changed ({old.name} -> {new.name})")
        if not new.digest:
            raise PolicyError(f"{where} ({service}): new image is not pinned by digest")
        if old.tag == new.tag:
            if old.digest == new.digest:
                raise PolicyError(f"{where} ({service}): non-functional edit to image line")
            kind = "digest"
        else:
            if old.tag is None or new.tag is None:
                raise PolicyError(f"{where} ({service}): tag added or removed")
            try:
                kind = classify_tag_change(old.tag, new.tag)
            except MajorVersionChange as e:
                raise MajorVersionChange(f"{where} ({service}): {e}", service=service, image=new.name)
            except PolicyError as e:
                raise PolicyError(f"{where} ({service}): {e}")
        changes.append(ImageChange(service, i + 1, old, new, kind, bl, hl))
    # Every service whose image changed must be fully accounted for by those lines.
    seen = {}
    for c in changes:
        if c.service in seen:
            raise PolicyError(f"service {c.service} has more than one changed image line")
        seen[c.service] = c
    return changes


# ---------------------------------------------------------------------------
# CODEOWNERS
# ---------------------------------------------------------------------------

def parse_codeowners(text: str) -> List[Tuple[str, List[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        parts = line.split()
        if len(parts) >= 2:
            rules.append((parts[0], parts[1:]))
    return rules


def codeowners_match(pattern: str, path: str) -> bool:
    path = path.lstrip("/")
    anchored = pattern.startswith("/")
    pat = pattern.lstrip("/")
    if pat.endswith("/"):
        if anchored:
            return path.startswith(pat)
        return path.startswith(pat) or ("/" + pat) in ("/" + path)
    if anchored or "/" in pat:
        if pat.endswith("/**"):
            return path.startswith(pat[:-2])
        # a pattern naming a directory also covers everything below it
        return fnmatch.fnmatchcase(path, pat) or path.startswith(pat + "/")
    # unanchored file pattern: match basename anywhere
    return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pat) or fnmatch.fnmatchcase(path, pat)


def protected_paths(paths: List[str], codeowners_text: str) -> List[str]:
    rules = parse_codeowners(codeowners_text)
    return [p for p in paths if any(codeowners_match(pat, p) for pat, _ in rules)]


# ---------------------------------------------------------------------------
# Update types and the fast-track class
# ---------------------------------------------------------------------------
# Every allowlisted service may take these update kinds unless its entry sets
# `updates:` to fewer. A fast-track image (allowlist `fast_track.images`) is
# further limited to digest/patch on every service that uses it, in every mode:
# its minor and major updates always go to a human.

UPDATE_KINDS = ("digest", "patch", "minor")
FAST_TRACK_KINDS = ("digest", "patch")


def _kinds(value, allowed: Tuple[str, ...], where: str) -> Tuple[str, ...]:
    if value is None:
        return allowed
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise PolicyError(f"{where}: `updates` must be a non-empty list")
    bad = [k for k in value if k not in allowed]
    if bad:
        raise PolicyError(f"{where}: update kinds {bad} are not allowed here (allowed: {list(allowed)})")
    return tuple(value)


def fast_track_config(allowlist: dict) -> dict:
    """Validated `fast_track` section: {enabled, authors, max_per_24h, images: {name: kinds}}.
    Raises PolicyError on a malformed section, so a typo fails closed."""
    ft = allowlist.get("fast_track") or {}
    images = {}
    for name, opts in (ft.get("images") or {}).items():
        if opts is not None and not isinstance(opts, dict):
            raise PolicyError(f"fast_track image {name}: options must be a mapping")
        images[name] = _kinds((opts or {}).get("updates"), FAST_TRACK_KINDS, f"fast_track image {name}")
    return {
        "enabled": ft.get("enabled") is True,
        "authors": list(ft.get("authors") or []),
        "max_per_24h": int(ft.get("max_per_24h", 4)),
        "images": images,
    }


def allowed_kinds(service: str, image: str, allowlist: dict) -> Tuple[str, ...]:
    """Update kinds an allowlisted service may take for this image (empty: none)."""
    svc = (allowlist.get("services") or {}).get(service)
    if svc is None:
        return ()
    kinds = _kinds((svc or {}).get("updates"), UPDATE_KINDS, f"service {service}")
    ft = fast_track_config(allowlist)["images"].get(image)
    if ft is not None:
        kinds = tuple(k for k in kinds if k in ft)
    return kinds


def fast_track_verdict(author: str, changes: List["ImageChange"], allowlist: dict) -> Tuple[bool, str]:
    """Does an (otherwise eligible) MR fall in the fast-track class?

    All of: the class is enabled; the author is a fast-track author; the MR
    changes exactly one image (one name, one old and one new reference, on
    one or more services); that image is listed; and every change is a kind
    the listed image allows. Returns (qualifies, explanation)."""
    ft = fast_track_config(allowlist)
    if not ft["enabled"]:
        return False, "fast track is disabled"
    if author not in ft["authors"]:
        return False, f"author {author!r} is not a fast-track author"
    if not changes:
        return False, "no image changes"
    names = {c.new.name for c in changes}
    if len(names) != 1:
        return False, "changes more than one image: " + ", ".join(sorted(names))
    name = names.pop()
    if {(str(c.old), str(c.new)) for c in changes} != {(str(changes[0].old), str(changes[0].new))}:
        return False, f"{name}: services move between different references"
    kinds = ft["images"].get(name)
    if kinds is None:
        return False, f"{name} is not a fast-track image"
    off = sorted({c.kind for c in changes} - set(kinds))
    if off:
        return False, f"{name}: {', '.join(off)} update is not fast-tracked (allowed: {', '.join(kinds)})"
    return True, (f"fast-track: {changes[0].kind} update of {name} on "
                  + ", ".join(c.service for c in changes))


# ---------------------------------------------------------------------------
# Heavy changes
# ---------------------------------------------------------------------------
# A heavy change is a major-version upgrade of a stateful service: one listed
# under `stateful.services` in the allowlist, or running an image listed under
# `stateful.images` (databases). A heavy change would need last night's backup
# to have succeeded and a fresh ZFS snapshot of the service's dataset. The bot
# cannot verify either reliably, so heavy changes stay manual-only in every
# storage state (and rule 3 already refuses every major-version change). The
# `no-heavy` storage level (rule 8) is the second line if that ever changes.

HEAVY_MANUAL_REASON = ("heavy change (major version of a stateful service): manual only; "
                       "the bot cannot verify last night's backup or a fresh snapshot of the dataset")


def is_stateful(service: Optional[str], image: Optional[str], allowlist: dict) -> bool:
    st = allowlist.get("stateful") or {}
    if service and service in (st.get("services") or []):
        return True
    if image:
        base = image.rsplit("/", 1)[-1]
        return any(image == i or base == i for i in (st.get("images") or []))
    return False


# ---------------------------------------------------------------------------
# Whole-MR evaluation (MR-local checks; global gates are added by the caller)
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    eligible: bool = True
    reasons: List[str] = field(default_factory=list)   # why NOT eligible
    notes: List[str] = field(default_factory=list)     # informational
    changes: List[ImageChange] = field(default_factory=list)
    fast_track: bool = False                           # eligible AND in the fast-track class
    heavy: bool = False                                # major version of a stateful service

    def reject(self, reason: str) -> None:
        self.eligible = False
        self.fast_track = False
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "fast_track": self.fast_track, "heavy": self.heavy,
                "reasons": self.reasons,
                "notes": self.notes, "changes": [c.as_dict() for c in self.changes]}


@dataclass
class FileChange:
    path: str
    old_path: Optional[str] = None
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    mode_changed: bool = False


def evaluate_mr(*, author: str, target_branch: str, labels: List[str],
                files: List[FileChange], base_compose: Optional[str],
                head_compose: Optional[str], allowlist: dict,
                codeowners_text: str, merger: Optional[str] = None) -> Verdict:
    """`merger` is the identity that would merge (the sweep's token user). When
    given, an MR authored by that identity is rejected: author != merger."""
    v = Verdict()
    authors = allowlist.get("authors") or []
    if author not in authors:
        v.reject(f"author {author!r} is not in the auto-merge author list")
    if merger is not None and author == merger:
        v.reject(f"author {author!r} is the merging identity; the merger must not be the author")
    if target_branch != allowlist.get("target_branch", "main"):
        v.reject(f"target branch {target_branch!r} is not {allowlist.get('target_branch', 'main')!r}")
    freeze_label = allowlist.get("freeze_label", "freeze")
    if freeze_label in labels:
        v.reject(f"MR has the {freeze_label!r} label")

    allowed_files = set(allowlist.get("allowed_files") or [COMPOSE_PATH])
    paths = sorted({f.path for f in files} | {f.old_path for f in files if f.old_path})
    if not files:
        v.reject("MR has no file changes")
    for f in files:
        if f.new_file or f.deleted_file or f.renamed_file or f.mode_changed:
            v.reject(f"{f.path}: file added, deleted, renamed or mode-changed")
    extra = [p for p in paths if p not in allowed_files]
    if extra:
        v.reject("touches files outside the auto-merge scope: " + ", ".join(extra))

    # CODEOWNERS: compose is special-cased (allowed only for image-only diffs,
    # verified below); any other code-owned path disqualifies.
    owned = [p for p in protected_paths(paths, codeowners_text) if p != COMPOSE_PATH]
    if owned:
        v.reject("touches CODEOWNERS-protected paths: " + ", ".join(owned))

    if COMPOSE_PATH in paths:
        if base_compose is None or head_compose is None:
            v.reject("could not read both versions of the compose file")
        else:
            try:
                v.changes = diff_compose(base_compose, head_compose)
            except MajorVersionChange as e:
                v.reject(f"compose: {e}")
                if is_stateful(e.service, e.image, allowlist):
                    v.heavy = True
                    v.reject(HEAVY_MANUAL_REASON)
            except PolicyError as e:
                v.reject(f"compose: {e}")
            for r in change_kind_reasons(v.changes, allowlist):
                v.reject(r)
            for c in v.changes:
                v.notes.append(f"{c.service}: {c.kind} update {c.old.tag or ''} -> {c.new.tag or ''}"
                               f" ({(c.new.digest or '')[:19]})")
            if not v.changes and v.eligible:
                v.reject("compose file changed but no image updates were found")
    elif v.eligible:
        v.reject("MR does not change the compose file")

    if v.eligible:
        try:
            v.fast_track, why = fast_track_verdict(author, v.changes, allowlist)
        except PolicyError as e:
            v.fast_track, why = False, f"fast track: {e}"
        v.notes.append(why if v.fast_track else f"not fast-track: {why}")
    return v


def change_kind_reasons(changes: List[ImageChange], allowlist: dict) -> List[str]:
    """Per-change allowlist check (service listed, update kind allowed). Used by
    evaluate_mr and again by plan-deploy on the merged commit."""
    reasons = []
    services = allowlist.get("services") or {}
    for c in changes:
        if c.service not in services:
            reasons.append(f"service {c.service!r} is not on the auto-merge allowlist")
            continue
        try:
            kinds = allowed_kinds(c.service, c.new.name, allowlist)
        except PolicyError as e:
            reasons.append(f"allowlist: {e}")
            continue
        if c.kind not in kinds:
            reasons.append(f"{c.service}: {c.kind} update of {c.new.name} is not allowed "
                           f"(allowed: {', '.join(kinds) or 'none'})")
    return reasons


# ---------------------------------------------------------------------------
# Host storage (rule 8)
# ---------------------------------------------------------------------------
# Decided by the redundancy each vdev has left, not by the DEGRADED label:
#
#   blocked   every auto-merge stops: a pool is SUSPENDED, UNAVAIL or FAULTED
#             (any health other than ONLINE/DEGRADED), I/O is suspended, data
#             errors are reported, the output cannot be parsed, or a data vdev
#             has no redundancy left (raidzN: N members unavailable; an N-way
#             mirror: N-1 members unavailable; a single-disk vdev never has any).
#   no-heavy  a resilver or scrub is running: heavy changes (see
#             `is_stateful`) stop, normal image updates go ahead.
#   open      anything else, including a DEGRADED pool that still has
#             redundancy and no resilver running.
#
# Log, cache and spare vdevs do not count; special and dedup vdevs do.

STORAGE_OPEN, STORAGE_NO_HEAVY, STORAGE_BLOCKED = "open", "no-heavy", "blocked"
_STORAGE_RANK = {STORAGE_OPEN: 0, STORAGE_NO_HEAVY: 1, STORAGE_BLOCKED: 2}

UNAVAILABLE_STATES = {"FAULTED", "UNAVAIL", "OFFLINE", "REMOVED"}
POOL_OK_HEALTH = {"ONLINE", "DEGRADED"}
DATA_SECTIONS = {"data", "special", "dedup"}
_SECTION_LABELS = {"logs", "cache", "spares", "special", "dedup"}
_REPAIR_SCAN_RE = re.compile(r"\b(resilver|scrub|rebuild)\b.*\bin progress\b")


@dataclass
class VdevNode:
    name: str
    state: str
    counts: Optional[Tuple[int, int, int]]
    section: str
    children: List["VdevNode"] = field(default_factory=list)


@dataclass
class PoolStatus:
    name: str
    state: str = ""
    resilvering: bool = False
    repairing: bool = False        # resilver, rebuild or scrub in progress
    suspended: bool = False
    errors_line: str = ""
    # vdev name -> (state, (read, write, cksum) or None when the row has no counters)
    rows: Dict[str, Tuple[str, Optional[Tuple[int, int, int]]]] = field(default_factory=dict)
    # top-level vdevs in config order, with their members as children
    vdevs: List[VdevNode] = field(default_factory=list)


def _counter(tok: str) -> int:
    """`zpool status -p` prints exact integers; anything else fails closed."""
    if not tok.isdigit():
        raise PolicyError(f"unparsable error counter {tok!r} (expected `zpool status -p` output)")
    return int(tok)


def parse_zpool_status(text: str) -> Dict[str, PoolStatus]:
    pools: Dict[str, PoolStatus] = {}
    cur: Optional[PoolStatus] = None
    in_config = False
    section = "data"
    stack: List[Tuple[int, VdevNode]] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        m = re.match(r"^\s*(pool|state|status|scan|errors):\s*(.*)$", line)
        if m and not raw.startswith("\t"):
            key, val = m.group(1), m.group(2).strip()
            if key == "pool":
                cur = pools.setdefault(val, PoolStatus(val))
                in_config = False
            elif cur is None:
                raise PolicyError(f"zpool status: {key!r} line before any pool line")
            elif key == "state":
                cur.state = val
            elif key == "status":
                if "suspend" in val.lower():
                    cur.suspended = True
            elif key == "scan":
                if _REPAIR_SCAN_RE.search(val):
                    cur.repairing = True
                    cur.resilvering = "resilver" in val or "rebuild" in val
            elif key == "errors":
                cur.errors_line = val
                in_config = False
            continue
        if cur is None:
            continue
        if stripped.startswith("config:"):
            in_config, section, stack = True, "data", []
            continue
        if "suspend" in stripped.lower() and not in_config:
            cur.suspended = True   # continuation of a status: paragraph
        if not in_config or not stripped:
            continue
        toks = stripped.split()
        if toks[0] == "NAME":
            continue
        body = line[1:] if line.startswith("\t") else line
        depth = (len(body) - len(body.lstrip(" "))) // 2
        if depth == 0:
            # the pool's own row (data section) or a section label
            if toks[0] == cur.name:
                section = "data"
            elif toks[0] in _SECTION_LABELS:
                section = toks[0]
            else:
                raise PolicyError(f"zpool status: unexpected top-level row {toks[0]!r}")
            stack = []
            if len(toks) < 2:
                continue
        if len(toks) < 2:
            raise PolicyError(f"zpool status: vdev row without a state: {stripped!r}")
        name, state = toks[0], toks[1]
        counts = None
        if len(toks) >= 5:
            counts = (_counter(toks[2]), _counter(toks[3]), _counter(toks[4]))
        cur.rows[name] = (state, counts)
        if depth == 0:
            continue
        node = VdevNode(name, state, counts, section)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if depth == 1:
            cur.vdevs.append(node)
        elif not stack or stack[-1][0] != depth - 1:
            raise PolicyError(f"zpool status: cannot place vdev row {name!r}")
        else:
            stack[-1][1].children.append(node)
        stack.append((depth, node))
    return pools


def _parity(vdev: VdevNode) -> int:
    """Members a top-level vdev can lose without losing data."""
    m = re.match(r"^(raidz|draid)([123])?[-:]", vdev.name)
    if m:
        return int(m.group(2) or 1)
    if re.match(r"^mirror-\d+$", vdev.name):
        return len(vdev.children) - 1
    return 0   # a single disk (or file) vdev


def _member_unavailable(member: VdevNode, repairing: bool) -> bool:
    if not member.children:
        return member.state in UNAVAILABLE_STATES
    # replacing-N / spare-N: the slot is available while one child holds the
    # data; during a resilver only when every child is available, since a
    # replacement that is still resilvering does not hold all of it yet.
    up = [c for c in member.children if not _member_unavailable(c, repairing)]
    if not up:
        return True
    return repairing and len(up) < len(member.children)


def vdev_redundancy(vdev: VdevNode, repairing: bool) -> Tuple[int, List[str]]:
    """(redundancy left, unavailable member names) for a top-level vdev."""
    members = vdev.children or [vdev]
    parity = _parity(vdev)
    down = [m.name for m in members if _member_unavailable(m, repairing)]
    if vdev.state in UNAVAILABLE_STATES:
        return -1, down or [vdev.name]
    return parity - len(down), down


def _pool_verdict(name: str, health: str, st: PoolStatus) -> Tuple[str, str]:
    if health not in POOL_OK_HEALTH or st.state not in POOL_OK_HEALTH:
        return STORAGE_BLOCKED, f"storage pool {name} is {health if health not in POOL_OK_HEALTH else st.state}"
    if st.suspended:
        return STORAGE_BLOCKED, f"storage pool {name}: I/O suspended"
    if st.errors_line != "No known data errors":
        return STORAGE_BLOCKED, f"storage pool {name}: errors: {st.errors_line or 'line missing'}"
    data_vdevs = [v for v in st.vdevs if v.section in DATA_SECTIONS]
    if not data_vdevs:
        return STORAGE_BLOCKED, f"storage pool {name}: no vdevs parsed from zpool status"
    degraded = []
    for v in data_vdevs:
        left, down = vdev_redundancy(v, st.repairing)
        if left <= 0:
            why = f"{', '.join(down)} unavailable" if down else "no parity"
            return STORAGE_BLOCKED, f"storage pool {name}: {v.name} has no redundancy left ({why})"
        if down:
            degraded.append(f"{v.name}: {', '.join(down)} unavailable, redundancy {left} left")
    detail = f" ({'; '.join(degraded)})" if degraded else ""
    if st.repairing:
        what = "resilvering" if st.resilvering else "scrubbing"
        return STORAGE_NO_HEAVY, f"{name} {health}, {what}{detail}: heavy changes wait"
    return STORAGE_OPEN, f"{name} {health}{detail}"


def storage_verdict(list_text: str, status_text: str) -> Tuple[str, str]:
    """Decide rule 8 from `zpool list -H -o name,health` and `zpool status -p` output.
    Returns (level, message), level one of STORAGE_OPEN / STORAGE_NO_HEAVY / STORAGE_BLOCKED.
    Anything unexpected fails closed (blocked)."""
    health = {}
    for line in list_text.strip().splitlines():
        parts = line.split()
        if len(parts) != 2:
            return STORAGE_BLOCKED, f"unexpected zpool list line: {line!r}"
        health[parts[0]] = parts[1]
    if not health:
        return STORAGE_BLOCKED, "storage check returned no pools"
    try:
        status = parse_zpool_status(status_text)
    except PolicyError as e:
        return STORAGE_BLOCKED, f"storage check: {e}"
    level, notes = STORAGE_OPEN, []
    for name, h in health.items():
        st = status.get(name)
        if st is None:
            return STORAGE_BLOCKED, f"storage pool {name}: missing from zpool status"
        lvl, msg = _pool_verdict(name, h, st)
        if lvl == STORAGE_BLOCKED:
            return lvl, msg
        if _STORAGE_RANK[lvl] > _STORAGE_RANK[level]:
            level = lvl
        notes.append(msg)
    return level, "storage pools: " + "; ".join(notes)
