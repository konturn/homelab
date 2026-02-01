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

**CI Runner Notes:**
- Runner has **no tags** — jobs run on default runner
- Pipeline stages: lint → validate → deploy
- Deploy only runs on `main` branch

---

## Issue Tracking — CRITICAL

**Keep issues and MRs in sync at all times.**

- If the MR references an issue (`Closes #N`), verify the link is still valid
- If feedback changes the MR scope significantly, update the linked issue description
- If the MR is abandoned/closed, update or close the related issue with explanation
- **Never leave orphaned issues** — if work is done, issues should be closed

When responding to feedback that changes scope, consider whether the issue description needs updating too.

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

source /home/node/clawd/skills/gitlab/lib.sh

# Basic environment check
if ! preflight_check; then
  escalate "Pre-flight validation failed"
fi

# Verify MR exists and is open
MR_STATE=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/merge_requests/$MR_IID" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.state')

if [ "$MR_STATE" != "opened" ]; then
  escalate "MR !$MR_IID is not open (state: $MR_STATE)"
fi
echo "✅ MR !$MR_IID is open"

# Check branch exists
BRANCH_CODE=$(curl -s -w "%{http_code}" -o /dev/null \
  "${GITLAB_API}/projects/${PROJECT_ID}/repository/branches/$BRANCH_NAME" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN")

if [ "$BRANCH_CODE" != "200" ]; then
  escalate "Branch $BRANCH_NAME not found"
fi
echo "✅ Branch $BRANCH_NAME exists"
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

**Use the library escalate function:**

```bash
escalate "MR Feedback Issue\n\nMR: !$MR_IID\nIssue: <describe the problem>\nComment: <quote the problematic feedback>"
```

**After escalating: STOP.** The escalate function exits automatically.

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
source /home/node/clawd/skills/gitlab/lib.sh

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
MR_JSON=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/merge_requests/$MR_IID" \
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

### 4. Push Changes and Wait for Pipeline

**Use the combined push_and_wait function:**

```bash
FIX_ATTEMPTS=0
MAX_FIX_ATTEMPTS=3

while true; do
  # Check for merge conflicts first
  if ! check_merge_conflicts "$BRANCH_NAME"; then
    escalate "Merge conflicts detected on $BRANCH_NAME — manual resolution needed"
  fi
  
  # Push and wait for pipeline
  if push_and_wait "$BRANCH_NAME" "$MR_IID"; then
    echo "✅ Changes pushed and pipeline passed"
    break
  else
    echo "❌ Pipeline failed — investigating..."
    
    FIX_ATTEMPTS=$((FIX_ATTEMPTS + 1))
    if [ $FIX_ATTEMPTS -ge $MAX_FIX_ATTEMPTS ]; then
      escalate "Failed to fix pipeline after $MAX_FIX_ATTEMPTS attempts\n\nMR: !$MR_IID"
    fi
    
    # DIAGNOSE AND FIX based on job logs (already output by wait_for_pipeline)
    # - Edit the relevant files
    # - Test locally if possible
    
    git add -A
    git commit -m "fix: address pipeline failure"
    
    echo "Fix attempt $FIX_ATTEMPTS. Retrying..."
  fi
done
```

### 5. Reply in Discussion Thread — REQUIRED

**Always reply in the existing thread** using the discussion endpoint:

```bash
# DISCUSSION_ID is provided by the cron when you're spawned
http_code=$(gitlab_api_call POST \
  "/projects/${PROJECT_ID}/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  '{"body": "Done ✓\n\n<explain what you changed and why>"}')

if [ "$http_code" != "201" ]; then
  echo "⚠️ Failed to post reply (HTTP $http_code)"
fi
```

**Reply content guidelines:**
- Start with "Done ✓" if you made the requested change
- Briefly explain what you changed
- If you made a different choice, explain why
- Keep it concise but informative

**If asking for clarification:**
```bash
gitlab_api_call POST \
  "/projects/${PROJECT_ID}/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  '{"body": "Could you clarify what you mean by X? I want to make sure I address this correctly."}'
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

# Update via API using the library helper
http_code=$(gitlab_api_call PUT "/projects/${PROJECT_ID}/merge_requests/$MR_IID" \
  "$(jq -n --arg desc "$NEW_DESC" '{description: $desc}')")

if [ "$http_code" = "200" ]; then
  echo "✅ Updated MR description"
else
  echo "⚠️ Failed to update description (HTTP $http_code)"
fi
```

### 7. Update Tracking File

```bash
jq --arg iid "$MR_IID" \
   --arg desc "$NEW_DESC" \
   --argjson ver "$NEW_VERSION" \
   '.[$iid].description = $desc | .[$iid].version = $ver' \
   "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"
```

### 8. Cleanup and Exit

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

The `gitlab_api_call` helper automatically retries on 409 errors.

### Merge Conflicts

Use the library function to detect:

```bash
if ! check_merge_conflicts "$BRANCH_NAME"; then
  # Manual resolution needed — escalate
  escalate "Merge conflicts on $BRANCH_NAME require manual resolution"
fi
```

### Pipeline Failures After Your Changes

The `push_and_wait` function handles this, but if you need manual control:

1. Use `get_failed_job_logs $PIPELINE_ID` to see what failed
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
