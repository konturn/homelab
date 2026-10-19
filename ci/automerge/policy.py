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
        raise PolicyError(f"major version change ({old_tag} -> {new_tag})")
    if o_major == 0 and n_minor != o_minor:
        # semver: in 0.x the minor component is the breaking one
        raise PolicyError(f"0.x minor version change counts as major ({old_tag} -> {new_tag})")
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
# Whole-MR evaluation (MR-local checks; global gates are added by the caller)
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    eligible: bool = True
    reasons: List[str] = field(default_factory=list)   # why NOT eligible
    notes: List[str] = field(default_factory=list)     # informational
    changes: List[ImageChange] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.eligible = False
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "reasons": self.reasons, "notes": self.notes,
                "changes": [c.as_dict() for c in self.changes]}


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
            except PolicyError as e:
                v.reject(f"compose: {e}")
            services = allowlist.get("services") or {}
            for c in v.changes:
                if c.service not in services:
                    v.reject(f"service {c.service!r} is not on the auto-merge allowlist")
                else:
                    v.notes.append(f"{c.service}: {c.kind} update {c.old.tag or ''} -> {c.new.tag or ''}"
                                   f" ({(c.new.digest or '')[:19]})")
            if not v.changes and v.eligible:
                v.reject("compose file changed but no image updates were found")
    elif v.eligible:
        v.reject("MR does not change the compose file")
    return v
