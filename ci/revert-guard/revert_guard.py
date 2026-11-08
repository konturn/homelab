#!/usr/bin/env python3
"""Stale-tree revert guard.

Catches an MR that silently undoes work another MR merged recently, the
failure mode of committing from a stale working tree (e.g. b0ac2497 in !1048,
which reverted parts of !1043, !1046, !1013, !989, !1044 and !1040).

For the MR diff (merge base..head):

  * every removed line is blamed at the merge base. If it was introduced by a
    commit that reached main through a *different* MR merged in the last
    --days days, and the hunk either only deletes lines or puts back lines
    that MR had removed, it is flagged: the MR is reverting that MR.
  * every added line that a different recent MR removed from the same file
    (and that is not at the merge base) is flagged: the MR is restoring what
    that MR deleted.

Ordinary follow-up edits (a hunk that replaces a recently added line with
something new) are not flagged.

It also reports, as a warning, files where the two-dot diff against the
target (what `git diff origin/main` shows) differs but the MR's own commits
never touched them: harmless for the merge itself (git keeps main's side),
but a sign the branch is behind and a stale checkout would revert them.

Exit status: 1 when reverts are found, unless the MR carries the override
label `revert-intended`; 0 otherwise. See ci/revert-guard/README.md.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

OVERRIDE_LABEL = "revert-intended"
MR_RE = re.compile(r"See merge request [\w./-]*!(\d+)")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# Lines too generic to say anything about who wrote them.
MIN_SIGNIFICANT = 4


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        errors="replace",
    ).stdout


def significant(line: str) -> bool:
    s = line.strip()
    if len(s) < MIN_SIGNIFICANT:
        return False
    return bool(re.search(r"[A-Za-z0-9]", s))


def words(line: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9_]{3,}", line.lower()))


def moved(h: "Hunk", added: set[str], threshold: float = 0.6) -> bool:
    removed = set().union(*(words(r) for r in h.removed)) if h.removed else set()
    if not removed:
        return False
    return len(removed & added) / len(removed) >= threshold


@dataclass
class Hunk:
    old_path: str | None
    new_path: str | None
    old_start: int
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def parse_diff(text: str) -> list[Hunk]:
    """Parse `git diff -U0` output into hunks (binary files are skipped)."""
    hunks: list[Hunk] = []
    old_path = new_path = None
    cur: Hunk | None = None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            old_path = new_path = None
            cur = None
        elif line.startswith("--- ") and cur is None:
            p = line[4:]
            old_path = None if p == "/dev/null" else _strip_prefix(p, "a/")
        elif line.startswith("+++ ") and cur is None:
            p = line[4:]
            new_path = None if p == "/dev/null" else _strip_prefix(p, "b/")
        elif line.startswith("@@"):
            m = HUNK_RE.match(line)
            if not m:
                continue
            cur = Hunk(old_path, new_path, int(m.group(1)))
            hunks.append(cur)
        elif cur is not None and line.startswith("-"):
            cur.removed.append(line[1:])
        elif cur is not None and line.startswith("+"):
            cur.added.append(line[1:])
    return hunks


def _strip_prefix(p: str, prefix: str) -> str:
    if p.startswith('"'):  # quoted path (unusual characters)
        p = p[1:-1].encode().decode("unicode_escape").encode("latin-1").decode()
    return p[len(prefix):] if p.startswith(prefix) else p


def diff_hunks(a: str, b: str, cwd: str | None = None) -> list[Hunk]:
    return parse_diff(git("diff", "-U0", "--no-color", "--no-ext-diff",
                          "-M", f"{a}", f"{b}", cwd=cwd))


@dataclass
class MergedMR:
    iid: str           # "1043", or "direct:<sha>" for a non-MR commit on main
    merge_sha: str
    when: int
    title: str
    commits: set[str]
    files: set[str] = field(default_factory=set)
    _removed: dict[str, set[str]] | None = None
    _added: dict[str, set[str]] | None = None

    def removed_by_file(self, cwd: str | None) -> dict[str, set[str]]:
        """Significant lines this MR removed, per path (old and new name)."""
        return self._changes(cwd)[0]

    def added_by_file(self, cwd: str | None) -> dict[str, set[str]]:
        """Significant lines this MR added, per path (old and new name)."""
        return self._changes(cwd)[1]

    def _changes(self, cwd: str | None) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        if self._removed is None:
            rm: dict[str, set[str]] = defaultdict(set)
            ad: dict[str, set[str]] = defaultdict(set)
            parents = git("rev-list", "--parents", "-n1", self.merge_sha,
                          cwd=cwd).split()[1:]
            if len(parents) >= 2:
                base = git("merge-base", parents[0], parents[1], cwd=cwd).strip()
                hunks = diff_hunks(base, parents[1], cwd)
            else:
                hunks = diff_hunks(f"{self.merge_sha}^", self.merge_sha, cwd)
            for h in hunks:
                paths = {p for p in (h.old_path, h.new_path) if p}
                for target, lines in ((rm, h.removed), (ad, h.added)):
                    for ln in lines:
                        if significant(ln):
                            for p in paths:
                                target[p].add(ln.strip())
            self._removed, self._added = dict(rm), dict(ad)
        return self._removed, self._added


def recent_mrs(target: str, since: int, cwd: str | None = None) -> list[MergedMR]:
    """MRs (and direct pushes) that landed on the target's first-parent
    history at or after `since` (epoch seconds), newest first."""
    out = git("log", "--first-parent", "--format=%H%x00%P%x00%ct%x00%B%x1e",
              target, cwd=cwd)
    mrs: list[MergedMR] = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        sha, parents, ct, body = rec.split("\x00", 3)
        if int(ct) < since:
            break
        ps = parents.split()
        title = body.strip().splitlines()[0] if body.strip() else ""
        if len(ps) >= 2:
            m = MR_RE.search(body)
            iid = m.group(1) if m else f"merge:{sha[:8]}"
            commits = set(git("rev-list", f"{ps[0]}..{ps[1]}", cwd=cwd).split())
            commits.add(sha)
            # Prefer the MR title (second line of a GitLab merge message).
            lines = [ln for ln in body.strip().splitlines() if ln.strip()]
            if len(lines) > 1 and lines[0].startswith("Merge branch"):
                title = lines[1]
        else:
            m = MR_RE.search(body)
            iid = m.group(1) if m else f"direct:{sha[:8]}"
            commits = {sha}
        mrs.append(MergedMR(iid, sha, int(ct), title, commits))
    if mrs:
        # Files each landed MR changed on main (vs. the first parent), in one call.
        files: dict[str, set[str]] = defaultdict(set)
        cur = None
        log = git("log", "--first-parent", "-m", "--name-only", "--format=%x1e%H",
                  f"{mrs[-1].merge_sha}^..{target}", cwd=cwd)
        for line in log.split("\n"):
            if line.startswith("\x1e"):
                cur = line[1:]
            elif line and cur:
                files[cur].add(line)
        for m in mrs:
            m.files = files.get(m.merge_sha, set())
    return mrs


def blame(rev: str, path: str, lines: list[int], cwd: str | None = None) -> dict[int, str]:
    """Map line number (at rev) -> commit sha that introduced it."""
    if not lines:
        return {}
    args = ["blame", "--porcelain"]
    for ln in sorted(set(lines)):
        args += ["-L", f"{ln},{ln}"]
    args += [rev, "--", path]
    try:
        out = git(*args, cwd=cwd)
    except subprocess.CalledProcessError:
        return {}
    res: dict[int, str] = {}
    header = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)")
    for line in out.split("\n"):
        m = header.match(line)
        if m:
            res[int(m.group(2))] = m.group(1)
    return res


@dataclass
class Finding:
    path: str
    mr: MergedMR
    kind: str                     # "reverts" or "restores"
    lines: list[tuple[int, str]]  # (line number, text)
    blocking: bool = True
    coverage: float = 1.0


# Share of an MR's surviving change to a file that must be undone to fail.
FULL_UNDO = 0.8


def coverage(f: "Finding", undone: set[str], at_base: set[str], cwd: str | None) -> float:
    """Undone share of what f.mr changed in f.path that is still visible at
    the merge base (its added lines still there, its removed lines still gone)."""
    added = {t for t in f.mr.added_by_file(cwd).get(f.path, set()) if t in at_base}
    removed = {t for t in f.mr.removed_by_file(cwd).get(f.path, set()) if t not in at_base}
    total = added | removed
    if not total:
        return 1.0
    return len(total & undone) / len(total)


def analyse(target: str, head: str, days: float, own_iid: str | None = None,
            now: int | None = None, cwd: str | None = None) -> tuple[list[Finding], list[str]]:
    """Return (findings, untouched two-dot files).

    A finding is `blocking` when its file holds nothing but such reverts and
    restores (every hunk with a significant line is explained by them): the
    MR has no business in that file, the stale-tree signature. Reverts mixed
    with the MR's own edits in a file are reported but do not fail the job;
    those are almost always deliberate follow-ups (rewriting or dropping what
    a recent MR added).
    """
    base = git("merge-base", target, head, cwd=cwd).strip()
    own = set(git("rev-list", f"{base}..{head}", cwd=cwd).split())
    now = now if now is not None else int(time.time())
    mrs = [m for m in recent_mrs(target, int(now - days * 86400), cwd)
           if m.iid != own_iid and not (m.commits & own)]
    owner: dict[str, MergedMR] = {}
    for m in reversed(mrs):  # newest wins if a commit appears twice
        for c in m.commits:
            owner[c] = m

    hunks = diff_hunks(base, head, cwd)
    added_words: dict[str, set[str]] = defaultdict(set)
    for h in hunks:
        if h.new_path:
            for a in h.added:
                added_words[h.new_path] |= words(a)
    findings: dict[tuple[str, str, str], Finding] = {}
    # Per hunk: indexes of significant removed/added lines explained by a revert/restore.
    explained_rm: dict[int, set[int]] = defaultdict(set)
    explained_add: dict[int, set[int]] = defaultdict(set)

    def add(h, path, mr, kind, ln, text):
        key = (path, mr.iid, kind)
        findings.setdefault(key, Finding(path, mr, kind, [])).lines.append((ln, text))

    # Removed lines: blame at the merge base.
    by_path: dict[str, list[tuple[Hunk, int, int, str]]] = defaultdict(list)
    for h in hunks:
        if h.old_path is None:
            continue
        for i, text in enumerate(h.removed):
            by_path[h.old_path].append((h, i, h.old_start + i, text))
    for path, items in by_path.items():
        blamed = blame(base, path, [ln for _, _, ln, _ in items], cwd)
        for h, i, ln, text in items:
            mr = owner.get(blamed.get(ln, ""))
            if mr is None or not significant(text):
                continue
            if not h.added:
                # A block deleted here but re-added (moved, reflowed) elsewhere
                # in the same file is an edit, not a revert.
                if moved(h, added_words.get(h.new_path or path, set())):
                    continue
            else:
                # A replacement only counts if it puts back what that MR removed.
                gone = mr.removed_by_file(cwd).get(path, set())
                sig = [a.strip() for a in h.added if significant(a)]
                if not sig or not all(a in gone for a in sig):
                    continue
            explained_rm[id(h)].add(i)
            add(h, path, mr, "reverts", ln, text)

    # Added lines: restoring something a recent foreign MR deleted.
    base_cache: dict[str, set[str]] = {}

    def base_text(path: str) -> set[str]:
        if path not in base_cache:
            try:
                content = git("show", f"{base}:{path}", cwd=cwd)
            except subprocess.CalledProcessError:
                content = ""
            base_cache[path] = {ln.strip() for ln in content.split("\n")}
        return base_cache[path]

    for h in hunks:
        if h.new_path is None or not h.added:
            continue
        lookup = h.old_path or h.new_path
        at_base = base_text(lookup)
        for i, text in enumerate(h.added):
            s = text.strip()
            if not significant(text) or s in at_base:
                continue
            for mr in mrs:
                if lookup not in mr.files and h.new_path not in mr.files:
                    continue
                if s in mr.removed_by_file(cwd).get(lookup, set()):
                    explained_add[id(h)].add(i)
                    add(h, h.new_path, mr, "restores", 0, text)
                    break

    # Which files are nothing but reverts/restores?
    mixed: set[str] = set()
    for h in hunks:
        sig_rm = {i for i, t in enumerate(h.removed) if significant(t)}
        sig_add = {i for i, t in enumerate(h.added) if significant(t)}
        if not sig_rm and not sig_add:
            continue
        if sig_rm - explained_rm[id(h)] or sig_add - explained_add[id(h)]:
            mixed.add(h.new_path or h.old_path)
            if h.old_path:
                mixed.add(h.old_path)
    # How much of each MR's change to a file is undone. A stale tree undoes
    # all of it; a deliberate follow-up usually drops or rewrites a part.
    undone: dict[tuple[str, str], set[str]] = defaultdict(set)
    for f in findings.values():
        undone[(f.path, f.mr.iid)] |= {t.strip() for _, t in f.lines}
    result = list(findings.values())
    for f in result:
        f.coverage = coverage(f, undone[(f.path, f.mr.iid)], base_text(f.path), cwd)
        f.blocking = f.path not in mixed and f.coverage >= FULL_UNDO
    result.sort(key=lambda f: (not f.blocking, f.path, f.mr.iid, f.kind))
    return result, untouched_two_dot(target, head, base, cwd)


def untouched_two_dot(target: str, head: str, base: str, cwd: str | None) -> list[str]:
    """Files the two-dot diff target..head shows that no own commit touched."""
    two_dot = set(git("diff", "--name-only", target, head, cwd=cwd).split("\n")) - {""}
    touched = set(git("log", "-m", "--name-only", "--format=", f"{base}..{head}",
                      cwd=cwd).split("\n")) - {""}
    return sorted(two_dot - touched)


def mr_ref(mr: MergedMR) -> str:
    return f"!{mr.iid}" if mr.iid.isdigit() else mr.iid


def _describe(f: Finding) -> list[str]:
    verb = "removes lines added by" if f.kind == "reverts" else "re-adds lines removed by"
    out = [f"  {f.path}: {verb} {mr_ref(f.mr)} ({f.mr.title}, merged "
           f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(f.mr.when))} UTC)"]
    nums = [n for n, _ in f.lines if n]
    if nums:
        out.append(f"    lines (at merge base): {_ranges(nums)}")
    for _, text in f.lines[:5]:
        out.append(f"    {'-' if f.kind == 'reverts' else '+'} {text.strip()[:120]}")
    if len(f.lines) > 5:
        out.append(f"    ... {len(f.lines) - 5} more")
    return out


def report(findings: list[Finding], untouched: list[str], days: float) -> str:
    out: list[str] = []
    blocking = [f for f in findings if f.blocking]
    notes = [f for f in findings if not f.blocking]
    if blocking:
        out.append("FAIL: this MR undoes changes that other MRs merged to main in the "
                   f"last {days:g} days, in files where it changes nothing else:\n")
        for f in blocking:
            out += _describe(f)
        out.append("")
        out.append("If this is a stale working tree: rebase onto origin/main and redo the "
                   "change on current files (`git diff origin/main --stat` must list only "
                   f"your files). If the revert is deliberate, add the `{OVERRIDE_LABEL}` "
                   "label and run a new pipeline (a retried job keeps the old labels).")
    else:
        out.append("OK: no stale-tree reverts of recently merged MRs.")
    if notes:
        out.append("")
        out.append("Note (not failing): edits that undo part of a recent MR in files this "
                   "MR also changes otherwise; check they are intended:")
        for f in notes:
            out += _describe(f)
    if untouched:
        out.append("")
        out.append(f"Note (not failing): `git diff <target>` (two-dot) also differs in "
                   f"{len(untouched)} file(s) none of this MR's commits touch. The branch "
                   "is behind the target: the merge keeps the target's version, but a "
                   "commit made from this tree would revert them. Rebase before "
                   "committing more.")
        for p in untouched[:30]:
            out.append(f"  {p}")
        if len(untouched) > 30:
            out.append(f"  ... {len(untouched) - 30} more")
    return "\n".join(out)


def _ranges(nums: list[int]) -> str:
    nums = sorted(set(nums))
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(str(start) if start == prev else f"{start}-{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--target", default="origin/main", help="target branch ref")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--days", type=float, default=float(os.environ.get("REVERT_GUARD_DAYS", 7)))
    ap.add_argument("--mr-iid", default=os.environ.get("CI_MERGE_REQUEST_IID"))
    ap.add_argument("--labels", default=os.environ.get("CI_MERGE_REQUEST_LABELS", ""),
                    help="comma-separated MR labels")
    ap.add_argument("--now", type=int, help="reference time (epoch), for replays")
    ap.add_argument("-C", dest="cwd", help="repository path")
    a = ap.parse_args(argv)

    findings, untouched = analyse(a.target, a.head, a.days, a.mr_iid, a.now, a.cwd)
    print(report(findings, untouched, a.days))
    if not any(f.blocking for f in findings):
        return 0
    labels = {s.strip() for s in a.labels.split(",") if s.strip()}
    if OVERRIDE_LABEL in labels:
        print(f"\n`{OVERRIDE_LABEL}` label present: not failing.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
