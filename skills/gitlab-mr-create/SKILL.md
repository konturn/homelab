---
name: gitlab-mr-create
description: Create new GitLab merge requests for homelab infrastructure. Handles branch creation, implementing changes, opening MRs, waiting for pipeline, and registering for tracking. Use when implementing new features, fixes, or configuration changes that need a fresh MR.
---

# GitLab MR Creation

Creates merge requests for homelab infrastructure changes:
1. **Pre-flight validation** — Verify environment and API access
2. **Branch creation** — Create feature branch from main
3. **Implementation** — Make code/config changes
4. **MR creation** — Open merge request via GitLab API
5. **Pipeline wait** — Wait for CI to pass (or handle failures)
6. **Registration** — Register MR for comment monitoring
7. **Notification** — Notify via Telegram when ready

---

## Shared Library

**Always source the shared GitLab library first:**

```bash
source /home/node/clawd/skills/gitlab/lib.sh
```

This provides: `wait_for_pipeline`, `push_and_wait`, `check_merge_conflicts`, `escalate`, `get_failed_job_logs`, `preflight_check`, `gitlab_api_call`

---

## Infrastructure Context

**GitLab Instance:** https://gitlab.lab.nkontur.com  
**Project:** root/homelab  
**Project ID:** 4 (use this for API calls, NOT 1)  
**Authentication:** `$GITLAB_TOKEN` environment variable  
**MR Tracking File:** `/home/node/clawd/memory/open-mrs.json`

---

## Issue Tracking — CRITICAL

**Keep issues and MRs in sync. This is mandatory.**

1. **When working on a backlog issue:** Include `Closes #N` in MR description (auto-closes issue on merge)
2. **When MR is merged:** Verify the linked issue was closed
3. **When MR is closed without merge:** Update or close the issue manually with explanation
4. **When creating new work:** Check if a backlog issue exists first; create one if it doesn't

**The backlog is the source of truth.** Don't let MRs exist without corresponding issues, and don't let issues stay open after their MR merges.

```bash
# Check if issue exists for your work
curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/issues?labels=agent-backlog&search=your+topic" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq '.[].iid'

# Create issue if needed
curl -s -X POST "https://gitlab.lab.nkontur.com/api/v4/projects/4/issues" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title": "Issue title", "description": "What and why", "labels": "agent-backlog"}'
```

**CI Runner Notes:**
- Runner has **no tags** — jobs run on default runner
- Pipeline stages: lint → validate → deploy
- Deploy only runs on `main` branch (MR pipelines skip deploy)

---

## MR Locking — REQUIRED

**Prevent cron/sub-agent conflicts by locking MRs you're working on.**

When you start working on an MR, set a lock in the tracking file. This tells the comment-monitor cron to skip this MR.

```bash
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"
SESSION_LABEL="gitlab.mr.${MR_IID}.your-task"
TIMESTAMP=$(date +%s)000  # milliseconds

# Set lock when starting work
jq --arg iid "$MR_IID" --arg session "$SESSION_LABEL" --argjson ts "$TIMESTAMP" \
  '.[$iid].lockedBy = $session | .[$iid].lockedAt = $ts' \
  "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"

# ... do your work ...

# Clear lock when done (success or failure)
jq --arg iid "$MR_IID" 'del(.[$iid].lockedBy, .[$iid].lockedAt)' \
  "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"
```

**Lock schema:**
```json
{
  "35": {
    "title": "...",
    "lastCommentId": 0,
    "lockedBy": "gitlab.mr.35.fix-pipeline",
    "lockedAt": 1769973600000
  }
}
```

**Rules:**
- Always set lock before starting work
- Always clear lock on exit (use trap for cleanup)
- Locks expire after 1 hour (cron ignores stale locks)

---

## Pre-Flight Validation — REQUIRED

```bash
#!/bin/bash
set -e

source /home/node/clawd/skills/gitlab/lib.sh

if ! preflight_check; then
  escalate "Pre-flight validation failed"
fi
```

**If pre-flight fails:** Do NOT proceed. The escalate function will notify and exit.

---

## Escalation Triggers — IMPORTANT

**If any of these occur, escalate immediately:**

1. **Stuck >2 minutes** on any single operation (API call, git command, pipeline check)
2. **Unknown errors** — Any error not covered in this skill
3. **Repeated failures** — Same operation fails 3+ times
4. **Pipeline stuck** — No status change for >5 minutes

**Escalation is built into the library:**

```bash
# This sends Telegram notification to Noah and exits
escalate "MR Creation Stuck\n\nMR: $MR_IID\nBranch: $BRANCH\nIssue: <describe the problem>"
```

---

## Workspace Setup

**Always clone a fresh copy to a temp directory.** Never use the shared workspace at `/home/node/clawd/homelab`.

```bash
# Create isolated workspace
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

# Clone fresh copy with auth
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab

# Configure git identity
git config user.email "moltbot@nkontur.com"
git config user.name "Moltbot"
```

---

## glab CLI Setup

```bash
export GITLAB_HOST=gitlab.lab.nkontur.com
export GITLAB_TOKEN="${GITLAB_TOKEN}"
```

---

## Complexity Assessment — Before Implementation

**Before writing code, assess the task complexity:**

### Simple (proceed directly):
- Single file change
- Adding/updating environment variables
- Obvious config tweaks
- Pattern already exists elsewhere in repo

### Complex (bubble up first):
- Touches 4+ files
- New service or major component
- Architectural decisions involved
- Novel pattern not seen in repo before
- Multiple ways to implement it
- You're unsure about the approach

**If complex:** Draft a brief plan and ask for confirmation before implementing:

```
Use sessions_send to main session:
"Planning MR for: [task]

Approach:
- [What files to change]
- [Key implementation decisions]
- [Potential risks or tradeoffs]

Questions:
- [Any uncertainties]

Proceed with this approach?"
```

**Wait for approval before implementing complex changes.** This catches wrong approaches early, before wasted effort.

For simple changes, proceed directly — don't add overhead where it's not needed.

---

## Core Workflow

### 1. Create Feature Branch

```bash
# Always branch from latest main
git checkout main
git pull origin main
git checkout -b feature/brief-description
```

**Branch naming:**
- `feature/` — New functionality
- `fix/` — Bug fixes
- `perf/` — Performance improvements
- `docs/` — Documentation only

### 2. Implement Changes

- Edit configuration files, docker-compose.yml, Ansible playbooks
- Test changes locally if possible
- Commit with conventional commit messages:

```bash
git add -A
git commit -m "feat(scope): brief description

Longer explanation if needed.
- Detail 1
- Detail 2"
```

**Commit prefixes:** `feat:`, `fix:`, `perf:`, `docs:`, `refactor:`, `chore:`

### 3. Push Branch

```bash
git push -u origin feature/branch-name
```

### 4. Create MR via API

```bash
BRANCH_NAME="feature/branch-name"
MR_TITLE="feat(scope): Brief description"
MR_DESCRIPTION=$(cat <<'EOF'
## Summary

What this MR does and why.

**Closes #N** <!-- If working on a backlog issue, include this to auto-close it -->

## Changes

- List of files/configs modified
- Key implementation details

## Testing

How to verify this works.

## Changes Log

- **v1**: Initial implementation
EOF
)

# Use the library helper for API calls with retry
http_code=$(gitlab_api_call POST "/projects/${PROJECT_ID}/merge_requests" \
  "$(jq -n \
    --arg src "$BRANCH_NAME" \
    --arg title "$MR_TITLE" \
    --arg desc "$MR_DESCRIPTION" \
    '{source_branch: $src, target_branch: "main", title: $title, description: $desc}')")

if [ "$http_code" != "201" ]; then
  echo "❌ MR creation failed (HTTP $http_code):"
  cat /tmp/gitlab_response.json | jq .
  escalate "MR creation failed for branch $BRANCH_NAME"
fi

MR_IID=$(jq -r '.iid' /tmp/gitlab_response.json)
MR_URL=$(jq -r '.web_url' /tmp/gitlab_response.json)

echo "✅ Created MR !$MR_IID: $MR_URL"
```

### 5. Wait for Pipeline

**Use the shared library function:**

```bash
FIX_ATTEMPTS=0
MAX_FIX_ATTEMPTS=3

while true; do
  if wait_for_pipeline "$MR_IID"; then
    echo "✅ Pipeline passed"
    break
  else
    echo "❌ Pipeline failed — investigating..."
    
    # The library already output the job logs, now diagnose and fix
    # Common failures:
    # - Ansible syntax error → fix the YAML
    # - Docker-compose validation error → fix compose syntax
    # - Missing variable → add the variable
    # - Linting error → fix the style issue
    # - Jinja2 template error → fix template syntax
    
    FIX_ATTEMPTS=$((FIX_ATTEMPTS + 1))
    if [ $FIX_ATTEMPTS -ge $MAX_FIX_ATTEMPTS ]; then
      escalate "Failed to fix pipeline after $MAX_FIX_ATTEMPTS attempts\n\nMR: !$MR_IID\nBranch: $BRANCH_NAME"
    fi
    
    # MAKE THE FIX
    # - Edit the relevant files based on error logs
    # - Test locally if possible (ansible-lint, docker-compose config, etc.)
    
    # COMMIT AND PUSH THE FIX
    git add -A
    git commit -m "fix: address pipeline failure - <describe fix>"
    git push origin "$BRANCH_NAME"
    
    echo "Fix attempt $FIX_ATTEMPTS pushed. Waiting for new pipeline..."
    sleep 5  # Give GitLab time to register new pipeline
  fi
done
```

### 6. Register MR for Tracking — REQUIRED

```bash
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"

# Initialize if needed
if [ ! -f "$TRACKING_FILE" ]; then
  echo '{}' > "$TRACKING_FILE"
fi

# Add MR to tracking
jq --arg iid "$MR_IID" \
   --arg title "$MR_TITLE" \
   --arg branch "$BRANCH_NAME" \
   --arg goal "$ORIGINAL_GOAL" \
   --arg desc "$MR_DESCRIPTION" \
   '. + {($iid): {"title": $title, "branch": $branch, "goal": $goal, "description": $desc, "lastCommentId": 0, "version": 1, "activeResolutions": []}}' \
   "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"

echo "✅ Registered MR !$MR_IID for tracking"
```

**Tracking schema:**
```json
{
  "42": {
    "title": "MR title",
    "branch": "feature/branch-name",
    "goal": "Original goal description",
    "description": "Current MR description",
    "lastCommentId": 123,
    "version": 1,
    "activeResolutions": []
  }
}
```

### 7. Notify via Telegram

```
Use the message tool:
- action: send
- channel: telegram
- message: "🔀 MR Ready for Review: <title>\n\n<brief description>\n\n<MR_URL>"
```

### 8. Cleanup and Exit

```bash
# Cleanup temp directory
cd /
rm -rf "$WORK_DIR"
```

**Do NOT implement comment polling loops.** A cron job handles that. Exit cleanly after notification.

---

## MR Description Template

```markdown
## Summary

[What this MR does and why — 1-2 sentences]

## Changes

- [File/config 1]: [What changed]
- [File/config 2]: [What changed]

## Testing

[How to verify this works]

## Changes Log

- **v1**: Initial implementation
```

Update this description when making follow-up changes (see gitlab-mr-respond skill).

---

## Error Handling

### GitLab Resource Locks (409 Conflict)

The `gitlab_api_call` helper automatically retries on 409 errors.

### Common Pipeline Failures

| Error | Cause | Fix |
|-------|-------|-----|
| `ansible-lint` | Linting violation | Fix the specific lint error |
| `yaml: line X` | YAML syntax error | Check indentation, quotes |
| `undefined variable` | Missing Jinja2 var | Add to defaults or vars |
| `docker-compose: invalid` | Compose syntax | Validate with `docker-compose config` |

---

## Sub-Agent Feedback — REQUIRED

Before exiting, append feedback to `/home/node/clawd/skills/gitlab-mr/feedback.jsonl`:

```bash
echo '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","session":"'$SESSION_LABEL'","mr":'$MR_IID',"type":"create","outcome":"success|partial|failed","friction":"what was hard","suggestion":"how to improve","notes":"context"}' >> /home/node/clawd/skills/gitlab-mr/feedback.jsonl
```

---

## Related Skills

- **gitlab-mr-respond** — For responding to feedback on existing MRs
