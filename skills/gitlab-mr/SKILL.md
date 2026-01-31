---
name: gitlab-mr
description: Comprehensive GitLab MR lifecycle management for homelab infrastructure. Creates feature branches, implements changes, opens MRs, monitors for feedback, and iterates until merged. Use when implementing infrastructure changes, fixing CI/CD issues, updating configurations, or any task requiring a complete MR workflow from branch creation to merge.
---

# GitLab MR Lifecycle Management

Automates the complete merge request lifecycle for homelab infrastructure changes:

1. **Branch creation** - Creates feature branches from main
2. **Implementation** - Makes code/config changes to accomplish goals
3. **MR creation** - Opens merge requests via GitLab API
4. **Tracking** - Registers MR for comment monitoring
5. **Iteration** - Responds to feedback (triggered by cron when comments detected)
6. **Completion** - Cleans up tracking when merged/closed

## Environment Setup

**GitLab Instance:** https://gitlab.lab.nkontur.com  
**Project:** root/homelab (Project ID: 4)  
**Authentication:** `$GITLAB_TOKEN` environment variable  
**MR Tracking File:** `/home/node/clawd/memory/open-mrs.json`

## Workspace Setup — IMPORTANT

**Always clone a fresh copy to a temp directory.** Never use the shared workspace at `/home/node/clawd/homelab` — that can cause conflicts with other agents.

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

## glab CLI Setup

The `glab` CLI is available for MR operations. Configure it before use:

```bash
export GITLAB_HOST=gitlab.lab.nkontur.com
export GITLAB_TOKEN="${GITLAB_TOKEN}"
```

## Core Workflow

### 1. Setup Fresh Workspace
```bash
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab
git config user.email "moltbot@nkontur.com"
git config user.name "Moltbot"
```

### 2. Create Feature Branch
```bash
git checkout -b feature/brief-description
```

### 3. Implement Changes
- Edit configuration files, docker-compose.yml, Ansible playbooks
- Commit with conventional commit messages (`feat:`, `fix:`, `perf:`, `docs:`)

### 4. Push and Create MR
```bash
git push -u origin feature/branch-name
```

Create MR via API:
```bash
MR_RESPONSE=$(curl -s -X POST "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests" \
     -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "source_branch": "feature/branch-name",
       "target_branch": "main", 
       "title": "Descriptive title",
       "description": "Detailed description"
     }')

MR_IID=$(echo "$MR_RESPONSE" | jq -r '.iid')
MR_URL=$(echo "$MR_RESPONSE" | jq -r '.web_url')
```

### 5. Register MR for Tracking — REQUIRED

After creating an MR, **you must register it** so the cron job can monitor for comments:

```bash
# Read existing tracking file (or initialize empty)
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"
if [ ! -f "$TRACKING_FILE" ]; then
  echo '{}' > "$TRACKING_FILE"
fi

# Add this MR to tracking (include description for changelog updates)
jq --arg iid "$MR_IID" \
   --arg title "$MR_TITLE" \
   --arg branch "$BRANCH_NAME" \
   --arg goal "$ORIGINAL_GOAL" \
   --arg desc "$MR_DESCRIPTION" \
   '. + {($iid): {"title": $title, "branch": $branch, "goal": $goal, "description": $desc, "lastCommentId": 0, "version": 1}}' \
   "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"
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
    "version": 2
  }
}
```

The `version` field tracks how many iterations of changes have been made (for changelog entries like "v2: Fixed pipeline").

Or using the `write` tool to update the JSON directly.

### 6. Update MR Description When Making Changes

**Keep the MR description current** as you iterate. When you fix pipeline issues or respond to feedback, update the description to reflect what's changed. This creates a living changelog.

```bash
# Update MR description via API
curl -s -X PUT "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "description": "## Summary\n\nOriginal description here...\n\n## Changes Log\n\n- **v2**: Fixed pipeline failure (missing dependency)\n- **v1**: Initial implementation"
  }'
```

**Description structure:**
```markdown
## Summary
What this MR does and why.

## Changes
- List of files/configs modified
- Key implementation details

## Changes Log
- **v3**: Addressed review feedback (added X, removed Y)
- **v2**: Fixed CI failure (typo in config)
- **v1**: Initial implementation

## Testing
How to verify this works.
```

This helps Noah see at a glance what's happened without reading through all the commits and comments.

### 7. Wait for Pipeline to Pass

Before reporting done, **wait for the CI pipeline to pass**. An MR with a red pipeline isn't done.

```bash
# Poll pipeline status until it completes
echo "Waiting for pipeline..."
MAX_ATTEMPTS=60  # 10 minutes max (60 * 10s)
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  PIPELINE_STATUS=$(glab mr view "$MR_IID" --output json 2>/dev/null | jq -r '.pipeline.status // "pending"')
  
  case "$PIPELINE_STATUS" in
    "success")
      echo "✓ Pipeline passed"
      break
      ;;
    "failed"|"canceled")
      echo "✗ Pipeline $PIPELINE_STATUS — investigating..."
      # Check job logs, fix issues, push again, reset counter
      # ... handle failure ...
      ATTEMPT=0
      ;;
    *)
      echo "Pipeline status: $PIPELINE_STATUS (attempt $ATTEMPT)"
      sleep 10
      ATTEMPT=$((ATTEMPT + 1))
      ;;
  esac
done

if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
  echo "⚠ Pipeline timed out — reporting anyway"
fi
```

**If pipeline fails:**
1. Check the job logs: `glab ci view`
2. Fix the issue in your branch
3. Commit and push the fix
4. Reset the polling loop

Only report the MR as complete once the pipeline is green (or you've exhausted reasonable retry attempts).

### 7. Notify via Telegram

After the pipeline passes, **send a Telegram notification** so Noah knows an MR is ready for review:

```
Use the message tool:
- action: send
- channel: telegram
- message: "🔀 MR Ready: <title>\n\n<brief description of what it does>\n\n<MR_URL>"
```

This ensures Noah doesn't have to check GitLab constantly — he gets a ping when something needs attention.

### 8. Report and Exit

After the pipeline passes, tracking is registered, and Telegram notification is sent, report the MR link and exit. A cron job will monitor for comments and spawn a new agent if Noah responds.

**Do NOT implement comment polling loops.** The cron handles that. But DO wait for pipeline success before exiting.

---

## Responding to Comments (for cron-spawned agents)

If you were spawned by the MR monitoring cron, you'll receive:
- The MR IID and branch name
- The new comments to address (including comment IDs for threading replies)
- The original goal

**Your workflow:**

1. **Clone the repo and checkout the branch:**
```bash
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab
git checkout feature/branch-name
git pull origin feature/branch-name
```

2. **Read and understand the comments**

3. **Make requested changes**

4. **Commit and push:**
```bash
git add -A
git commit -m "Address review feedback: <summary>"
git push origin feature/branch-name
```

5. **Reply in the discussion thread:**

Reply directly in the existing discussion thread. The cron passes you both the `COMMENT_ID` and `DISCUSSION_ID` — use the discussion endpoint for proper threading:

```bash
# Reply within the existing discussion thread
curl -s -X POST \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Done — <explain what you changed>"}'
```

**Important:** Always use the discussions endpoint with the `discussion_id`, not just `/notes`. This keeps replies threaded under the original comment.

6. **Update the MR description:**

After making changes, update the MR description with a changelog entry so the history is visible at a glance:

```bash
# Get current description
CURRENT_DESC=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.description')

# Append to changelog (or update programmatically)
NEW_DESC="$CURRENT_DESC\n- **v2**: Addressed feedback — <summary of changes>"

curl -s -X PUT "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"description\": \"$NEW_DESC\"}"
```

7. **Wait for pipeline to pass** (same as earlier for new MRs)

8. **Exit.** The cron will continue monitoring for further comments.

---

## Cleanup (when MR is merged/closed)

The cron job automatically removes merged/closed MRs from tracking. No manual cleanup needed.

---

## Key Guidelines

- **Fresh clone every time** — Avoid workspace conflicts
- **Never push to main** — Always use feature branches
- **Register every MR** — Or comments won't be detected
- **No polling loops** — Let cron handle monitoring
- **Cleanup temp dirs** — `rm -rf "$WORK_DIR"` when done
- **Always report feedback** — Help improve this skill (see below)

---

## Error Handling

### GitLab Resource Locks (409 Conflict)

GitLab can temporarily lock resources during updates (MR descriptions, notes, etc.). If you get a `409 Conflict` error:

```bash
# Retry with exponential backoff
for i in 1 2 3; do
  RESPONSE=$(curl -s -w "\n%{http_code}" -X PUT \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")
  
  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  if [ "$HTTP_CODE" = "200" ]; then
    break
  elif [ "$HTTP_CODE" = "409" ]; then
    echo "Resource locked, retrying in ${i}s..."
    sleep $i
  else
    echo "Unexpected error: $HTTP_CODE"
    break
  fi
done
```

**Common causes:**
- Another process updating the same MR
- GitLab internal housekeeping
- Rapid successive updates

**Solution:** Sleep 1-3 seconds and retry. Usually succeeds on second attempt.

---

## Sub-Agent Feedback — REQUIRED

**Before exiting, every sub-agent must report feedback** to help improve this skill.

Ask yourself:
- Did I hit any friction or confusion?
- Was something in the skill unclear or missing?
- Did I have to improvise or figure something out?
- Did the pipeline fail for a reason the skill should document?
- What would have made this easier?

**Feedback file:** `/home/node/clawd/skills/gitlab-mr/feedback.jsonl`

**Append a JSON line:**
```bash
echo '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","session":"'$SESSION_LABEL'","mr":'$MR_IID',"outcome":"success|partial|failed","friction":"what was hard or unclear","suggestion":"how to improve the skill","notes":"any other context"}' >> /home/node/clawd/skills/gitlab-mr/feedback.jsonl
```

**Example entries:**
```json
{"ts":"2026-01-31T02:00:00Z","session":"gitlab.mr.21.feedback","mr":21,"outcome":"success","friction":"none","suggestion":"none","notes":"Clean run, no issues"}
{"ts":"2026-01-31T02:30:00Z","session":"gitlab.mr.22.feedback","mr":22,"outcome":"partial","friction":"Pipeline failed with ansible-lint error not covered in skill","suggestion":"Add section on common ansible-lint fixes","notes":"Had to google the fix"}
{"ts":"2026-01-31T03:00:00Z","session":"gitlab.mr.14.feedback","mr":14,"outcome":"failed","friction":"Couldn't figure out how to test locally","suggestion":"Add local testing instructions for docker-compose changes","notes":"Gave up after 3 attempts"}
```

**Outcome values:**
- `success` — Completed the task without issues
- `partial` — Completed but hit friction or had to improvise  
- `failed` — Could not complete the task

Even successful runs should report — knowing what works well is valuable too.

The main session reviews this feedback during heartbeats and updates the skill accordingly.

---

## Bundled Resources

- **[API Reference](references/api.md)** - GitLab API endpoints
- **[Workflow Details](references/workflow.md)** - Additional patterns
