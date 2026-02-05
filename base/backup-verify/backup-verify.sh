#!/bin/bash
# backup-verify.sh — Verify restic backup freshness and integrity
# Sends notification on failure via email (MAILTO) and pushes timestamp to InfluxDB.
#
# Required environment variables:
#   RESTIC_REPOSITORY     — restic repo URL (e.g., s3:s3.us-east-005.backblazeb2.com/nkontur-homelab)
#   RESTIC_PASSWORD       — restic repository password
#   AWS_ACCESS_KEY_ID     — S3/B2 access key
#   AWS_SECRET_ACCESS_KEY — S3/B2 secret key
#
# Optional:
#   INFLUXDB_URL          — InfluxDB write endpoint (default: http://localhost:8086)
#   INFLUXDB_TOKEN        — InfluxDB auth token
#   INFLUXDB_ORG          — InfluxDB org (default: homelab)
#   INFLUXDB_BUCKET       — InfluxDB bucket (default: metrics)
#   NOTIFICATION_EMAIL    — Email for failure alerts (default: noah@nkontur.com)
#   MAX_BACKUP_AGE_HOURS  — Max acceptable age of latest snapshot (default: 26)

set -euo pipefail

INFLUXDB_URL="${INFLUXDB_URL:-http://localhost:8086}"
INFLUXDB_ORG="${INFLUXDB_ORG:-homelab}"
INFLUXDB_BUCKET="${INFLUXDB_BUCKET:-metrics}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:-noah@nkontur.com}"
MAX_BACKUP_AGE_HOURS="${MAX_BACKUP_AGE_HOURS:-26}"
LOG_FILE="/var/log/backup-verify.log"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG_FILE"
}

send_notification() {
  local subject="$1"
  local body="$2"

  # Try email notification
  if command -v mail &>/dev/null; then
    echo "$body" | mail -s "$subject" "$NOTIFICATION_EMAIL"
    log "Email notification sent to $NOTIFICATION_EMAIL"
  fi

  # Log the failure prominently
  log "ALERT: $subject"
  log "$body"
}

push_metric() {
  local measurement="$1"
  local fields="$2"
  local tags="${3:-}"

  if [ -n "${INFLUXDB_TOKEN:-}" ]; then
    local tag_str=""
    [ -n "$tags" ] && tag_str=",$tags"

    curl -s --max-time 10 -X POST \
      "${INFLUXDB_URL}/api/v2/write?org=${INFLUXDB_ORG}&bucket=${INFLUXDB_BUCKET}&precision=s" \
      -H "Authorization: Token ${INFLUXDB_TOKEN}" \
      -H "Content-Type: text/plain" \
      --data-binary "${measurement}${tag_str} ${fields} $(date +%s)" \
      && log "Metric pushed: ${measurement}" \
      || log "WARNING: Failed to push metric to InfluxDB"
  else
    log "INFLUXDB_TOKEN not set, skipping metric push"
  fi
}

# Step 1: Check restic repository integrity
log "=== Starting backup verification ==="

VERIFY_STATUS="ok"
VERIFY_ERRORS=""

log "Running restic check..."
if ! CHECK_OUTPUT=$(restic check 2>&1); then
  VERIFY_STATUS="error"
  VERIFY_ERRORS="Repository integrity check failed:\n${CHECK_OUTPUT}"
  log "ERROR: restic check failed"
  log "$CHECK_OUTPUT"
else
  log "restic check passed"
fi

# Step 2: Check latest snapshot age
log "Checking latest snapshot age..."
LATEST_SNAPSHOT=$(restic snapshots --json --latest 1 2>/dev/null)

if [ -z "$LATEST_SNAPSHOT" ] || [ "$LATEST_SNAPSHOT" = "[]" ] || [ "$LATEST_SNAPSHOT" = "null" ]; then
  VERIFY_STATUS="error"
  VERIFY_ERRORS="${VERIFY_ERRORS}\nNo snapshots found in repository"
  log "ERROR: No snapshots found"
else
  # Parse the snapshot time
  SNAPSHOT_TIME=$(echo "$LATEST_SNAPSHOT" | jq -r '.[0].time' 2>/dev/null)

  if [ -n "$SNAPSHOT_TIME" ] && [ "$SNAPSHOT_TIME" != "null" ]; then
    SNAPSHOT_EPOCH=$(date -d "$SNAPSHOT_TIME" +%s 2>/dev/null || echo "0")
    NOW_EPOCH=$(date +%s)
    AGE_HOURS=$(( (NOW_EPOCH - SNAPSHOT_EPOCH) / 3600 ))

    log "Latest snapshot: ${SNAPSHOT_TIME} (${AGE_HOURS}h ago)"

    if [ "$AGE_HOURS" -gt "$MAX_BACKUP_AGE_HOURS" ]; then
      VERIFY_STATUS="stale"
      VERIFY_ERRORS="${VERIFY_ERRORS}\nLatest backup is ${AGE_HOURS}h old (max: ${MAX_BACKUP_AGE_HOURS}h)"
      log "WARNING: Backup is stale (${AGE_HOURS}h > ${MAX_BACKUP_AGE_HOURS}h)"
    fi

    push_metric "backup_verify" "snapshot_age_hours=${AGE_HOURS}i,status_ok=$([ "$VERIFY_STATUS" = "ok" ] && echo 1 || echo 0)i" "host=router"
  else
    VERIFY_STATUS="error"
    VERIFY_ERRORS="${VERIFY_ERRORS}\nCould not parse snapshot time"
    log "ERROR: Could not parse snapshot time"
  fi
fi

# Step 3: Count total snapshots for metrics
SNAPSHOT_COUNT=$(restic snapshots --json 2>/dev/null | jq 'length' 2>/dev/null || echo "0")
push_metric "backup_stats" "snapshot_count=${SNAPSHOT_COUNT}i" "host=router"

# Step 4: Push verification timestamp (success or failure)
push_metric "backup_verify_run" "status=\"${VERIFY_STATUS}\",timestamp=$(date +%s)i" "host=router"

# Step 5: Send notification on failure
if [ "$VERIFY_STATUS" != "ok" ]; then
  send_notification \
    "[HOMELAB] Backup verification FAILED — ${VERIFY_STATUS}" \
    "Backup verification detected issues on $(hostname) at $(date):\n\n${VERIFY_ERRORS}\n\nRepository: ${RESTIC_REPOSITORY}\n\nAction required: Check restic backup configuration and cron jobs."
  log "=== Backup verification FAILED (${VERIFY_STATUS}) ==="
  exit 1
else
  log "=== Backup verification PASSED ==="
  exit 0
fi
