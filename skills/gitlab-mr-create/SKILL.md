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

## Infrastructure Context

**GitLab Instance:** https://gitlab.lab.nkontur.com  
**Project:** root/homelab  
**Project ID:** 4 (use this for API calls, NOT 1)  
**Authentication:** `$GITLAB_TOKEN` environment variable  
**MR Tracking File:** `/home/node/clawd/memory/open-mrs.json`

**CI Runner Notes:**
- Runner has **no tags** — jobs run on default runner
- Pipeline stages: lint → validate → deploy
- Deploy only runs on `main` branch (MR pipelines skip deploy)

---

## Pre-Flight Validation — REQUIRED

**Before starting any work, validate your environment.** Failing fast saves time.

```bash
#!/bin/bash
set -e

echo "=== Pre-Flight Validation ==="

# 1. Check GITLAB_TOKEN exists
if [ -z "$GITLAB_TOKEN" ]; then
  echo "❌ GITLAB_TOKEN not set"
  exit 1
fi
echo "✓ GITLAB_TOKEN present"

# 2. Test API access
API_TEST=$(curl -s -w "%{http_code}" -o /tmp/api_test.json \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN")

if [ "$API_TEST" != "200" ]; then
  echo "❌ API access failed (HTTP $API_TEST)"
  cat /tmp/api_test.json
  exit 1
fi
echo "✓ API access confirmed"

# 3. Verify project details
PROJECT_NAME=$(jq -r '.path_with_namespace' /tmp/api_test.json)
if [ "$PROJECT_NAME" != "root/homelab" ]; then
  echo "❌ Wrong project: $PROJECT_NAME (expected root/homelab)"
  exit 1
fi
echo "✓ Project: $PROJECT_NAME"

# 4. Check runner status
RUNNER_STATUS=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/runners" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.[0].status // "unknown"')
echo "ℹ Runner status: $RUNNER_STATUS"

# 5. Check main branch is accessible
MAIN_CHECK=$(curl -s -w "%{http_code}" -o /dev/null \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/repository/branches/main" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
if [ "$MAIN_CHECK" != "200" ]; then
  echo "❌ Cannot access main branch"
  exit 1
fi
echo "✓ Main branch accessible"

echo "=== Pre-Flight Complete ==="
```

**If pre-flight fails:** Do NOT proceed. Report the error and stop.

---

## Escalation Triggers — IMPORTANT

**If any of these occur, escalate immediately:**

1. **Stuck >2 minutes** on any single operation (API call, git command, pipeline check)
2. **Unknown errors** — Any error not covered in this skill
3. **Repeated failures** — Same operation fails 3+ times
4. **Pipeline stuck** — No status change for >5 minutes

**Escalation procedure:**

```bash
# Use the message tool to notify main session
# action: send
# channel: telegram
# message: "🚨 MR Creation Stuck\n\nMR: $MR_IID\nBranch: $BRANCH\nIssue: <describe the problem>\n\nStopping to await guidance."
```

**After sending escalation: STOP.** Do not continue. Do not retry. Wait for human input.

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

## Changes

- List of files/configs modified
- Key implementation details

## Testing

How to verify this works.

## Changes Log

- **v1**: Initial implementation
EOF
)

MR_RESPONSE=$(curl -s -X POST "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests" \
     -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     -H "Content-Type: application/json" \
     -d "$(jq -n \
       --arg src "$BRANCH_NAME" \
       --arg title "$MR_TITLE" \
       --arg desc "$MR_DESCRIPTION" \
       '{source_branch: $src, target_branch: "main", title: $title, description: $desc}')")

MR_IID=$(echo "$MR_RESPONSE" | jq -r '.iid')
MR_URL=$(echo "$MR_RESPONSE" | jq -r '.web_url')

if [ "$MR_IID" = "null" ] || [ -z "$MR_IID" ]; then
  echo "❌ MR creation failed:"
  echo "$MR_RESPONSE" | jq .
  # ESCALATE
  exit 1
fi

echo "✓ Created MR !$MR_IID: $MR_URL"
```

### 5. Wait for Pipeline

```bash
echo "Waiting for pipeline..."
MAX_ATTEMPTS=60  # 10 minutes max
ATTEMPT=0
STUCK_START=""

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  PIPELINE_JSON=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/pipelines" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.[0] // empty')
  
  if [ -z "$PIPELINE_JSON" ]; then
    echo "No pipeline yet, waiting..."
    sleep 10
    ATTEMPT=$((ATTEMPT + 1))
    continue
  fi
  
  PIPELINE_STATUS=$(echo "$PIPELINE_JSON" | jq -r '.status')
  PIPELINE_ID=$(echo "$PIPELINE_JSON" | jq -r '.id')
  
  case "$PIPELINE_STATUS" in
    "success")
      echo "✓ Pipeline #$PIPELINE_ID passed"
      break
      ;;
    "failed"|"canceled")
      echo "✗ Pipeline $PIPELINE_STATUS — investigating..."
      # Get failed job logs
      FAILED_JOBS=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/pipelines/$PIPELINE_ID/jobs" \
        -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.[] | select(.status == "failed") | .name')
      echo "Failed jobs: $FAILED_JOBS"
      # Handle failure (fix and push), then reset
      # ... implementation-specific fixes ...
      ATTEMPT=0
      STUCK_START=""
      ;;
    "running"|"pending")
      # Check for stuck condition
      if [ -z "$STUCK_START" ]; then
        STUCK_START=$(date +%s)
      else
        ELAPSED=$(($(date +%s) - STUCK_START))
        if [ $ELAPSED -gt 300 ]; then
          echo "⚠ Pipeline stuck for >5 minutes — ESCALATING"
          # Send escalation via Telegram
          exit 1
        fi
      fi
      echo "Pipeline $PIPELINE_STATUS (attempt $ATTEMPT)"
      sleep 10
      ATTEMPT=$((ATTEMPT + 1))
      ;;
    *)
      echo "Unknown status: $PIPELINE_STATUS"
      sleep 10
      ATTEMPT=$((ATTEMPT + 1))
      ;;
  esac
done

if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
  echo "⚠ Pipeline timed out — ESCALATING"
  exit 1
fi
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

echo "✓ Registered MR !$MR_IID for tracking"
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

```bash
for i in 1 2 3; do
  HTTP_CODE=$(curl -s -w "%{http_code}" -o /tmp/response.json -X POST \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")
  
  if [ "$HTTP_CODE" = "201" ] || [ "$HTTP_CODE" = "200" ]; then
    break
  elif [ "$HTTP_CODE" = "409" ]; then
    echo "Resource locked, retrying in ${i}s..."
    sleep $i
  else
    echo "Error: HTTP $HTTP_CODE"
    cat /tmp/response.json
    break
  fi
done
```

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
