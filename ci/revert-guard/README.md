# Stale-tree revert guard

`revert_guard.py` runs in every MR pipeline (`revert-guard` job) and fails an
MR that silently undoes work another MR merged recently. That is what a commit
made from a stale working tree does: b0ac2497 (in !1048) was committed from an
old checkout and reverted parts of !1046, !1040, !1041, !1013, !1044 and !989
(the Pi memory cgroup and image prune, the OpenClaw infra summary, GitLab
tracing, unit file modes, a deploy-scope entry, the nginx CI image). !1057 put
them back by hand.

## How it works

For the MR diff (merge base with the target..head, i.e. what the MR merges):

1. **Removed lines.** Each one is blamed at the merge base. If the commit that
   introduced it reached the target through a *different* MR (or a direct
   push) in the last 7 days (`--days`, `REVERT_GUARD_DAYS`), and the hunk
   either only deletes, or replaces it with exactly what that MR had removed,
   the line counts as reverted. Commits are mapped to MRs through the
   first-parent merge commits on the target (`See merge request ...!N`).
   A deleted block whose words reappear elsewhere in the same file is a move,
   not a revert.
2. **Added lines.** A line that a different recent MR removed from the same
   file, and that is not at the merge base, counts as restored.
3. **Fail only on the stale-tree signature.** A finding fails the job when, in
   that file, the MR changes nothing *but* such reverts/restores, and it undoes
   at least 80% of what that MR changed in the file (and is still visible at
   the merge base). A stale checkout undoes another MR's change to a file
   wholesale; a deliberate follow-up usually rewrites or trims a part of it
   next to its own edits. Partial or mixed cases are printed as notes.
4. **Two-dot note.** Files that `git diff <target>` (two-dot) shows but none of
   the MR's commits touch are listed as a note: the branch is behind, the merge
   keeps the target's side, but a commit made from this tree would revert them.
   This never fails the job (most branches are behind main).

The report lists file, line numbers at the merge base, a sample of the lines,
and the MR (number, title, merge time) that introduced them.

Blame names the *last* MR that touched a line: !1048 also undid lines that
!1043 had added and !1046 then edited; those are reported under !1046.

## Overriding

A deliberate revert (`Revert !N: ...`, or restoring what a stale commit
reverted, as !1047 and !1057 did) fails the guard by design. Add the
`revert-intended` label and run a new pipeline (a retried job keeps the labels
the pipeline started with).

## Results on history

Replayed on the last 80 merged MRs (2026-11-07, merges from !998 to !1073,
20 of them Renovate image updates; target = first parent of the merge commit,
head = second parent, clock = merge time):

| Result | MRs |
|---|---|
| Fail, true positive | !1048: fails on 12 files (!1046, !1040, !1041, !1013, !1044); the !989 nginx line and two more !1013 mode lines are notes (files the MR also edits) |
| Fail, deliberate revert (needs the label) | !1047 (`Revert !1041`), !1057 (restores what !1048 reverted) |
| Notes only, no failure | !1001, !1023, !1042, !1045, !1058 |
| Clean | the other 72 |

## Running it

    python3 ci/revert-guard/revert_guard.py --target origin/main --head HEAD
    python3 -m unittest discover -s ci/revert-guard -p 'test_*.py' -v

The job needs full history (`GIT_DEPTH: "0"`) to blame. Runtime is about
1-2 s for the analysis on this repo.
