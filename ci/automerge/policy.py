"""Auto-merge policy: pure, side-effect-free checks.

Everything in this module operates on plain data (file contents, path lists,
label lists) so it can be unit tested without GitLab or a host. The CLI in
automerge.py fetches that data and calls in here.

The core rule: an MR qualifies only if its change is a set of in-place edits
to `image:` lines of deployed compose files, each keeping the same image name
and moving only the tag (patch/minor) and/or the digest. Every service
qualifies except those on the denylist, those that run only under a compose
profile, and update kinds a service's options restrict. Major versions, and
tag changes whose version scheme is ambiguous, never qualify.
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
from dataclasses import dataclass, field
from typing import Collection, Dict, List, Optional, Set, Tuple

# The router's compose file. Other deployed compose files are listed under
# `compose_files` in the policy file (see `compose_files`).
COMPOSE_PATH = "docker/docker-compose.yml"
COMPOSE_GLOB = re.compile(r"^docker/docker-compose(-[^/]+)?\.ya?ml$")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._/:-]*$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+):\s*(#.*)?$")
_IMAGE_RE = re.compile(r"^(\s+)image:\s*(\S.*?)\s*$")
# major.minor[.patch[.build]] followed by an optional flavor suffix
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
# trailing git-hash-like build id (e.g. plex "-cb3ebc72d"); ignored for flavor
_HASH_SUFFIX_RE = re.compile(r"-[0-9a-f]{7,40}$")
# Line breaks other than "\n" (the ones str.splitlines() knows). The policy
# reads files line by line on "\n", but YAML also breaks lines on "\r" (and
# YAML 1.1 parsers on NEL, LS and PS), and Jinja, which renders the compose
# files before Docker reads them, turns a lone "\r" into "\n". A changed line
# carrying one could hide extra YAML lines (`privileged: true`, volumes) inside
# what the policy reads as one comment or value.
_ODD_LINE_BREAK = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


class PolicyError(Exception):
    """A change is outside the policy. The message is the human-readable reason."""


class MajorVersionChange(PolicyError):
    """A major-version tag change, a 0.x minor one, or a tag change whose
    version scheme is ambiguous (treated as major). Never auto-merged; for a
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


def check_changed_line(line: str, where: str, jinja: bool) -> None:
    """Refuse a changed line whose meaning the line-by-line checks cannot see:
    one with a line break other than "\n" (_ODD_LINE_BREAK), or, in a Jinja
    template (`jinja`, the compose files), one with a brace anywhere, a trailing
    comment included: Ansible renders `{{ lookup('pipe', ...) }}` in a YAML
    comment like anywhere else, on the deploy runner."""
    if _ODD_LINE_BREAK.search(line):
        raise PolicyError(f"{where}: line contains a line break other than \\n (e.g. \\r)")
    if jinja and ("{" in line or "}" in line):
        raise PolicyError(f"{where}: changed line contains '{{' or '}}' (the compose files are Jinja templates)")


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


def classify_tag_change(old_tag: str, new_tag: str, zero_minor_is_minor: bool = False) -> str:
    """Return 'patch' or 'minor', or raise PolicyError.

    Anything that cannot be shown to stay within one major version raises
    MajorVersionChange: a different major, a 0.x minor, a tag that is not a
    major.minor[.patch] version (`latest`, `18993091-ls1`, a date), or a
    changed flavor. Where the scheme is ambiguous, the change counts as major.
    `zero_minor_is_minor` counts a 0.x minor as a minor (a service whose 0.x
    minors are routine releases, see `zero_minor_services`)."""
    old_v, new_v = parse_version(old_tag), parse_version(new_tag)
    if old_v is None or new_v is None:
        raise MajorVersionChange(
            f"tag {old_tag!r} -> {new_tag!r} is not a recognisable major.minor version; "
            "ambiguous version scheme, treated as major (only digest-only updates qualify)")
    if old_v.flavor != new_v.flavor:
        raise MajorVersionChange(
            f"tag flavor changed ({old_tag!r} -> {new_tag!r}); treated as major")
    if (new_v.nums, new_v.suffix_nums) <= (old_v.nums, old_v.suffix_nums):
        raise PolicyError(f"tag {old_tag!r} -> {new_tag!r} is not an upgrade")
    o_major, o_minor = old_v.nums[0], old_v.nums[1]
    n_major, n_minor = new_v.nums[0], new_v.nums[1]
    if n_major != o_major:
        raise MajorVersionChange(f"major version change ({old_tag} -> {new_tag})")
    if o_major == 0 and n_minor != o_minor and not zero_minor_is_minor:
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
class ServiceInfo:
    profiles: List[str] = field(default_factory=list)
    healthcheck: bool = False


_CHILD_KEY_RE = re.compile(r"^(\s+)([A-Za-z0-9_.-]+):\s*(.*?)\s*$")


def compose_service_info(text: str) -> Dict[str, ServiceInfo]:
    """Per service: its compose `profiles` and whether it defines a healthcheck.

    A service with `profiles` starts only when one of them is enabled (the
    retired `wordpress` profile, for example). A per-service deploy names the
    service explicitly, which starts it whatever its profiles, so such a
    service must never be auto-merged."""
    owner, _ = compose_line_services(text)
    lines = text.split("\n")
    info: Dict[str, ServiceInfo] = {}
    svc_indent: Dict[str, int] = {}
    child_indent: Dict[str, int] = {}
    for i, line in enumerate(lines):
        svc = owner[i]
        if svc is None or not line.strip() or line.lstrip().startswith("#"):
            continue
        info.setdefault(svc, ServiceInfo())
        indent = len(line) - len(line.lstrip(" "))
        if svc not in svc_indent:          # the service's own key line
            svc_indent[svc] = indent
            continue
        m = _CHILD_KEY_RE.match(line)
        if not m:
            continue
        # direct children only: the first deeper indent seen for the service
        if indent != child_indent.setdefault(svc, indent):
            continue
        key, value = m.group(2), m.group(3)
        if key == "healthcheck":
            info[svc].healthcheck = True
        elif key == "profiles":
            value = value.split("#", 1)[0].strip()
            if value.startswith("["):
                names = [n.strip().strip("'\"") for n in value.strip("[]").split(",") if n.strip()]
            else:
                names = []
                for nxt in lines[i + 1:]:
                    t = nxt.strip()
                    if not t or t.startswith("#"):
                        continue
                    if not t.startswith("- "):
                        break
                    names.append(t[2:].strip().strip("'\""))
            info[svc].profiles = names or ["(unparsed)"]
    return info


@dataclass
class ImageChange:
    service: str
    line: int              # 1-based
    old: ImageRef
    new: ImageRef
    kind: str              # 'digest' | 'patch' | 'minor'
    old_line: str
    new_line: str
    file: str = COMPOSE_PATH

    def as_dict(self) -> dict:
        return {
            "file": self.file, "service": self.service, "line": self.line, "kind": self.kind,
            "old": str(self.old), "new": str(self.new),
            "old_line": self.old_line, "new_line": self.new_line,
        }


def _image_value(line: str) -> Tuple[str, str]:
    m = _IMAGE_RE.match(line)
    assert m
    return m.group(1), m.group(2)


def diff_compose(base: str, head: str, path: str = COMPOSE_PATH,
                 zero_minor: Collection[str] = ()) -> List[ImageChange]:
    """Compare two versions of a compose file. Raise PolicyError unless every
    difference is an in-place, same-image tag/digest edit of a service image line.
    Services in `zero_minor` take 0.x minors as minors (`zero_minor_services`)."""
    b_lines, h_lines = base.split("\n"), head.split("\n")
    if len(b_lines) != len(h_lines):
        raise PolicyError(
            f"{path}: line count changed ({len(b_lines)} -> {len(h_lines)}); "
            "only in-place image line edits qualify")
    b_owner, b_images = compose_line_services(base)
    h_owner, h_images = compose_line_services(head)
    changes: List[ImageChange] = []
    for i, (bl, hl) in enumerate(zip(b_lines, h_lines)):
        if bl == hl:
            continue
        where = (f"{path} " if path != COMPOSE_PATH else "") + f"line {i + 1}"
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
        check_changed_line(hl, f"{where} ({service})", jinja=True)
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
                kind = classify_tag_change(old.tag, new.tag, service in zero_minor)
            except MajorVersionChange as e:
                raise MajorVersionChange(f"{where} ({service}): {e}", service=service, image=new.name)
            except PolicyError as e:
                raise PolicyError(f"{where} ({service}): {e}")
        changes.append(ImageChange(service, i + 1, old, new, kind, bl, hl, path))
    # Every service whose image changed must be fully accounted for by those lines.
    seen = {}
    for c in changes:
        if c.service in seen:
            raise PolicyError(f"service {c.service} has more than one changed image line")
        seen[c.service] = c
    return changes


# ---------------------------------------------------------------------------
# CI job images (the "CI image" class)
# ---------------------------------------------------------------------------
# A CI file (`ci_images.files`, .gitlab-ci.yml) may change only in place on
# an image line: a job's or template's `image: <ref>`, or `name: <ref>`
# directly under an `image:` mapping or under an anchored template that some
# job uses as `image: *anchor`. Nothing is deployed; the sweep watches the
# main pipeline of the merge instead (automerge.py, `ci_watch`).

CI_PATH = ".gitlab-ci.yml"
_CI_IMAGE_RE = re.compile(r"^(\s*)(image|name):\s*(\S.*?)\s*$")
_CI_KEY_RE = re.compile(r"^(\s*)(?:-\s+)?([A-Za-z0-9_.:/-]+):(?:\s+(.*?))?\s*$")


def _ci_structure(text: str):
    """Per line: (indent, key, value) for `key: value` lines, None otherwise;
    and the parent line index of each line (by indentation)."""
    lines = text.split("\n")
    info: List[Optional[Tuple[int, str, str]]] = []
    parent: List[Optional[int]] = []
    stack: List[Tuple[int, int]] = []           # (indent, line index)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            info.append(None)
            parent.append(None)
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent.append(stack[-1][1] if stack else None)
        m = _CI_KEY_RE.match(line)
        info.append((indent, m.group(2), m.group(3) or "") if m else None)
        stack.append((indent, i))
    return lines, info, parent


def ci_image_lines(text: str) -> Dict[int, str]:
    """0-based line index -> owning top-level key, for every image line of a
    CI file (see the section comment)."""
    lines, info, parent = _ci_structure(text)
    aliases = set(re.findall(r"^\s*image:\s*\*([A-Za-z0-9_-]+)\s*$", text, re.M))
    out: Dict[int, str] = {}
    for i, line in enumerate(lines):
        m = _CI_IMAGE_RE.match(line)
        if not m or info[i] is None or info[i][1] != m.group(2):
            continue
        value = m.group(3)
        if value[:1] in ("*", "&", "{", "[", "|", ">", "!") or value.startswith("#"):
            continue
        if m.group(2) == "name":
            p = parent[i]
            if p is None or info[p] is None:
                continue
            pkey, pval = info[p][1], info[p][2]
            anchor = re.match(r"^&([A-Za-z0-9_-]+)\s*(#.*)?$", pval)
            if not (pkey == "image" or (anchor and anchor.group(1) in aliases)):
                continue
        # every ancestor must be a mapping key (`job:`, `image:`, `.tpl: &a`):
        # an `image:` line inside a block scalar (`script: - |`) is script text
        top, inside_value = i, False
        while parent[top] is not None:
            top = parent[top]
            if info[top] is None or not re.match(r"^(&[A-Za-z0-9_-]+)?\s*(#.*)?$", info[top][2]):
                inside_value = True
        if inside_value or top == i:
            continue                            # a top-level `image:` is global (default) config
        out[i] = info[top][1]
    return out


def diff_ci_images(base: str, head: str, path: str = CI_PATH) -> List[ImageChange]:
    """Like diff_compose, for a CI file: every difference must be an in-place,
    same-image tag/digest edit of an image line (`ci_image_lines`). Each
    change's `service` is the job or template that owns the line."""
    b_lines, h_lines = base.split("\n"), head.split("\n")
    if len(b_lines) != len(h_lines):
        raise PolicyError(f"{path}: line count changed ({len(b_lines)} -> {len(h_lines)}); "
                          "only in-place image line edits qualify")
    b_img, h_img = ci_image_lines(base), ci_image_lines(head)
    changes: List[ImageChange] = []
    for i, (bl, hl) in enumerate(zip(b_lines, h_lines)):
        if bl == hl:
            continue
        where = f"{path} line {i + 1}"
        if i not in b_img or i not in h_img:
            raise PolicyError(f"{where} is not a job image line")
        if b_img[i] != h_img[i]:
            raise PolicyError(f"{where}: job structure changed")
        job = h_img[i]
        bm, hm = _CI_IMAGE_RE.match(bl), _CI_IMAGE_RE.match(hl)
        if (bm.group(1), bm.group(2)) != (hm.group(1), hm.group(2)):
            raise PolicyError(f"{where} ({job}): key or indentation changed")
        try:
            old, new = parse_image_ref(bm.group(3)), parse_image_ref(hm.group(3))
        except PolicyError as e:
            raise PolicyError(f"{where} ({job}): {e}")
        check_changed_line(hl, f"{where} ({job})", jinja=False)
        if old.name != new.name:
            raise PolicyError(f"{where} ({job}): image name changed ({old.name} -> {new.name})")
        if not new.digest:
            raise PolicyError(f"{where} ({job}): new image is not pinned by digest")
        if old.tag == new.tag:
            if old.digest == new.digest:
                raise PolicyError(f"{where} ({job}): non-functional edit to image line")
            kind = "digest"
        else:
            if old.tag is None or new.tag is None:
                raise PolicyError(f"{where} ({job}): tag added or removed")
            try:
                kind = classify_tag_change(old.tag, new.tag)
            except MajorVersionChange as e:
                raise MajorVersionChange(f"{where} ({job}): {e}", service=job, image=new.name)
            except PolicyError as e:
                raise PolicyError(f"{where} ({job}): {e}")
        changes.append(ImageChange(job, i + 1, old, new, kind, bl, hl, path))
    return changes


def ci_images_config(allowlist: dict) -> dict:
    """Validated `ci_images` section: {enabled, authors, files, denylist}.
    Raises PolicyError on anything malformed (fails closed)."""
    sec = allowlist.get("ci_images")
    if sec is None:
        return {"enabled": False, "authors": [], "files": [], "denylist": {}}
    if not isinstance(sec, dict):
        raise PolicyError("`ci_images` must be a mapping")
    files = sec.get("files") or [CI_PATH]
    if not isinstance(files, list) or any(not isinstance(f, str) or f.startswith("/") for f in files):
        raise PolicyError("ci_images.files must be a list of repository paths")
    deny = {}
    for name, reason in (sec.get("denylist") or {}).items():
        if not isinstance(reason, str) or not reason.strip():
            raise PolicyError(f"ci_images.denylist {name}: a reason is required")
        deny[name] = " ".join(reason.split())
    authors = sec.get("authors") or []
    if not isinstance(authors, list):
        raise PolicyError("ci_images.authors must be a list")
    return {"enabled": sec.get("enabled") is True, "authors": list(authors),
            "files": list(files), "denylist": deny}


def ci_files(allowlist: dict) -> List[str]:
    try:
        cfg = ci_images_config(allowlist)
    except PolicyError:
        return []
    return cfg["files"] if cfg["enabled"] else []


def ci_change_reasons(changes: List[ImageChange], allowlist: dict) -> List[str]:
    """Per-change checks of the CI image class: the global image denylist and
    `ci_images.denylist`. Used by evaluate_mr and again by the post-merge watch."""
    reasons = []
    d = denylist(allowlist)["images"]
    cd = ci_images_config(allowlist)["denylist"]
    for c in changes:
        why = d.get(c.new.name) or cd.get(c.new.name)
        if why:
            reasons.append(f"{c.service}: {DENIED_MARK} ({c.new.name}: {why})")
    return reasons


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
# Deployed compose files and their hosts
# ---------------------------------------------------------------------------
# Which host renders which compose file comes from `docker_compose_file_names`
# in ansible/inventory.yml (host vars over group vars over `all` vars, as in
# Ansible). The policy file maps each host to the prefix of its per-service
# deploy job (`<prefix>:deploy-service` in .gitlab-ci.yml) under `deploy_hosts`.

@dataclass(frozen=True)
class Deployment:
    host: str              # inventory host name
    job: Optional[str]     # deploy job prefix (None: no per-service deploy)
    project: str           # compose project name on the host
    address: str           # ansible_host (for the reachability check)


def compose_deployments(inventory: dict, allowlist: dict) -> Dict[str, List[Deployment]]:
    """Compose file (repo path) -> the hosts that render and run it."""
    jobs = allowlist.get("deploy_hosts") or {}
    out: Dict[str, List[Deployment]] = {}

    def walk(group: dict, inherited: dict) -> None:
        group = group or {}
        gvars = {**inherited, **(group.get("vars") or {})}
        for host, hvars in (group.get("hosts") or {}).items():
            eff = {**gvars, **(hvars or {})}
            for name in eff.get("docker_compose_file_names") or []:
                path = "docker/" + str(name).strip()
                dep = Deployment(host=host, job=jobs.get(host),
                                 project=str(eff.get("docker_compose_project_name") or "docker"),
                                 address=str(eff.get("ansible_host") or host))
                if dep not in out.setdefault(path, []):
                    out[path].append(dep)
        for child in (group.get("children") or {}).values():
            walk(child, gvars)

    walk(inventory.get("all") or {}, {})
    return out


def compose_files(allowlist: dict) -> List[str]:
    """Compose files the sweep may merge (`compose_files`; each must also be
    deployed by a host with a per-service deploy)."""
    return list(allowlist.get("compose_files") or allowlist.get("allowed_files") or [COMPOSE_PATH])


def default_deployments(allowlist: dict) -> Dict[str, List[Deployment]]:
    """Used when no inventory is given (unit tests): every listed compose file
    runs on the router."""
    return {f: [Deployment("router.lab.nkontur.com", "router", "docker", "10.4.0.1")]
            for f in compose_files(allowlist)}


# ---------------------------------------------------------------------------
# Update kinds, denylist and the fast-track class
# ---------------------------------------------------------------------------
# Every service may take digest, patch and minor updates unless it is on the
# denylist, runs only under a compose profile, or its entry under `services`
# sets `updates:` to fewer. Majors never qualify (classify_tag_change). A
# fast-track image (`fast_track.images`) is merged promptly for the kinds it
# lists; its other non-major kinds go through the general class.

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


def denylist(allowlist: dict) -> Dict[str, Dict[str, str]]:
    """{'services': {name: reason}, 'images': {name: reason}}. Every entry needs
    a reason; a malformed section raises PolicyError (fails closed)."""
    d = allowlist.get("denylist") or {}
    out: Dict[str, Dict[str, str]] = {"services": {}, "images": {}}
    for kind in ("services", "images"):
        for name, reason in (d.get(kind) or {}).items():
            if not isinstance(reason, str) or not reason.strip():
                raise PolicyError(f"denylist {kind} {name}: a reason is required")
            out[kind][name] = " ".join(reason.split())
    return out


def deny_reason(service: str, image: Optional[str], allowlist: dict) -> Optional[str]:
    d = denylist(allowlist)
    if service in d["services"]:
        return d["services"][service]
    if image and image in d["images"]:
        return d["images"][image]
    return None


def zero_minor_services(allowlist: dict) -> Set[str]:
    """Services whose 0.x minor updates count as minors (`zero_minor_is_minor:
    true` under `services`). Anything but a boolean raises PolicyError."""
    out = set()
    for name, svc in (allowlist.get("services") or {}).items():
        value = (svc or {}).get("zero_minor_is_minor", False)
        if not isinstance(value, bool):
            raise PolicyError(f"service {name}: `zero_minor_is_minor` must be true or false")
        if value:
            out.add(name)
    return out


# ---------------------------------------------------------------------------
# Pre-upgrade backups
# ---------------------------------------------------------------------------
# A service whose version change migrates its data one way (Home Assistant's
# recorder and .storage, InfluxDB's metadata) sets `pre_upgrade_backup: <type>`.
# Before a version (minor) update of it is deployed, the per-service deploy
# takes a backup with ci/automerge/pre-upgrade-backup.sh, and restores it if
# the rollback fires. Digest and patch updates take none.

PRE_UPGRADE_TYPES = ("homeassistant", "influxdb")
PRE_UPGRADE_KINDS = ("minor",)


def pre_upgrade_config(allowlist: dict) -> dict:
    """Validated `pre_upgrade_backups` section plus the per-service types:
    {dir, keep, rate_mb, timeout, services: {service: type}}. Raises
    PolicyError on anything malformed, so a typo fails closed."""
    services = {}
    for name, svc in (allowlist.get("services") or {}).items():
        t = (svc or {}).get("pre_upgrade_backup")
        if t is None:
            continue
        if t not in PRE_UPGRADE_TYPES:
            raise PolicyError(f"service {name}: `pre_upgrade_backup` must be one of {list(PRE_UPGRADE_TYPES)}")
        services[name] = t
    sec = allowlist.get("pre_upgrade_backups")
    if sec is None and not services:
        return {"dir": "", "keep": 2, "rate_mb": 40, "timeout": 1800, "services": {}}
    if not isinstance(sec, dict):
        raise PolicyError("`pre_upgrade_backups` must be a mapping (dir, keep, rate_mb, timeout)")
    root = sec.get("dir")
    if not isinstance(root, str) or not root.startswith("/") or root.rstrip("/") in ("", "/") \
            or root.startswith("/persistent_data"):
        raise PolicyError("pre_upgrade_backups.dir must be an absolute path on the ZFS pool")
    out = {"dir": root.rstrip("/"), "services": services}
    for key, default, low in (("keep", 2, 1), ("rate_mb", 40, 1), ("timeout", 1800, 60)):
        value = sec.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise PolicyError(f"pre_upgrade_backups.{key} must be an integer >= {low}")
        out[key] = value
    return out


def _path_part(tag: Optional[str]) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", tag or "untagged")


def pre_upgrade_backups(changes: List["ImageChange"], allowlist: dict, stamp: str) -> Dict[str, dict]:
    """service -> {type, dir} for the changes that need a pre-upgrade backup:
    version (minor) updates of services with `pre_upgrade_backup`. `stamp` is
    the UTC time (YYYYmmddTHHMMSSZ) that starts each backup directory name."""
    cfg = pre_upgrade_config(allowlist)
    out = {}
    for c in changes:
        t = cfg["services"].get(c.service)
        if t and c.kind in PRE_UPGRADE_KINDS:
            out[c.service] = {
                "type": t,
                "dir": f"{cfg['dir']}/{c.service}/{stamp}-{_path_part(c.old.tag)}-to-{_path_part(c.new.tag)}",
            }
    return out


def allowed_kinds(service: str, image: str, allowlist: dict) -> Tuple[str, ...]:
    """Update kinds a service may take for this image (empty: none)."""
    if deny_reason(service, image, allowlist):
        return ()
    svc = (allowlist.get("services") or {}).get(service) or {}
    return _kinds(svc.get("updates"), UPDATE_KINDS, f"service {service}")


def fast_track_verdict(author: str, changes: List["ImageChange"], allowlist: dict) -> Tuple[bool, str]:
    """Does an (otherwise eligible) MR fall in the fast-track class?

    All of: the class is enabled; the author is a fast-track author; the MR
    changes exactly one image (one name, one old and one new reference, on
    one or more services) in the router compose file; that image is listed;
    and every change is a kind the listed image allows. Returns
    (qualifies, explanation)."""
    ft = fast_track_config(allowlist)
    if not ft["enabled"]:
        return False, "fast track is disabled"
    if author not in ft["authors"]:
        return False, f"author {author!r} is not a fast-track author"
    if not changes:
        return False, "no image changes"
    if any(c.file != COMPOSE_PATH for c in changes):
        return False, f"fast track covers {COMPOSE_PATH} only"
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
# under `stateful.services` in the policy file, or running an image listed under
# `stateful.images` (databases). A heavy change would need last night's backup
# to have succeeded and a fresh ZFS snapshot of the service's dataset. The bot
# cannot verify either reliably, so heavy changes stay manual-only (and every
# major-version change is refused anyway).

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
# Dependency class (docs/AUTOMERGE.md, "Dependency updates")
# ---------------------------------------------------------------------------
# Renovate MRs that bump a version in our own build and deploy files: base
# images on Dockerfile FROM lines, Renovate-managed pin lines (npm global
# installs, `# renovate:` annotations) in Dockerfiles and ansible
# tasks/defaults, and the OpenClaw tool npm dependencies. Every changed line
# is checked here; anything the checks cannot read is refused (fails closed).

DEP_HOLD_JOB = "dep"
SECURITY_KINDS = ("digest", "patch")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*$")
_ANNOTATION = re.compile(r"^\s*#\s*renovate:\s*datasource=(\S+)\s+depName=(\S+)", re.A)
_NPM_GLOBAL = re.compile(r"npm install (?:-g|--global) (@?[a-z0-9][a-z0-9._-]*(?:/[a-z0-9._-]+)?)@(\d+\.\d+\.\d+)(?![\w.])", re.A)
# A version token: digits separated by dots, not glued to other word characters.
_VER_TOKEN = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+){1,3})(?![\w.]|-(?:rc|alpha|beta|pre|dev)\b)", re.A)
_HEX_TOKEN = re.compile(r"(?<![0-9a-fA-F])([0-9a-f]{64}|[0-9a-f]{128})(?![0-9a-fA-F])")
_FROM_RE = re.compile(r"^( *FROM +)((?:--[a-z][a-z-]*=[!-~]+ +)*)([!-~]+)( +AS +[A-Za-z0-9_.-]+)? *$", re.I | re.A)
_EXACT_SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DEP_SECTIONS = ("dependencies", "optionalDependencies", "devDependencies", "peerDependencies")


def _glob_re(pattern: str) -> "re.Pattern[str]":
    """A path glob: `*` matches within one path segment, `**` across segments."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def glob_match(pattern: str, path: str) -> bool:
    """`path` is a plain repository path (no `.`/`..` segments, no doubled or
    leading slash, ASCII) that `pattern` matches."""
    return bool(_SAFE_PATH.match(path)) and ".." not in path.split("/") \
        and bool(_glob_re(pattern).match(path))


def _reasons_map(value, where: str) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{where} must be a mapping of name: reason")
    out = {}
    for name, reason in value.items():
        if not isinstance(name, str) or not isinstance(reason, str) or not reason.strip():
            raise PolicyError(f"{where} {name}: a reason is required")
        out[name] = " ".join(reason.split())
    return out


def _str_list(value, where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise PolicyError(f"{where} must be a list of strings")
    return list(value)


def dependencies_config(allowlist: dict) -> dict:
    """Validated `dependencies` section. Raises PolicyError on anything
    malformed, so a typo fails closed."""
    sec = allowlist.get("dependencies")
    empty = {"enabled": False, "authors": [], "codeowner": "", "dockerfiles": [], "npm": {},
             "pin_files": [], "zero_minor": set(), "denylist": {}, "critical_files": {}, "critical_jobs": {}}
    if sec is None:
        return empty
    if not isinstance(sec, dict):
        raise PolicyError("`dependencies` must be a mapping")
    npm = sec.get("npm") or {}
    if not isinstance(npm, dict):
        raise PolicyError("dependencies.npm must be a mapping")
    if npm:
        for key in ("package_json", "lockfile", "registry"):
            if not isinstance(npm.get(key), str) or not npm[key]:
                raise PolicyError(f"dependencies.npm.{key} is required")
        if not npm["registry"].startswith("https://") or not npm["registry"].endswith("/"):
            raise PolicyError("dependencies.npm.registry must be an https:// URL ending in /")
        for key in ("package_json", "lockfile"):
            if not _SAFE_PATH.match(npm[key]):
                raise PolicyError(f"dependencies.npm.{key} must be a repository path")
    crit = sec.get("security_critical") or {}
    if not isinstance(crit, dict):
        raise PolicyError("dependencies.security_critical must be a mapping")
    codeowner = sec.get("codeowner") or ""
    if not isinstance(codeowner, str) or (codeowner and not codeowner.startswith("@")):
        raise PolicyError("dependencies.codeowner must be a CODEOWNERS owner (@name)")
    dockerfiles = _str_list(sec.get("dockerfiles"), "dependencies.dockerfiles")
    if any(not d.endswith("/Dockerfile") for d in dockerfiles):
        raise PolicyError("dependencies.dockerfiles entries must name Dockerfiles")
    return {
        "enabled": sec.get("enabled") is True,
        "authors": _str_list(sec.get("authors"), "dependencies.authors"),
        "codeowner": codeowner,
        "dockerfiles": dockerfiles,
        "npm": dict(npm),
        "pin_files": _str_list(sec.get("pin_files"), "dependencies.pin_files"),
        "zero_minor": set(_str_list(sec.get("zero_minor_is_minor"), "dependencies.zero_minor_is_minor")),
        "denylist": _reasons_map(sec.get("denylist"), "dependencies.denylist"),
        "critical_files": _reasons_map(crit.get("files"), "dependencies.security_critical.files"),
        "critical_jobs": _reasons_map(crit.get("ci_jobs"), "dependencies.security_critical.ci_jobs"),
    }


def dependency_file_kind(path: str, cfg: dict) -> Optional[str]:
    """'dockerfile' | 'package_json' | 'lockfile' | 'pins', or None when the
    class does not cover the path (or the class is disabled)."""
    if not cfg.get("enabled"):
        return None
    npm = cfg.get("npm") or {}
    if npm and path == npm.get("package_json"):
        return "package_json"
    if npm and path == npm.get("lockfile"):
        return "lockfile"
    if any(glob_match(p, path) for p in cfg["dockerfiles"]):
        return "dockerfile"
    if any(glob_match(p, path) for p in cfg["pin_files"]):
        return "pins"
    return None


def dependency_paths(paths: Collection[str], allowlist: dict) -> List[str]:
    """The paths the dependency class covers (empty if it is disabled or malformed)."""
    try:
        cfg = dependencies_config(allowlist)
    except PolicyError:
        return []
    return [p for p in paths if dependency_file_kind(p, cfg)]


def critical_job_reason(job: str, cfg: dict) -> Optional[str]:
    for pat, why in cfg["critical_jobs"].items():
        if fnmatch.fnmatchcase(job, pat):
            return why
    return None


@dataclass
class DepChange:
    file: str
    line: int              # 1-based; 0 for whole-file (npm) changes
    name: str              # image, package or depName
    old: str
    new: str
    kind: str              # 'digest' | 'patch' | 'minor' | 'checksum' | 'lock'
    old_line: str = ""
    new_line: str = ""

    def as_dict(self) -> dict:
        return {"file": self.file, "line": self.line, "name": self.name, "kind": self.kind,
                "old": self.old, "new": self.new}


def dockerfile_structure(text: str) -> List[str]:
    """Per line: 'blank', 'comment', 'cont' (a continuation of the previous
    instruction), 'heredoc' (here-document body) or 'instr:<NAME>'. Raises
    PolicyError for an `escape` parser directive (it changes what a line
    continuation is)."""
    lines = text.split("\n")
    for line in lines[:20]:
        s = line.strip()
        if not s.startswith("#"):
            break
        if re.match(r"^#\s*escape\s*=", s, re.I):
            raise PolicyError("Dockerfile sets an escape parser directive")
    out: List[str] = []
    in_cont = False
    heredocs: List[Tuple[str, bool]] = []
    for line in lines:
        if in_cont:
            out.append("cont")
            heredocs += [(m.group(2), bool(m.group(1))) for m in
                         re.finditer(r"<<(-?)\s*[\"']?([A-Za-z0-9_]+)[\"']?", line)]
            in_cont = line.rstrip().endswith("\\")
            continue
        if heredocs:
            delim, strip_tabs = heredocs[0]
            out.append("heredoc")
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                heredocs.pop(0)
            continue
        s = line.strip()
        if not s:
            out.append("blank")
        elif s.startswith("#"):
            out.append("comment")
        else:
            out.append("instr:" + s.split()[0].upper())
            heredocs += [(m.group(2), bool(m.group(1))) for m in
                         re.finditer(r"<<(-?)\s*[\"']?([A-Za-z0-9_]+)[\"']?", line)]
            in_cont = line.rstrip().endswith("\\")
    return out


def _version_only_change(old: str, new: str, where: str, zero_minor: bool) -> List[Tuple[str, str, str]]:
    """The (old, new, kind) version changes of a line whose only change is
    version tokens. Raises PolicyError (MajorVersionChange for a major)."""
    o_toks, n_toks = _VER_TOKEN.findall(old), _VER_TOKEN.findall(new)
    if _VER_TOKEN.sub("\0", old) != _VER_TOKEN.sub("\0", new) or len(o_toks) != len(n_toks):
        raise PolicyError(f"{where}: the change is not only a version")
    pairs = {(o, n) for o, n in zip(o_toks, n_toks) if o != n}
    if not pairs:
        raise PolicyError(f"{where}: non-functional edit")
    if len(pairs) > 1:
        raise PolicyError(f"{where}: more than one version changed")
    o, n = pairs.pop()
    return [(o, n, classify_tag_change(o, n, zero_minor))]


def _annotation(lines: List[str], i: int) -> Optional[str]:
    """depName of a `# renovate:` annotation on the line above line i."""
    if i == 0:
        return None
    m = _ANNOTATION.match(lines[i - 1])
    return m.group(2) if m else None


def _pin_block(lines: List[str], i: int) -> range:
    """The lines a pin's checksum may sit on: from the pin to the next blank
    line, at most 12 lines."""
    end = i + 1
    while end < len(lines) and end < i + 12 and lines[end].strip():
        end += 1
    return range(i + 1, end)


def _diff_pin_lines(path: str, base: str, head: str, cfg: dict, jinja: bool,
                    kinds: Optional[List[str]] = None, from_ok: bool = False) -> List[DepChange]:
    """In-place edits of Renovate-managed pin lines (and Dockerfile FROM lines
    when `from_ok`). `kinds` (Dockerfiles) is the per-line structure."""
    b_lines, h_lines = base.split("\n"), head.split("\n")
    if len(b_lines) != len(h_lines):
        raise PolicyError(f"{path}: line count changed ({len(b_lines)} -> {len(h_lines)}); "
                          "only in-place version edits qualify")
    changed = [i for i, (b, h) in enumerate(zip(b_lines, h_lines)) if b != h]
    pins: Dict[int, str] = {}
    out: List[DepChange] = []
    for i in changed:
        bl, hl = b_lines[i], h_lines[i]
        where = f"{path} line {i + 1}"
        check_changed_line(hl, where, jinja=jinja)
        if _ODD_LINE_BREAK.search(bl):
            raise PolicyError(f"{where}: line contains a line break other than \\n (e.g. \\r)")
        if kinds is not None and kinds[i] == "heredoc":
            raise PolicyError(f"{where} is inside a here-document")
        if from_ok and kinds is not None and kinds[i] == "instr:FROM":
            out.append(_diff_from_line(path, i, bl, hl, cfg))
            continue
        npm_b, npm_h = _NPM_GLOBAL.search(bl), _NPM_GLOBAL.search(hl)
        dep = _annotation(h_lines, i) if b_lines[i - 1:i] == h_lines[i - 1:i] else None
        npm_pin = bool(npm_b and npm_h and npm_b.group(1) == npm_h.group(1) and kinds is not None)
        if npm_pin:
            dep = npm_b.group(1)
        if dep is None:
            continue           # maybe a checksum line, checked below
        pins[i] = dep
        for o, n, kind in _version_only_change(bl, hl, where, dep in cfg["zero_minor"]):
            if npm_pin and (npm_b.group(2), npm_h.group(2)) != (o, n):
                raise PolicyError(f"{where}: the version that changed is not the {dep} pin")
            out.append(DepChange(path, i + 1, dep, o, n, kind, bl, hl))
    pin_blocks = {j: p for p in pins for j in _pin_block(h_lines, p)}
    for i in changed:
        if i in pins or (from_ok and kinds is not None and kinds[i] == "instr:FROM"):
            continue
        bl, hl = b_lines[i], h_lines[i]
        where = f"{path} line {i + 1}"
        if i not in pin_blocks:
            raise PolicyError(f"{where} is not a Renovate-managed version pin"
                              + (" or a FROM line" if from_ok else ""))
        if _HEX_TOKEN.sub("\0", bl) != _HEX_TOKEN.sub("\0", hl) or bl == hl:
            raise PolicyError(f"{where}: next to a pin, only a checksum may change")
        out.append(DepChange(path, i + 1, pins[pin_blocks[i]], _HEX_TOKEN.search(bl).group(1)[:12],
                             _HEX_TOKEN.search(hl).group(1)[:12], "checksum", bl, hl))
    # A pin with a checksum next to it must move it too.
    for p in pins:
        for j in _pin_block(h_lines, p):
            if j in pins:
                break
            if _HEX_TOKEN.search(b_lines[j]) and b_lines[j] == h_lines[j]:
                raise PolicyError(f"{path} line {j + 1}: checksum next to the {pins[p]} pin was not updated")
    return out


def _diff_from_line(path: str, i: int, bl: str, hl: str, cfg: dict) -> DepChange:
    where = f"{path} line {i + 1}"
    mb, mh = _FROM_RE.match(bl), _FROM_RE.match(hl)
    if not mb or not mh:
        raise PolicyError(f"{where}: FROM line not understood")
    if (mb.group(1), mb.group(2), mb.group(4)) != (mh.group(1), mh.group(2), mh.group(4)):
        raise PolicyError(f"{where}: FROM options or stage name changed")
    try:
        old, new = parse_image_ref(mb.group(3)), parse_image_ref(mh.group(3))
    except PolicyError as e:
        raise PolicyError(f"{where}: {e}")
    if old.name != new.name:
        raise PolicyError(f"{where}: base image changed ({old.name} -> {new.name})")
    if not new.digest:
        raise PolicyError(f"{where}: new base image is not pinned by digest")
    if old.tag == new.tag:
        if old.digest == new.digest:
            raise PolicyError(f"{where}: non-functional edit")
        kind = "digest"
    else:
        if old.tag is None or new.tag is None:
            raise PolicyError(f"{where}: tag added or removed")
        try:
            kind = classify_tag_change(old.tag, new.tag)
        except MajorVersionChange as e:
            raise MajorVersionChange(f"{where}: {e}", image=new.name)
        except PolicyError as e:
            raise PolicyError(f"{where}: {e}")
    return DepChange(path, i + 1, new.name, str(old), str(new), kind, bl, hl)


def _load_json(text: str, where: str):
    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise PolicyError(f"{where}: duplicate JSON key")
        return dict(pairs)
    try:
        return json.loads(text, object_pairs_hook=no_dupes)
    except (ValueError, RecursionError) as e:
        raise PolicyError(f"{where}: not valid JSON ({str(e)[:80]})")


def _diff_package_json(path: str, base: str, head: str, cfg: dict) -> Tuple[List[DepChange], dict]:
    b, h = _load_json(base, path), _load_json(head, path)
    if not isinstance(b, dict) or not isinstance(h, dict):
        raise PolicyError(f"{path}: not a JSON object")
    if set(b) != set(h):
        raise PolicyError(f"{path}: top-level keys changed")
    out = []
    for key in b:
        if key not in _DEP_SECTIONS:
            if b[key] != h[key]:
                raise PolicyError(f"{path}: `{key}` changed; only dependency versions may")
            continue
        bd, hd = b[key], h[key]
        if not isinstance(bd, dict) or not isinstance(hd, dict) or list(bd) != list(hd):
            raise PolicyError(f"{path}: packages added, removed or reordered in `{key}`")
        for name in bd:
            if bd[name] == hd[name]:
                continue
            ov, nv = bd[name], hd[name]
            if not (isinstance(ov, str) and isinstance(nv, str) and _EXACT_SEMVER.match(ov)
                    and _EXACT_SEMVER.match(nv)):
                raise PolicyError(f"{path}: {name}: only exact versions (x.y.z) qualify")
            try:
                kind = classify_tag_change(ov, nv, name in cfg["zero_minor"])
            except MajorVersionChange as e:
                raise MajorVersionChange(f"{path}: {name}: {e}", image=name)
            out.append(DepChange(path, 0, name, ov, nv, kind))
    if not out:
        raise PolicyError(f"{path}: no dependency version changed")
    return out, h


def _check_lockfile(path: str, base: str, head: str, pkg: dict, cfg: dict) -> None:
    b, h = _load_json(base, path), _load_json(head, path)
    if not isinstance(b, dict) or not isinstance(h, dict):
        raise PolicyError(f"{path}: not a JSON object")
    if set(b) != set(h) or any(b[k] != h[k] for k in b if k not in ("packages", "dependencies")):
        raise PolicyError(f"{path}: lockfile metadata changed")
    if not isinstance(h.get("lockfileVersion"), int) or h["lockfileVersion"] < 2:
        raise PolicyError(f"{path}: lockfileVersion 2 or later required")
    pkgs = h.get("packages")
    if not isinstance(pkgs, dict) or not isinstance(pkgs.get(""), dict):
        raise PolicyError(f"{path}: no packages map")
    root = pkgs[""]
    for sec in _DEP_SECTIONS:
        if (root.get(sec) or {}) != (pkg.get(sec) or {}):
            raise PolicyError(f"{path}: `{sec}` does not match package.json (lockfile not updated?)")
    registry = cfg["npm"]["registry"]
    for key, entry in pkgs.items():
        if key == "":
            continue
        if not isinstance(entry, dict) or not key.startswith("node_modules/"):
            raise PolicyError(f"{path}: unexpected package entry {key[:60]!r}")
        if entry.get("link") or "resolved" not in entry:
            if entry.get("link") or not entry.get("inBundle"):
                raise PolicyError(f"{path}: {key}: not resolved from the registry")
            continue
        if not isinstance(entry.get("resolved"), str) or not entry["resolved"].startswith(registry):
            raise PolicyError(f"{path}: {key}: resolved outside {registry}")
        if not isinstance(entry.get("integrity"), str) or not entry["integrity"].startswith("sha512-"):
            raise PolicyError(f"{path}: {key}: no sha512 integrity hash")
    for sec in _DEP_SECTIONS:
        for name, ver in (pkg.get(sec) or {}).items():
            entry = pkgs.get("node_modules/" + name)
            if not isinstance(entry, dict) or entry.get("version") != ver:
                raise PolicyError(f"{path}: {name} is not locked at {ver}")


@dataclass
class DepResult:
    changes: List[DepChange] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    major: bool = False
    required_jobs: List[str] = field(default_factory=list)
    names: List[str] = field(default_factory=list)


def build_check_jobs(ci_text: str) -> Dict[str, str]:
    """BUILD_CONTEXT -> `<image>:build-check` job, from a CI file. Needs PyYAML."""
    return dict(_build_check_jobs(ci_text))


@functools.lru_cache(maxsize=4)
def _build_check_jobs(ci_text: str) -> Tuple[Tuple[str, str], ...]:
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
    doc = yaml.load(ci_text, Loader=_Loader) or {}
    out = {}
    for name, job in doc.items():
        if isinstance(name, str) and name.endswith(":build-check") and isinstance(job, dict):
            ctx = (job.get("variables") or {}).get("BUILD_CONTEXT")
            if isinstance(ctx, str) and _SAFE_PATH.match(ctx):
                out[ctx] = name
    return tuple(sorted(out.items()))


def dependency_changes(paths: List[str], contents: Dict[str, "ComposePair"], allowlist: dict,
                       ci_text: Optional[str]) -> DepResult:
    """Check every dependency-class file of an MR (or of a merge). `contents`
    maps each path to its (base, head) text."""
    r = DepResult()
    cfg = dependencies_config(allowlist)
    deny = {**denylist(allowlist)["images"], **cfg["denylist"]}
    npm = cfg.get("npm") or {}
    pkg_head = None
    kinds = {p: dependency_file_kind(p, cfg) for p in paths}
    if npm and (npm["package_json"] in paths) != (npm["lockfile"] in paths):
        r.reasons.append(f"{npm['package_json']} and {npm['lockfile']} must change together")
    for path in sorted(paths, key=lambda p: kinds[p] == "lockfile"):
        base, head = contents.get(path, (None, None))
        if base is None or head is None:
            r.reasons.append(f"could not read both versions of {path}")
            continue
        try:
            if kinds[path] == "dockerfile":
                b_kinds, h_kinds = dockerfile_structure(base), dockerfile_structure(head)
                if b_kinds != h_kinds:
                    raise PolicyError(f"{path}: instruction structure changed")
                found = _diff_pin_lines(path, base, head, cfg, jinja=False, kinds=h_kinds, from_ok=True)
            elif kinds[path] == "pins":
                found = _diff_pin_lines(path, base, head, cfg, jinja=True)
            elif kinds[path] == "package_json":
                found, pkg_head = _diff_package_json(path, base, head, cfg)
            elif kinds[path] == "lockfile":
                if pkg_head is None:
                    raise PolicyError(f"{path}: changed without a valid package.json change")
                _check_lockfile(path, base, head, pkg_head, cfg)
                found = [DepChange(path, 0, "package-lock", "", "", "lock")]
            else:
                raise PolicyError(f"{path} is not covered by the dependency class")
        except MajorVersionChange as e:
            r.major = True
            r.reasons.append(f"dependency: {e}")
            continue
        except PolicyError as e:
            r.reasons.append(f"dependency: {e}")
            continue
        if not found:
            r.reasons.append(f"dependency: {path}: no version change found")
        r.changes += found
        crit = cfg["critical_files"].get(path)
        for c in found:
            if c.kind in ("checksum", "lock"):
                continue
            if c.name in deny:
                r.reasons.append(f"{path}: {c.name}: {DENIED_MARK} ({deny[c.name]})")
            if crit and c.kind not in SECURITY_KINDS:
                r.reasons.append(f"{path}: {c.kind} update of {c.name} in a security-critical image "
                                 f"(digest and patch only): {crit}")
    # Files under an image's build context need its build-check job.
    if ci_text is None:
        contexts = None
    else:
        try:
            contexts = build_check_jobs(ci_text)
        except Exception as e:  # noqa: BLE001 (any parse failure fails closed)
            contexts = None
            r.reasons.append(f"cannot read the build-check jobs: {str(e)[:80]}")
    for path in paths:
        if kinds[path] not in ("dockerfile", "package_json", "lockfile"):
            continue
        if contexts is None:
            r.reasons.append(f"{path}: no CI file to find its build-check job in")
            continue
        job = next((j for ctx, j in contexts.items() if path.startswith(ctx + "/")), None)
        if job is None:
            r.reasons.append(f"{path}: no <image>:build-check job builds it")
        elif job not in r.required_jobs:
            r.required_jobs.append(job)
    r.names = sorted({c.name for c in r.changes if c.kind not in ("checksum", "lock")})
    return r


# ---------------------------------------------------------------------------
# Whole-MR evaluation (MR-local checks; global gates are added by the caller)
# ---------------------------------------------------------------------------

LABEL_MAJOR = "major"
DENIED_MARK = "on the auto-merge denylist"


@dataclass
class Verdict:
    eligible: bool = True
    reasons: List[str] = field(default_factory=list)   # why NOT eligible
    notes: List[str] = field(default_factory=list)     # informational
    changes: List[ImageChange] = field(default_factory=list)
    fast_track: bool = False                           # eligible AND in the fast-track class
    heavy: bool = False                                # major version of a stateful service
    major: bool = False                                # a major (or ambiguous) version change
    denied: bool = False                               # touches a denylisted service/image
    frozen: bool = False                               # carries the freeze label
    deploy_jobs: List[str] = field(default_factory=list)   # per-service deploy job prefixes
    deploy_hosts: List[Deployment] = field(default_factory=list)
    ci_image: bool = False                             # CI image class (job images in a CI file)
    ci_images: List[str] = field(default_factory=list)     # image names it changes
    bot_config: bool = False                           # bot-config class (moltbot config MRs)
    bot_changes: list = field(default_factory=list)    # botconfig.ConfigChange
    shadow_playbook: bool = False                      # bot-config MR that adds new shadow playbooks
    config_services: List[Tuple[str, str]] = field(default_factory=list)   # (compose file, service)
    grafana_files: List[str] = field(default_factory=list)
    hold_keys: List[Tuple[str, str]] = field(default_factory=list)          # (deploy job, service)
    dependency: bool = False                           # dependency class (Dockerfiles, pins, npm)
    dep_changes: List[DepChange] = field(default_factory=list)
    dep_names: List[str] = field(default_factory=list)     # images/packages it changes
    required_jobs: List[str] = field(default_factory=list)  # MR pipeline jobs that must have passed

    def reject(self, reason: str) -> None:
        self.eligible = False
        self.fast_track = False
        self.reasons.append(reason)

    @property
    def manual(self) -> bool:
        """Not eligible for a lasting policy reason (not only a freeze): the MR
        waits for a human, and the sweep labels it so it stays visible."""
        return not self.eligible and len(self.reasons) > (1 if self.frozen else 0)

    def as_dict(self) -> dict:
        return {"eligible": self.eligible, "fast_track": self.fast_track, "ci_image": self.ci_image,
                "dependency": self.dependency, "dep_changes": [c.as_dict() for c in self.dep_changes],
                "required_jobs": self.required_jobs,
                "bot_config": self.bot_config, "shadow_playbook": self.shadow_playbook,
                "bot_changes": [c.as_dict() for c in self.bot_changes],
                "heavy": self.heavy,
                "major": self.major, "denied": self.denied, "manual": self.manual,
                "deploy_jobs": self.deploy_jobs, "reasons": self.reasons,
                "notes": self.notes, "changes": [c.as_dict() for c in self.changes]}


@dataclass
class FileChange:
    path: str
    old_path: Optional[str] = None
    new_file: bool = False
    deleted_file: bool = False
    renamed_file: bool = False
    mode_changed: bool = False


ComposePair = Tuple[Optional[str], Optional[str]]


def evaluate_mr(*, author: str, target_branch: str, labels: List[str],
                files: List[FileChange], allowlist: dict, codeowners_text: str,
                base_compose: Optional[str] = None, head_compose: Optional[str] = None,
                composes: Optional[Dict[str, ComposePair]] = None,
                deployments: Optional[Dict[str, List[Deployment]]] = None,
                merger: Optional[str] = None, ci_text: Optional[str] = None) -> Verdict:
    """Image-update policy (general, fast track, CI image, dependency), and for MRs by a
    bot-config author that are not image updates, the bot-config class.
    `composes` maps changed files to their (base, head) text (every changed
    file for a bot-config author; every dependency-class file). `ci_text` is
    main's .gitlab-ci.yml, for the build-check jobs of the dependency class."""
    v = _evaluate_images(author=author, target_branch=target_branch, labels=labels, files=files,
                         allowlist=allowlist, codeowners_text=codeowners_text, base_compose=base_compose,
                         head_compose=head_compose, composes=composes, deployments=deployments,
                         merger=merger, ci_text=ci_text)
    if v.eligible or v.changes or v.major or v.dep_changes:
        return v
    import botconfig
    try:
        cfg = botconfig.config(allowlist)
    except PolicyError as e:
        v.reject(f"policy: {e}")
        return v
    if not cfg["enabled"] or author not in cfg["authors"]:
        return v
    return evaluate_bot_config(author=author, target_branch=target_branch, labels=labels, files=files,
                               contents=composes or {}, allowlist=allowlist, merger=merger,
                               deployments=deployments if deployments is not None
                               else default_deployments(allowlist))


LABEL_SECURITY = "security"


def bot_config_targets(result, deployments: Dict[str, List[Deployment]]) -> Tuple[List[Deployment], List[str]]:
    """(deploy targets, problems) of a bot-config change: the hosts of each
    changed compose file, and the router for Grafana provisioning files."""
    targets: List[Deployment] = []
    problems: List[str] = []
    for path in sorted({f for f, _ in result.compose_services}):
        hosts = deployments.get(path) or []
        if not hosts:
            problems.append(f"{path} is not deployed by any host")
        for d in hosts:
            if not d.job:
                problems.append(f"{path} runs on {d.host}, which has no per-service deploy")
            elif d not in targets:
                targets.append(d)
    if result.grafana_files:
        router = [d for d in deployments.get(COMPOSE_PATH) or [] if d.job == "router"]
        if not router:
            problems.append("Grafana files deploy on the router, which has no per-service deploy")
        elif router[0] not in targets:
            targets.append(router[0])
    return targets, problems


GRAFANA_HOLD = "grafana-provisioning"


def evaluate_bot_config(*, author: str, target_branch: str, labels: List[str], files: List[FileChange],
                        contents: Dict[str, ComposePair], allowlist: dict, merger: Optional[str],
                        deployments: Dict[str, List[Deployment]]) -> Verdict:
    """The bot-config class (ci/automerge/botconfig.py; docs/AUTOMERGE.md)."""
    import botconfig
    v = Verdict(bot_config=True)
    cfg = botconfig.config(allowlist)
    if author not in cfg["authors"]:
        v.reject(f"author {author!r} is not a bot-config author")
    if merger is not None and author == merger:
        v.reject(f"author {author!r} is the merging identity; the merger must not be the author")
    if target_branch != allowlist.get("target_branch", "main"):
        v.reject(f"target branch {target_branch!r} is not {allowlist.get('target_branch', 'main')!r}")
    freeze_label = allowlist.get("freeze_label", "freeze")
    if freeze_label in labels:
        v.frozen = True
        v.reject(f"MR has the {freeze_label!r} label")
    for label in (LABEL_SECURITY, LABEL_MAJOR):
        if label in labels:
            v.reject(f"MR has the {label!r} label")
    if len(files) > 30:
        v.reject(f"MR changes {len(files)} files (more than 30)")
    r = botconfig.evaluate_files(files, contents, allowlist)
    for reason in r.reasons:
        v.reject(reason)
    v.bot_changes = r.changes
    v.shadow_playbook = bool(r.shadow_playbooks)
    v.config_services = r.compose_services
    v.grafana_files = r.grafana_files
    targets, problems = bot_config_targets(r, deployments)
    for p in problems:
        v.reject(p)
    for d in targets:
        v.deploy_jobs.append(d.job)
        v.deploy_hosts.append(d)
    by_file = {}
    for f, svc in r.compose_services:
        by_file.setdefault(f, []).append(svc)
    for d in targets:
        for f, svcs in by_file.items():
            if d in (deployments.get(f) or []):
                v.hold_keys += [(d.job, s) for s in svcs]
    if r.grafana_files:
        v.hold_keys.append(("router", GRAFANA_HOLD))
    for c in r.changes:
        v.notes.append(f"{c.file}: {c.service + ': ' if c.service else ''}{c.what}")
    if v.deploy_jobs:
        v.notes.append("deploys via " + ", ".join(f"{j}:deploy-service" for j in v.deploy_jobs))
    if v.eligible:
        v.notes.append("class: bot-config" + (" (shadow playbooks)" if v.shadow_playbook else ""))
    return v


def _evaluate_images(*, author: str, target_branch: str, labels: List[str],
                files: List[FileChange], allowlist: dict, codeowners_text: str,
                base_compose: Optional[str] = None, head_compose: Optional[str] = None,
                composes: Optional[Dict[str, ComposePair]] = None,
                deployments: Optional[Dict[str, List[Deployment]]] = None,
                merger: Optional[str] = None, ci_text: Optional[str] = None) -> Verdict:
    """`composes` maps each changed compose file to its (base, head) text;
    `base_compose`/`head_compose` are shorthand for the router file.
    `deployments` comes from `compose_deployments` (default: every listed
    compose file on the router). `merger` is the identity that would merge
    (the sweep's token user); an MR authored by it is rejected."""
    v = Verdict()
    composes = dict(composes or {})
    if base_compose is not None or head_compose is not None:
        composes.setdefault(COMPOSE_PATH, (base_compose, head_compose))
    if deployments is None:
        deployments = default_deployments(allowlist)

    # A malformed policy section fails closed for every MR.
    for check in (fast_track_config, denylist, pre_upgrade_config, ci_images_config, dependencies_config):
        try:
            check(allowlist)
        except PolicyError as e:
            v.reject(f"policy: {e}")

    authors = allowlist.get("authors") or []
    if author not in authors:
        v.reject(f"author {author!r} is not in the auto-merge author list")
    if merger is not None and author == merger:
        v.reject(f"author {author!r} is the merging identity; the merger must not be the author")
    if target_branch != allowlist.get("target_branch", "main"):
        v.reject(f"target branch {target_branch!r} is not {allowlist.get('target_branch', 'main')!r}")
    freeze_label = allowlist.get("freeze_label", "freeze")
    if freeze_label in labels:
        v.frozen = True
        v.reject(f"MR has the {freeze_label!r} label")
    if LABEL_MAJOR in labels:
        v.major = True
        v.reject(f"labelled {LABEL_MAJOR!r} by Renovate: major updates are never auto-merged")

    allowed = set(compose_files(allowlist))
    ci_allowed = set(ci_files(allowlist))
    paths = sorted({f.path for f in files} | {f.old_path for f in files if f.old_path})
    dep_allowed = set(dependency_paths(paths, allowlist))
    if not files:
        v.reject("MR has no file changes")
    for f in files:
        if f.new_file or f.deleted_file or f.renamed_file or f.mode_changed:
            v.reject(f"{f.path}: file added, deleted, renamed or mode-changed")
    for p in paths:
        if p in allowed or p in ci_allowed or p in dep_allowed:
            continue
        if COMPOSE_GLOB.match(p):
            if not deployments.get(p):
                v.reject(f"{p} is not deployed by any host (docker_compose_file_names in "
                         "ansible/inventory.yml), so nothing would deploy or health-check it")
            else:
                v.reject(f"{p} is not listed under compose_files in the auto-merge policy")
        else:
            v.reject(f"touches files outside the auto-merge scope: {p}")

    # CODEOWNERS: the listed compose files and CI files are special-cased
    # (allowed only for image-only diffs, verified below); any other
    # code-owned path disqualifies.
    owned = [p for p in protected_paths(paths, codeowners_text) if p not in allowed | ci_allowed | dep_allowed]
    if owned:
        v.reject("touches CODEOWNERS-protected paths: " + ", ".join(owned))

    if dep_allowed:
        _evaluate_dependencies(v, author, sorted(dep_allowed), [p for p in paths if p not in dep_allowed],
                               composes, allowlist, codeowners_text, ci_text)
        return v
    changed_ci = [p for p in paths if p in ci_allowed]
    if changed_ci:
        _evaluate_ci(v, author, changed_ci, [p for p in paths if p not in ci_allowed], composes, allowlist)
        return v

    changed_composes = [p for p in paths if p in allowed]
    for path in changed_composes:
        base, head = composes.get(path, (None, None))
        if base is None or head is None:
            v.reject(f"could not read both versions of {path}")
            continue
        try:
            file_changes = diff_compose(base, head, path, zero_minor_services(allowlist))
        except MajorVersionChange as e:
            v.major = True
            v.reject(f"compose: {e}")
            if is_stateful(e.service, e.image, allowlist):
                v.heavy = True
                v.reject(HEAVY_MANUAL_REASON)
            continue
        except PolicyError as e:
            v.reject(f"compose: {e}")
            continue
        v.changes += file_changes
        for r in change_reasons(file_changes, allowlist, {path: compose_service_info(head)},
                                {path: compose_service_info(base)}):
            v.reject(r)
            if DENIED_MARK in r:
                v.denied = True
        hosts = deployments.get(path) or []
        if not hosts:
            v.reject(f"{path} is not deployed by any host")
        for d in hosts:
            if not d.job:
                v.reject(f"{path} runs on {d.host}, which has no per-service deploy (deploy_hosts)")
            elif d.job not in v.deploy_jobs:
                v.deploy_jobs.append(d.job)
                v.deploy_hosts.append(d)
    for c in v.changes:
        v.notes.append(f"{c.service}: {c.kind} update {c.old.tag or ''} -> {c.new.tag or ''}"
                       f" ({(c.new.digest or '')[:19]})"
                       + (f" [{c.file}]" if c.file != COMPOSE_PATH else ""))
    try:
        for svc, b in pre_upgrade_backups(v.changes, allowlist, "now").items():
            v.notes.append(f"{svc}: {b['type']} pre-upgrade backup before the deploy, restored on rollback")
    except PolicyError:
        pass    # already rejected above
    if v.deploy_jobs:
        v.notes.append("deploys via " + ", ".join(f"{j}:deploy-service" for j in v.deploy_jobs))
    if not changed_composes and v.eligible:
        v.reject("MR does not change a deployed compose file")
    elif changed_composes and not v.changes and v.eligible:
        v.reject("compose file changed but no image updates were found")

    if v.eligible:
        try:
            v.fast_track, why = fast_track_verdict(author, v.changes, allowlist)
        except PolicyError as e:
            v.fast_track, why = False, f"fast track: {e}"
        v.notes.append(why if v.fast_track else f"not fast-track: {why}")
    return v


def _evaluate_ci(v: Verdict, author: str, changed_ci: List[str], others: List[str],
                 files: Dict[str, ComposePair], allowlist: dict) -> None:
    """The CI image class: job image lines in CI files only, by a CI-class
    author. Nothing is deployed (deploy_jobs stays empty)."""
    cfg = ci_images_config(allowlist)
    v.ci_image = True
    if others:
        v.reject("a CI image update must not change other files: " + ", ".join(others))
    if author not in cfg["authors"]:
        v.reject(f"author {author!r} may not auto-merge CI image updates (ci_images.authors)")
    for path in changed_ci:
        base, head = files.get(path, (None, None))
        if base is None or head is None:
            v.reject(f"could not read both versions of {path}")
            continue
        try:
            changes = diff_ci_images(base, head, path)
        except MajorVersionChange as e:
            v.major = True
            v.reject(f"ci: {e}")
            continue
        except PolicyError as e:
            v.reject(f"ci: {e}")
            continue
        v.changes += changes
        for r in ci_change_reasons(changes, allowlist):
            v.reject(r)
            v.denied = True
        for r in ci_critical_reasons(changes, allowlist):
            v.reject(r)
    v.ci_images = sorted({c.new.name for c in v.changes})
    for c in v.changes:
        v.notes.append(f"{c.service}: {c.kind} update {c.old.tag or ''} -> {c.new.tag or ''}"
                       f" ({(c.new.digest or '')[:19]}) [{c.file}]")
    if v.eligible and not v.changes:
        v.reject("CI file changed but no image updates were found")
    if v.eligible:
        v.notes.append("class: CI image (nothing to deploy; the sweep watches the main pipeline)")


def ci_critical_reasons(changes: List[ImageChange], allowlist: dict) -> List[str]:
    """Security-critical CI jobs (`dependencies.security_critical.ci_jobs`)
    take digest refreshes and patch releases only."""
    try:
        cfg = dependencies_config(allowlist)
    except PolicyError as e:
        return [f"policy: {e}"]
    out = []
    for c in changes:
        why = critical_job_reason(c.service, cfg)
        if why and c.kind not in SECURITY_KINDS:
            out.append(f"{c.service}: {c.kind} update of {c.new.name} in a security-critical job "
                       f"(digest and patch only): {why}")
    return out


def codeowner_of(path: str, codeowners_text: str) -> List[str]:
    """Owners of the last matching CODEOWNERS rule (GitLab semantics)."""
    found: List[str] = []
    for pat, own in parse_codeowners(codeowners_text):
        if codeowners_match(pat, path):
            found = own
    return found


def _evaluate_dependencies(v: Verdict, author: str, dep_paths: List[str], others: List[str],
                           files: Dict[str, ComposePair], allowlist: dict, codeowners_text: str,
                           ci_text: Optional[str]) -> None:
    """The dependency class. Nothing special is deployed: the merge deploys
    like a reviewed one, and the sweep watches its main pipeline."""
    cfg = dependencies_config(allowlist)
    v.dependency = True
    if others:
        v.reject("a dependency update must not change other files: " + ", ".join(others))
    if author not in cfg["authors"]:
        v.reject(f"author {author!r} may not auto-merge dependency updates (dependencies.authors)")
    for p in dep_paths:
        if not cfg["codeowner"] or cfg["codeowner"] not in codeowner_of(p, codeowners_text):
            v.reject(f"CODEOWNERS does not list the auto-merge bot for {p}, so its approval would not count")
    r = dependency_changes(dep_paths, files, allowlist, ci_text)
    for reason in r.reasons:
        v.reject(reason)
        if DENIED_MARK in reason:
            v.denied = True
    v.major = v.major or r.major
    v.dep_changes = r.changes
    v.dep_names = r.names
    v.required_jobs = r.required_jobs
    v.hold_keys = [(DEP_HOLD_JOB, n) for n in r.names]
    for c in r.changes:
        if c.kind == "lock":
            v.notes.append(f"{c.file}: lockfile consistent with package.json, registry-only, sha512")
        else:
            v.notes.append(f"{c.name}: {c.kind} update {c.old} -> {c.new} [{c.file}"
                           + (f" line {c.line}]" if c.line else "]"))
    if v.required_jobs:
        v.notes.append("needs passed MR pipeline jobs: " + ", ".join(v.required_jobs))
    if v.eligible and not r.changes:
        v.reject("no dependency updates were found")
    if v.eligible:
        v.notes.append("class: dependency (normal deploy; the sweep watches the main pipeline)")


def change_reasons(changes: List[ImageChange], allowlist: dict,
                   head_info: Optional[Dict[str, Dict[str, ServiceInfo]]] = None,
                   base_info: Optional[Dict[str, Dict[str, ServiceInfo]]] = None) -> List[str]:
    """Per-change checks: denylist, compose profiles, update kind. Used by
    evaluate_mr and again by plan-deploy on the merged commit. `*_info` map a
    compose path to `compose_service_info` of that version of the file."""
    reasons = []
    for c in changes:
        try:
            why = deny_reason(c.service, c.new.name, allowlist)
        except PolicyError as e:
            reasons.append(f"policy: {e}")
            continue
        if why:
            reasons.append(f"{c.service}: {DENIED_MARK} ({why})")
            continue
        profiles: List[str] = []
        for info in (head_info or {}, base_info or {}):
            si = info.get(c.file, {}).get(c.service)
            if si and si.profiles:
                profiles = si.profiles
        if profiles:
            reasons.append(f"{c.service}: runs only under compose profile(s) {', '.join(profiles)}; "
                           "a per-service deploy would start it")
            continue
        try:
            kinds = allowed_kinds(c.service, c.new.name, allowlist)
        except PolicyError as e:
            reasons.append(f"policy: {e}")
            continue
        if c.kind not in kinds:
            reasons.append(f"{c.service}: {c.kind} update of {c.new.name} is not allowed "
                           f"(allowed: {', '.join(kinds) or 'none'})")
    return reasons


# ---------------------------------------------------------------------------
# Jobs that run a CI image (for the post-merge watch)
# ---------------------------------------------------------------------------

_CI_RESERVED = {"stages", "variables", "default", "include", "workflow", "image", "services",
                "before_script", "after_script", "cache"}


def ci_image_jobs(text: str, names: Collection[str]) -> List[str]:
    """Jobs of a CI file whose effective image (own `image`, else through
    `extends`, else `default.image`) is one of `names`. Needs PyYAML."""
    import yaml

    class _Loader(yaml.SafeLoader):
        pass

    # GitLab's !reference [a, b]: only its shape matters here.
    _Loader.add_constructor("!reference", lambda loader, node: loader.construct_sequence(node))
    doc = yaml.load(text, Loader=_Loader) or {}

    def own_image(key: str, seen: frozenset):
        job = doc.get(key)
        if not isinstance(job, dict) or key in seen:
            return None
        if "image" in job:
            return job["image"]
        ext = job.get("extends") or []
        for parent in reversed([ext] if isinstance(ext, str) else list(ext)):
            img = own_image(parent, seen | {key})
            if img is not None:
                return img
        return None

    default = (doc.get("default") or {}).get("image") if isinstance(doc.get("default"), dict) else None
    out = []
    for key, job in doc.items():
        if not isinstance(key, str) or key.startswith(".") or key in _CI_RESERVED or not isinstance(job, dict):
            continue
        img = own_image(key, frozenset())
        if img is None:
            img = default
        if isinstance(img, dict):
            img = img.get("name")
        if not isinstance(img, str):
            continue
        try:
            name = parse_image_ref(img).name
        except PolicyError:
            continue
        if name in names:
            out.append(key)
    return out
