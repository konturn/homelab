---
name: gitlab-mr
description: GitLab MR operations for homelab infrastructure. Reference doc for creating MRs, responding to feedback, and managing the pipeline. You know git — this covers the repo-specific stuff.
---

# GitLab MR Reference

You know how to use git. This doc covers what's specific to this repo.

---

## Environment

```bash
source /home/node/clawd/skills/gitlab/lib.sh  # Always source first
```

| Variable | Value |
|----------|-------|
| `GITLAB_HOST` | gitlab.lab.nkontur.com |
| `PROJECT_ID` | **4** (not 1 — common mistake) |
| `GITLAB_API` | https://gitlab.lab.nkontur.com/api/v4 |
| `GITLAB_TOKEN` | From environment |

---

## Permissions (as of 2026-02-02)

- **Maintainer** — can merge own MRs
- **CI/CD secrets** — can create/modify pipeline variables
- **Self-merge policy:** Trivial changes and iteration = merge freely. Architectural changes = get Noah's review.

---

## lib.sh Functions

| Function | Purpose |
|----------|---------|
| `preflight_check` | Validate env, API access, project |
| `wait_for_pipeline $MR_IID` | Poll until pass/fail (~10 min max), outputs failed job logs |
| `push_and_wait $BRANCH $MR_IID` | Push + wait_for_pipeline |
| `check_merge_conflicts $BRANCH` | Returns 0 if clean, 1 if conflicts |
| `gitlab_api_call $METHOD $ENDPOINT [$DATA]` | API call with 409 retry, response in `/tmp/gitlab_response.json` |
| `escalate $MESSAGE` | Telegram Noah and exit 1 |
| `get_failed_job_logs $PIPELINE_ID` | Fetch logs from failed jobs |

---

## Gotchas

1. **Project ID is 4**, not 1. API calls to project 1 will fail silently or hit wrong project.

2. **⚠️ CRITICAL: Use `git worktree` for branch isolation.** Multiple sub-agents run concurrently on the same repo. If you checkout branches in the shared clone, dirty files from other agents WILL contaminate your branch. This has caused leaked diffs in MRs repeatedly.
   ```bash
   REPO="/home/node/.openclaw/workspace/homelab"
   BRANCH="feat/my-thing"
   WORK_DIR="/tmp/homelab-${BRANCH}"
   
   cd "$REPO" && git fetch origin main
   git worktree add -b "$BRANCH" "$WORK_DIR" origin/main
   cd "$WORK_DIR"
   git config user.email "moltbot@nkontur.com"
   git config user.name "Moltbot"
   
   # ... make changes, commit, push ...
   
   # Clean up when done
   cd /tmp && git -C "$REPO" worktree remove "$WORK_DIR" 2>/dev/null
   ```
   **NEVER `git checkout` in `/home/node/.openclaw/workspace/homelab`** — that's the shared clone. Always use worktrees.

3. **409 Conflict errors** — `gitlab_api_call` handles retry automatically.

4. **Pipeline stages:** validate → build → deploy → configure → bootstrap (deploy+ main only)

5. **Every MR must update relevant docs.** Changed pipeline structure → update ASCII diagram in `.gitlab-ci.yml` header. New secrets/auth → update `docs/SECRET_ROTATION.md`. Network rules → update `docs/FIREWALL.md`. New services → update `docs/DISASTER_RECOVERY.md`. Code without doc updates will be sent back.

6. **No runner tags** — jobs use default runner, don't specify tags.

7. **Git identity:**
   ```bash
   git config user.email "moltbot@nkontur.com"
   git config user.name "Moltbot"
   ```

---

## Issue ↔ MR Sync

**Backlog issues:** https://gitlab.lab.nkontur.com/root/homelab/-/issues?label_name=agent-backlog

- Working on a backlog issue → include `Closes #N` in MR description
- MR merged → verify issue auto-closed
- MR abandoned → close/update the issue manually
- New work → check if issue exists, create one if not

Don't leave orphaned issues or untracked MRs.

---

## MR Tracking

**File:** `/home/node/clawd/memory/open-mrs.json`

Track open MRs for the comment-monitor cron:
```json
{
  "64": {
    "title": "feat: Add BRAVE_API_KEY",
    "branch": "feat/brave-api-key",
    "lastCommentId": 0,
    "version": 1
  }
}
```

**Locking:** If doing extended work on an MR, set `lockedBy` and `lockedAt` to prevent cron conflicts. Clear when done.

---

## When to Escalate

- Stuck >2 min on any operation
- Unknown errors
- Same fix fails 3+ times
- Merge conflicts need manual resolution
- Scope creep or architectural questions
- Ambiguous feedback you can't interpret

```bash
escalate "MR !64: Pipeline keeps failing on docker build\n\nTried: X, Y, Z"
```

---

## When to Self-Merge

✅ **Merge freely:**
- Config tweaks, env vars, simple additions
- Your fix worked and pipeline is green
- Iterating on your own MR
- Trivial changes

🛑 **Get review:**
- New services or major components
- Changes touching 5+ files
- Architectural decisions
- You're unsure

---

## Quick Patterns

**Create MR:**
```bash
source /home/node/clawd/skills/gitlab/lib.sh
preflight_check || exit 1

# ... clone, branch, make changes, commit ...

# Create MR
gitlab_api_call POST "/projects/4/merge_requests" \
  '{"source_branch":"feat/thing","target_branch":"main","title":"feat: Thing"}'
MR_IID=$(jq -r '.iid' /tmp/gitlab_response.json)

# Wait for pipeline, merge if appropriate
wait_for_pipeline "$MR_IID" && \
  gitlab_api_call PUT "/projects/4/merge_requests/$MR_IID/merge"
```

**Reply to comment:**
```bash
gitlab_api_call POST "/projects/4/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  '{"body":"Done ✓ — fixed the thing"}'
```

**Check unresolved comments:**
```bash
curl -s "${GITLAB_API}/projects/4/merge_requests/$MR_IID/discussions" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | \
  jq '[.[] | select(.notes[0].resolvable and .notes[0].resolved == false)] | length'
```

---

## Common Pipeline Failures

| Error | Fix |
|-------|-----|
| `ansible-lint` failure | Fix the specific lint rule violation |
| `yaml: line X` | Check indentation, missing quotes |
| `undefined variable` | Add to defaults/vars or inventory |
| `docker-compose: invalid` | Run `docker-compose config` locally to debug |
| Jinja2 template error | Check template syntax, missing filters |

---

## Feedback

After completing work, append to `/home/node/clawd/skills/gitlab-mr/feedback.jsonl`:
```json
{"ts":"2026-02-02T19:00:00Z","mr":64,"outcome":"success","friction":"none","notes":"worked first try"}
```

---

## That's It

You know git. You know APIs. The above is what's specific to this repo. Go ship.
