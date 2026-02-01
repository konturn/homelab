#!/bin/bash
# GitLab Shared Library for Homelab MR Operations
# Source this in skills: source /home/node/clawd/skills/gitlab/lib.sh
#
# Environment required:
#   GITLAB_TOKEN - GitLab API token
#   GITLAB_HOST  - GitLab host (default: gitlab.lab.nkontur.com)
#   PROJECT_ID   - GitLab project ID (default: 4)

GITLAB_HOST="${GITLAB_HOST:-gitlab.lab.nkontur.com}"
PROJECT_ID="${PROJECT_ID:-4}"
GITLAB_API="https://${GITLAB_HOST}/api/v4"

# ============================================================================
# get_failed_job_logs $PIPELINE_ID
# Fetch and output logs from all failed jobs in a pipeline
# Returns: 0 always (informational)
# ============================================================================
get_failed_job_logs() {
  local pipeline_id="$1"
  
  if [ -z "$pipeline_id" ]; then
    echo "ERROR: get_failed_job_logs requires pipeline_id" >&2
    return 1
  fi
  
  local failed_jobs
  failed_jobs=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/pipelines/${pipeline_id}/jobs" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
  
  local job_ids
  job_ids=$(echo "$failed_jobs" | jq -r '.[] | select(.status == "failed") | .id')
  
  if [ -z "$job_ids" ]; then
    echo "No failed jobs found in pipeline #${pipeline_id}"
    return 0
  fi
  
  for job_id in $job_ids; do
    local job_name
    job_name=$(echo "$failed_jobs" | jq -r ".[] | select(.id == $job_id) | .name")
    echo ""
    echo "══════════════════════════════════════════════════════════════════"
    echo "FAILED JOB: $job_name (ID: $job_id)"
    echo "══════════════════════════════════════════════════════════════════"
    curl -s "${GITLAB_API}/projects/${PROJECT_ID}/jobs/${job_id}/trace" \
      -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | tail -100
    echo ""
  done
  
  return 0
}

# ============================================================================
# wait_for_pipeline $MR_IID
# Poll pipeline status until success/failure. Output job logs on failure.
# Max 60 attempts (~10 min) with exponential backoff.
# Returns: 0 on success, 1 on failure/timeout
# ============================================================================
wait_for_pipeline() {
  local mr_iid="$1"
  
  if [ -z "$mr_iid" ]; then
    echo "ERROR: wait_for_pipeline requires MR IID" >&2
    return 1
  fi
  
  echo "⏳ Waiting for pipeline on MR !${mr_iid}..."
  
  local max_attempts=60
  local attempt=0
  local base_delay=5
  local max_delay=30
  local stuck_start=""
  
  while [ $attempt -lt $max_attempts ]; do
    local pipeline_json
    pipeline_json=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/merge_requests/${mr_iid}/pipelines" \
      -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | jq -r '.[0] // empty')
    
    if [ -z "$pipeline_json" ]; then
      echo "  No pipeline yet (attempt $((attempt + 1))/${max_attempts})"
      sleep $base_delay
      attempt=$((attempt + 1))
      continue
    fi
    
    local status
    local pipeline_id
    status=$(echo "$pipeline_json" | jq -r '.status')
    pipeline_id=$(echo "$pipeline_json" | jq -r '.id')
    
    case "$status" in
      "success")
        echo "✅ Pipeline #${pipeline_id} passed"
        return 0
        ;;
      "failed"|"canceled")
        echo "❌ Pipeline #${pipeline_id} ${status}"
        get_failed_job_logs "$pipeline_id"
        return 1
        ;;
      "running"|"pending"|"created")
        # Check for stuck condition (>5 min same status)
        if [ -z "$stuck_start" ]; then
          stuck_start=$(date +%s)
        else
          local elapsed=$(($(date +%s) - stuck_start))
          if [ $elapsed -gt 300 ]; then
            echo "⚠️  Pipeline stuck for >5 minutes"
            return 1
          fi
        fi
        
        # Exponential backoff with cap
        local delay=$((base_delay * (1 + attempt / 10)))
        [ $delay -gt $max_delay ] && delay=$max_delay
        
        echo "  Pipeline ${status} #${pipeline_id} (attempt $((attempt + 1))/${max_attempts}, next check in ${delay}s)"
        sleep $delay
        attempt=$((attempt + 1))
        ;;
      *)
        echo "  Unknown status: ${status} (attempt $((attempt + 1))/${max_attempts})"
        sleep $base_delay
        attempt=$((attempt + 1))
        ;;
    esac
  done
  
  echo "❌ Pipeline timed out after ${max_attempts} attempts"
  return 1
}

# ============================================================================
# check_merge_conflicts $BRANCH
# Check if branch has merge conflicts with main
# Returns: 0 if clean (no conflicts), 1 if conflicts exist
# ============================================================================
check_merge_conflicts() {
  local branch="$1"
  
  if [ -z "$branch" ]; then
    echo "ERROR: check_merge_conflicts requires branch name" >&2
    return 1
  fi
  
  # Fetch latest main
  git fetch origin main 2>/dev/null
  
  # Try merge --no-commit to test for conflicts
  if git merge-tree $(git merge-base origin/main HEAD) origin/main HEAD | grep -q "^<<<<<<<"; then
    echo "⚠️  Merge conflicts detected between ${branch} and main"
    return 1
  fi
  
  # Alternative: use git merge --no-commit --no-ff then abort
  if ! git merge --no-commit --no-ff origin/main >/dev/null 2>&1; then
    git merge --abort 2>/dev/null
    echo "⚠️  Merge conflicts detected between ${branch} and main"
    return 1
  fi
  git merge --abort 2>/dev/null
  
  echo "✅ No merge conflicts with main"
  return 0
}

# ============================================================================
# push_and_wait $BRANCH $MR_IID
# Git push and wait for pipeline to complete
# Returns: 0 on success, 1 on push failure or pipeline failure
# ============================================================================
push_and_wait() {
  local branch="$1"
  local mr_iid="$2"
  
  if [ -z "$branch" ] || [ -z "$mr_iid" ]; then
    echo "ERROR: push_and_wait requires branch and MR IID" >&2
    return 1
  fi
  
  echo "📤 Pushing to ${branch}..."
  if ! git push origin "$branch"; then
    echo "❌ Git push failed"
    return 1
  fi
  echo "✅ Push successful"
  
  # Small delay for GitLab to register the push and create pipeline
  sleep 3
  
  wait_for_pipeline "$mr_iid"
  return $?
}

# ============================================================================
# escalate $MESSAGE
# Send Telegram notification to Noah and exit with error
# This function does NOT return - it exits the script
# ============================================================================
escalate() {
  local message="$1"
  
  if [ -z "$message" ]; then
    message="GitLab operation requires attention"
  fi
  
  echo ""
  echo "🚨 ESCALATING: ${message}"
  echo ""
  
  # Use curl to send via Telegram Bot API directly
  # The TELEGRAM_BOT_TOKEN should be available, or we use moltbot CLI
  local chat_id="8531859108"  # Noah's Telegram chat ID
  
  # Try moltbot message command if available
  if command -v moltbot &> /dev/null; then
    moltbot message send --channel telegram --to "$chat_id" --message "🚨 GitLab Escalation

${message}

Action required - automated process stopped."
  else
    # Fallback: write to a file for the agent to pick up
    echo "ESCALATION: ${message}" >> /tmp/gitlab-escalation.log
    echo "⚠️  Could not send Telegram notification directly"
    echo "⚠️  Escalation logged to /tmp/gitlab-escalation.log"
  fi
  
  exit 1
}

# ============================================================================
# gitlab_api_call $METHOD $ENDPOINT [$DATA]
# Helper for making GitLab API calls with retry on 409
# Returns: HTTP response code, response body in /tmp/gitlab_response.json
# ============================================================================
gitlab_api_call() {
  local method="$1"
  local endpoint="$2"
  local data="$3"
  
  local url="${GITLAB_API}${endpoint}"
  local max_retries=3
  
  for i in $(seq 1 $max_retries); do
    local http_code
    if [ -n "$data" ]; then
      http_code=$(curl -s -w "%{http_code}" -o /tmp/gitlab_response.json \
        -X "$method" "$url" \
        -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$data")
    else
      http_code=$(curl -s -w "%{http_code}" -o /tmp/gitlab_response.json \
        -X "$method" "$url" \
        -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
    fi
    
    if [ "$http_code" = "409" ]; then
      echo "  Resource locked (409), retry $i/$max_retries..." >&2
      sleep $i
      continue
    fi
    
    echo "$http_code"
    return 0
  done
  
  echo "$http_code"
  return 1
}

# ============================================================================
# preflight_check
# Validate environment before starting GitLab operations
# Returns: 0 if all checks pass, 1 otherwise
# ============================================================================
preflight_check() {
  echo "=== Pre-Flight Validation ==="
  
  # 1. Check GITLAB_TOKEN
  if [ -z "$GITLAB_TOKEN" ]; then
    echo "❌ GITLAB_TOKEN not set"
    return 1
  fi
  echo "✅ GITLAB_TOKEN present"
  
  # 2. Test API access
  local api_code
  api_code=$(curl -s -w "%{http_code}" -o /tmp/api_test.json \
    "${GITLAB_API}/projects/${PROJECT_ID}" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
  
  if [ "$api_code" != "200" ]; then
    echo "❌ API access failed (HTTP $api_code)"
    cat /tmp/api_test.json
    return 1
  fi
  echo "✅ API access confirmed"
  
  # 3. Verify project
  local project_name
  project_name=$(jq -r '.path_with_namespace' /tmp/api_test.json)
  if [ "$project_name" != "root/homelab" ]; then
    echo "❌ Wrong project: $project_name (expected root/homelab)"
    return 1
  fi
  echo "✅ Project: $project_name"
  
  # 4. Check main branch
  local main_code
  main_code=$(curl -s -w "%{http_code}" -o /dev/null \
    "${GITLAB_API}/projects/${PROJECT_ID}/repository/branches/main" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
  
  if [ "$main_code" != "200" ]; then
    echo "❌ Cannot access main branch"
    return 1
  fi
  echo "✅ Main branch accessible"
  
  echo "=== Pre-Flight Complete ==="
  return 0
}

# Export functions for subshells
export -f get_failed_job_logs
export -f wait_for_pipeline
export -f check_merge_conflicts
export -f push_and_wait
export -f escalate
export -f gitlab_api_call
export -f preflight_check
