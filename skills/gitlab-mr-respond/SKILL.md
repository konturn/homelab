---
name: gitlab-mr-respond
description: Respond to feedback on existing GitLab merge requests. Reads comments, understands intent, makes fixes, posts threaded replies, and updates MR description. Use when spawned by MR monitoring cron or when explicitly asked to address feedback on an open MR.
---

# GitLab MR Feedback Response

Handles the feedback loop for existing merge requests:
1. **Pre-flight validation** — Verify environment and MR state
2. **Comment analysis** — Understand what's being asked
3. **Implementation** — Make requested changes
4. **Reply** — Post threaded response in discussion
5. **Description update** — Update MR changelog
6. **Pipeline wait** — Ensure CI passes
7. **Exit** — Let cron continue monitoring

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
- Deploy only runs on `main` branch

---

## When You're Spawned

The MR monitoring cron spawns you with context including:
- **MR IID** — The merge request number
- **Branch name** — The feature branch
- **New comments** — Comments to address (with IDs for threading)
- **Original goal** — What the MR is trying to accomplish
- **Discussion IDs** — For posting threaded replies

---

## Pre-Flight Validation — REQUIRED

```bash
#!/bin/bash
set -e

echo "=== Pre-Flight Validation ==="

# 1. Check GITLAB_TOKEN
if [ -z "$GITLAB_TOKEN" ]; then
  echo "❌ GITLAB_TOKEN not set"
  exit 1
fi
echo "✓ GITLAB_TOKEN present"

# 2. Verify MR exists and is open
MR_STATE=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.state')

if [ "$MR_STATE" != "opened" ]; then
  echo "❌ MR !$MR_IID is not open (state: $MR_STATE)"
  exit 1
fi
echo "✓ MR !$MR_IID is open"

# 3. Check branch exists
BRANCH_EXISTS=$(curl -s -w "%{http_code}" -o /dev/null \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/repository/branches/$BRANCH_NAME" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN")

if [ "$BRANCH_EXISTS" != "200" ]; then
  echo "❌ Branch $BRANCH_NAME not found"
  exit 1
fi
echo "✓ Branch $BRANCH_NAME exists"

# 4. Verify tracking entry exists
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"
if [ -f "$TRACKING_FILE" ]; then
  TRACKED=$(jq -r --arg iid "$MR_IID" '.[$iid] // empty' "$TRACKING_FILE")
  if [ -n "$TRACKED" ]; then
    echo "✓ MR is tracked"
  else
    echo "⚠ MR not in tracking file (proceeding anyway)"
  fi
fi

echo "=== Pre-Flight Complete ==="
```

---

## Escalation Triggers — IMPORTANT

**Escalate immediately if:**

1. **Stuck >2 minutes** on any single operation
2. **Unknown errors** not covered in this skill
3. **Ambiguous feedback** — Can't determine what's being asked
4. **Repeated failures** — Same fix fails 3+ times
5. **Scope creep** — Feedback asks for something beyond original MR scope
6. **Conflict with goal** — Feedback contradicts the MR's purpose

**Escalation procedure:**

```
Use the message tool:
- action: send
- channel: telegram  
- message: "🚨 MR Feedback Issue\n\nMR: !<MR_IID>\nIssue: <describe the problem>\nComment: <quote the problematic feedback>\n\nNeed guidance — stopping."
```

**After escalating: STOP.** Do not guess. Do not keep trying. Wait for human input.

---

## Understanding Feedback Intent

Before making changes, categorize the feedback:

### Clear Actions (proceed immediately)
- "Fix the typo in line X" → Make the specific fix
- "Add X to the config" → Add the specified item
- "Remove the deprecated option" → Remove it
- "Use Y instead of Z" → Make the substitution

### Clarification Needed (ask first)
- "This doesn't look right" → What specifically?
- "Can you improve this?" → Improve how?
- "I'm not sure about X" → What's the concern?

### Beyond Scope (escalate)
- "While you're here, also add Y" → New feature, new MR
- "Actually, let's redesign this" → Scope change
- "This should work differently" → Architecture change

**When in doubt:** Ask for clarification via a reply comment, then wait for response.

---

## Core Workflow

### 1. Clone and Checkout Branch

```bash
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab

git config user.email "moltbot@nkontur.com"
git config user.name "Moltbot"

git checkout "$BRANCH_NAME"
git pull origin "$BRANCH_NAME"
```

### 2. Read Full MR Context

```bash
# Get MR details including current description
MR_JSON=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN")

CURRENT_DESC=$(echo "$MR_JSON" | jq -r '.description')
MR_TITLE=$(echo "$MR_JSON" | jq -r '.title')

# Get current version from tracking
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"
CURRENT_VERSION=$(jq -r --arg iid "$MR_IID" '.[$iid].version // 1' "$TRACKING_FILE")
NEW_VERSION=$((CURRENT_VERSION + 1))
```

### 3. Make Requested Changes

Implement the changes based on feedback. Keep changes focused on what was requested.

```bash
# Edit files as needed
# ...

git add -A
git commit -m "fix: address review feedback

- <specific change 1>
- <specific change 2>

Responds to comment by <reviewer>"
```

### 4. Push Changes

```bash
git push origin "$BRANCH_NAME"
```

### 5. Reply in Discussion Thread — REQUIRED

**Always reply in the existing thread** using the discussion endpoint:

```bash
# DISCUSSION_ID is provided by the cron when you're spawned
curl -s -X POST \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Done ✓\n\n<explain what you changed and why>"}'
```

**Reply content guidelines:**
- Start with "Done ✓" if you made the requested change
- Briefly explain what you changed
- If you made a different choice, explain why
- Keep it concise but informative

**If asking for clarification:**
```bash
curl -s -X POST \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Could you clarify what you mean by X? I want to make sure I address this correctly."}'
```

Then **exit and wait** for the cron to detect their response.

### 6. Update MR Description

**Keep the MR description as a living changelog:**

```bash
# Build new description with changelog entry
NEW_DESC=$(cat <<EOF
$CURRENT_DESC

- **v$NEW_VERSION**: <summary of changes made>
EOF
)

# Update via API (with retry for 409)
for i in 1 2 3; do
  HTTP_CODE=$(curl -s -w "%{http_code}" -o /tmp/update.json -X PUT \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg desc "$NEW_DESC" '{description: $desc}')")
  
  if [ "$HTTP_CODE" = "200" ]; then
    echo "✓ Updated MR description"
    break
  elif [ "$HTTP_CODE" = "409" ]; then
    sleep $i
  else
    echo "⚠ Failed to update description (HTTP $HTTP_CODE)"
    break
  fi
done
```

### 7. Update Tracking File

```bash
jq --arg iid "$MR_IID" \
   --arg desc "$NEW_DESC" \
   --argjson ver "$NEW_VERSION" \
   '.[$iid].description = $desc | .[$iid].version = $ver' \
   "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"
```

### 8. Wait for Pipeline — CRITICAL

**DO NOT respond "Done" until pipeline is green.** If pipeline fails, fix it first.

```bash
echo "Waiting for pipeline..."
MAX_ATTEMPTS=60
ATTEMPT=0
FIX_ATTEMPTS=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  PIPELINE_JSON=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/pipelines" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.[0] // empty')
  
  PIPELINE_STATUS=$(echo "$PIPELINE_JSON" | jq -r '.status // "pending"')
  PIPELINE_ID=$(echo "$PIPELINE_JSON" | jq -r '.id // empty')
  
  case "$PIPELINE_STATUS" in
    "success")
      echo "✓ Pipeline passed"
      break
      ;;
    "failed")
      echo "✗ Pipeline failed — YOU MUST FIX THIS"
      
      # Step A: Get failed job logs
      FAILED_JOBS=$(curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/pipelines/$PIPELINE_ID/jobs" \
        -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
      
      # Step B: For each failed job, fetch and display log
      for JOB_ID in $(echo "$FAILED_JOBS" | jq -r '.[] | select(.status == "failed") | .id'); do
        JOB_NAME=$(echo "$FAILED_JOBS" | jq -r ".[] | select(.id == $JOB_ID) | .name")
        echo "=== Failed job: $JOB_NAME ==="
        curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/jobs/$JOB_ID/trace" \
          -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | tail -80
      done
      
      # Step C: Diagnose and fix the issue
      # - Read the error message carefully
      # - Edit the relevant files to fix
      # - Common issues: YAML syntax, Jinja2 errors, missing vars
      
      # Step D: Commit and push fix
      # git add -A
      # git commit -m "fix: address pipeline failure"
      # git push origin $BRANCH_NAME
      
      # Step E: Check attempt limit
      FIX_ATTEMPTS=$((FIX_ATTEMPTS + 1))
      if [ $FIX_ATTEMPTS -ge 3 ]; then
        echo "❌ Failed 3 times — escalating to main session"
        # Post comment explaining you're stuck, then exit
        exit 1
      fi
      
      ATTEMPT=0
      ;;
    *)
      sleep 10
      ATTEMPT=$((ATTEMPT + 1))
      ;;
  esac
done
```

### 9. Cleanup and Exit

```bash
cd /
rm -rf "$WORK_DIR"
```

**Do NOT poll for more comments.** Exit and let the cron continue monitoring.

---

## When to Ask for Help vs Iterate

### Iterate on your own:
- Pipeline failures with clear error messages
- Simple implementation details
- Typos, formatting, small adjustments
- Obvious improvements within scope

### Ask for clarification:
- Vague feedback ("make this better")
- Multiple interpretations possible
- Trade-offs that need human judgment
- Conflicting requests

### Escalate to main session:
- Blocked for >2 minutes
- Errors you can't diagnose
- Scope changes requested
- Fundamental approach questioned
- You've tried 3+ times without success

---

## Handling Multiple Comments

If spawned with multiple comments to address:

1. **Group by topic** — Related comments can be addressed together
2. **Prioritize** — Critical issues first, style/nits last
3. **One commit per logical change** — Don't squash everything
4. **Reply to each thread** — Acknowledge every comment

```bash
# For each discussion thread
for DISCUSSION_ID in $DISCUSSION_IDS; do
  # Make relevant changes
  # Reply in that specific thread
done
```

---

## Error Handling

### GitLab Resource Locks (409 Conflict)

Retry with backoff (see examples above).

### Merge Conflicts

```bash
# If push fails due to conflicts
git fetch origin "$BRANCH_NAME"
git rebase origin/"$BRANCH_NAME"
# Resolve conflicts manually
git add -A
git rebase --continue
git push --force-with-lease origin "$BRANCH_NAME"
```

**If conflicts are complex:** Escalate rather than guess.

### Pipeline Failures After Your Changes

1. Check job logs: `glab ci view` or API
2. If your change caused it: fix immediately
3. If pre-existing: note in reply, proceed
4. If unclear: escalate

---

## Sub-Agent Feedback — REQUIRED

Before exiting, append feedback:

```bash
echo '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","session":"'$SESSION_LABEL'","mr":'$MR_IID',"type":"respond","outcome":"success|partial|failed","friction":"what was hard","suggestion":"how to improve","notes":"context"}' >> /home/node/clawd/skills/gitlab-mr/feedback.jsonl
```

---

## Related Skills

- **gitlab-mr-create** — For creating new merge requests
