#!/bin/bash
# mr-comment-check.sh - Fast check for new MR comments
# Returns JSON: {"dispatch": [...], "cleanup": [...], "skipped": [...]}

TRACKING_FILE="${TRACKING_FILE:-/home/node/.openclaw/workspace/memory/open-mrs.json}"
GITLAB_API="https://gitlab.lab.nkontur.com/api/v4"
PROJECT_ID="4"
LOCK_EXPIRY_MS=3600000  # 1 hour

[[ ! -f "$TRACKING_FILE" ]] && echo '{"dispatch":[],"cleanup":[],"skipped":[],"error":"no tracking file"}' && exit 0

now_ms=$(($(date +%s) * 1000))

# Fetch all open MRs in one call (sanitize control chars)
all_mrs=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/merge_requests?state=opened&per_page=100" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" | tr -d '\000-\011\013-\037')

dispatch="[]"
cleanup="[]"
skipped="[]"

for iid in $(jq -r 'keys[]' "$TRACKING_FILE"); do
  tracking=$(jq -r --arg i "$iid" '.[$i]' "$TRACKING_FILE")
  last_cid=$(echo "$tracking" | jq -r '.lastCommentId // 0')
  locked_by=$(echo "$tracking" | jq -r '.lockedBy // empty')
  locked_at=$(echo "$tracking" | jq -r '.lockedAt // 0')
  
  # Check lock
  if [[ -n "$locked_by" && "$locked_at" != "0" && "$locked_at" != "null" ]]; then
    if [[ "$locked_at" =~ ^[0-9]+$ ]]; then
      lock_age=$((now_ms - locked_at))
      [[ $lock_age -lt $LOCK_EXPIRY_MS ]] && skipped=$(echo "$skipped" | jq --arg i "$iid" --arg r "locked" '. + [{"iid":($i|tonumber),"reason":$r}]') && continue
    fi
  fi
  
  # Check if MR exists in open MRs
  mr=$(echo "$all_mrs" | jq --arg i "$iid" '.[] | select(.iid == ($i|tonumber))')
  if [[ -z "$mr" || "$mr" == "null" ]]; then
    cleanup=$(echo "$cleanup" | jq --arg i "$iid" '. + [{"iid":($i|tonumber),"state":"closed_or_merged"}]')
    continue
  fi
  
  # Check pipeline
  pipeline=$(echo "$mr" | jq -r '.head_pipeline.status // "none"')
  if [[ "$pipeline" == "running" || "$pipeline" == "pending" ]]; then
    skipped=$(echo "$skipped" | jq --arg i "$iid" --arg r "pipeline $pipeline" '. + [{"iid":($i|tonumber),"reason":$r}]')
    continue
  fi
  
  # Fetch discussions for this MR (only one API call per MR that passes filters)
  notes=$(curl -s "${GITLAB_API}/projects/${PROJECT_ID}/merge_requests/${iid}/notes?per_page=100" \
    -H "PRIVATE-TOKEN: $GITLAB_TOKEN")
  
  # Find new comments from root
  new_comment=$(echo "$notes" | jq -r --argjson last "$last_cid" '
    [.[] | select(.author.username == "root" and .id > $last and .system == false)]
    | sort_by(.id) | last // empty
  ')
  
  if [[ -n "$new_comment" && "$new_comment" != "null" ]]; then
    cid=$(echo "$new_comment" | jq -r '.id')
    dispatch=$(echo "$dispatch" | jq --arg i "$iid" --argjson c "$cid" '. + [{"iid":($i|tonumber),"commentId":$c}]')
  fi
done

echo "{\"dispatch\":$dispatch,\"cleanup\":$cleanup,\"skipped\":$skipped}"
