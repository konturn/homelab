# Detailed Workflow Logic

Step-by-step implementation guide for the complete GitLab MR lifecycle.

## Phase 1: Setup and Branch Creation

1. **Validate environment**
   ```bash
   # Verify GitLab token
   curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
        "https://gitlab.lab.nkontur.com/api/v4/projects/4" > /dev/null
   
   # Verify repo exists and is clean
   cd /home/node/clawd/homelab
   git status --porcelain
   ```

2. **Create feature branch**
   ```bash
   # Get latest main
   git checkout main
   git pull origin main
   
   # Create and switch to feature branch
   BRANCH_NAME="feature/$(echo "$GOAL" | tr ' ' '-' | tr '[:upper:]' '[:lower:]' | head -c 40)"
   git checkout -b "$BRANCH_NAME"
   ```

3. **Validate branch creation**
   ```bash
   # Confirm we're on the new branch
   git branch --show-current
   # Should output the feature branch name
   ```

## Phase 2: Implementation

1. **Analyze goal and plan changes**
   - Read relevant files in the homelab repo
   - Understand the change requirements
   - Identify files that need modification

2. **Make incremental changes**
   - Edit files systematically
   - Test syntax/configuration validity where possible
   - Commit logically grouped changes

3. **Commit strategy**
   ```bash
   # Make descriptive commits
   git add specific-files
   git commit -m "Specific change description"
   
   # Push frequently to feature branch
   git push -u origin "$BRANCH_NAME"
   ```

## Phase 3: MR Creation

1. **Prepare MR content**
   - Title: Concise description of change
   - Description: Problem statement, solution approach, testing notes

2. **Create MR via API**
   ```bash
   MR_RESPONSE=$(curl -s -X POST \
     "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests" \
     -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     -H "Content-Type: application/json" \
     -d "{
       \"source_branch\": \"$BRANCH_NAME\",
       \"target_branch\": \"main\",
       \"title\": \"$MR_TITLE\",
       \"description\": \"$MR_DESCRIPTION\",
       \"remove_source_branch\": true
     }")
   ```

3. **Extract MR details**
   ```bash
   MR_IID=$(echo "$MR_RESPONSE" | jq -r '.iid')
   MR_URL=$(echo "$MR_RESPONSE" | jq -r '.web_url')
   ```

## Phase 4: Exponential Backoff Monitoring

### Initial Setup
```bash
INTERVAL=30          # Base interval: 30 seconds
MAX_INTERVAL=900     # Cap: 15 minutes  
MULTIPLIER=2         # 2x growth
LAST_CHECK_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
LAST_COMMENT_COUNT=0
```

### Monitoring Loop

```bash
while true; do
  # Wait for current interval
  sleep $INTERVAL
  
  # Check MR status
  MR_STATUS=$(curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID")
  
  STATE=$(echo "$MR_STATUS" | jq -r '.state')
  COMMENT_COUNT=$(echo "$MR_STATUS" | jq -r '.user_notes_count')
  UPVOTES=$(echo "$MR_STATUS" | jq -r '.upvotes')
  
  # Check for completion conditions
  if [[ "$STATE" == "merged" ]]; then
    echo "✅ MR merged successfully!"
    break
  fi
  
  if [[ "$STATE" == "closed" ]]; then
    echo "❌ MR was closed/rejected"
    break
  fi
  
  if [[ "$UPVOTES" -gt 0 ]]; then
    echo "👍 MR received approval (upvotes: $UPVOTES)"
    # Continue monitoring for merge
  fi
  
  # Check for new comments
  if [[ "$COMMENT_COUNT" -gt "$LAST_COMMENT_COUNT" ]]; then
    echo "💬 New comments detected ($COMMENT_COUNT vs $LAST_COMMENT_COUNT)"
    
    # Fetch and process new comments
    process_new_comments "$MR_IID" "$LAST_CHECK_TIME"
    
    # Reset interval to base
    INTERVAL=30
    LAST_COMMENT_COUNT=$COMMENT_COUNT
    LAST_CHECK_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  else
    # No activity - increase interval (exponential backoff)
    if [[ $INTERVAL -lt $MAX_INTERVAL ]]; then
      INTERVAL=$((INTERVAL * MULTIPLIER))
      if [[ $INTERVAL -gt $MAX_INTERVAL ]]; then
        INTERVAL=$MAX_INTERVAL
      fi
    fi
    echo "⏰ No activity, checking again in ${INTERVAL}s"
  fi
done
```

### Comment Processing

```bash
process_new_comments() {
  local mr_iid=$1
  local since_time=$2
  
  # Get comments since last check
  COMMENTS=$(curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$mr_iid/notes?sort=asc&order_by=created_at")
  
  # Filter to new comments (created after since_time)
  NEW_COMMENTS=$(echo "$COMMENTS" | jq -r "
    .[] | select(
      (.created_at > \"$since_time\") and 
      (.system == false) and
      (.author.username != \"moltbot\")
    ) | .body"
  )
  
  if [[ -n "$NEW_COMMENTS" ]]; then
    echo "Processing feedback..."
    
    # Analyze comments and determine required changes
    # Make necessary modifications
    # Commit and push updates
    # Reply to comments acknowledging changes
    
    reply_to_comments "$mr_iid" "Changes implemented based on feedback. Please review."
  fi
}
```

### Reply Function

```bash
reply_to_comments() {
  local mr_iid=$1
  local reply_text=$2
  
  curl -s -X POST \
    "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$mr_iid/notes" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"body\": \"$reply_text\"}" > /dev/null
}
```

## Phase 5: Iteration and Response

1. **Analyze feedback**
   - Parse comment content for actionable requests
   - Identify specific files/lines that need changes
   - Understand the reviewer's concerns

2. **Implement changes**
   ```bash
   # Make requested modifications
   git add -A
   git commit -m "Address feedback: specific change description"
   git push origin "$BRANCH_NAME"
   ```

3. **Acknowledge feedback**
   - Reply to comments explaining what was changed
   - Reference specific commits if helpful
   - Ask for clarification if feedback is unclear

## Error Conditions

**API failures:** Retry with exponential backoff  
**Git conflicts:** Rebase feature branch on latest main  
**Token expiry:** Alert human, cannot continue automatically  
**Network issues:** Increase interval, continue monitoring  
**Invalid feedback:** Ask for clarification in MR comments

## Success Criteria

MR is considered successfully completed when:
1. `state == "merged"` OR
2. Human explicitly confirms completion

The workflow continues until one of these conditions is met or an unrecoverable error occurs.