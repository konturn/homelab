#!/usr/bin/env bash
# scrub-transcripts.sh — Redact credentials from OpenClaw session transcripts
#
# Uses gitleaks to detect secrets across ~160 rule patterns, then redacts
# each finding in-place. Also purges archived sub-agent transcripts older
# than 7 days.
#
# Usage: scrub-transcripts.sh <transcript_directory>
#
# Prerequisites: gitleaks binary at /usr/local/bin/gitleaks,
#                config at /etc/scrub-transcripts/gitleaks-transcripts.toml

set -euo pipefail

TRANSCRIPT_DIR="${1:?Usage: scrub-transcripts.sh <transcript_directory>}"
LOG_DIR="/var/log/scrub-transcripts"
LOG_FILE="${LOG_DIR}/scrub.log"
GITLEAKS_BIN="/usr/local/bin/gitleaks"
GITLEAKS_CONFIG="/etc/scrub-transcripts/gitleaks-transcripts.toml"
REPORT_FILE="/tmp/gitleaks-report.json"

mkdir -p "$LOG_DIR"

if [[ ! -d "$TRANSCRIPT_DIR" ]]; then
  echo "ERROR: Directory does not exist: $TRANSCRIPT_DIR" >&2
  exit 1
fi

if [[ ! -x "$GITLEAKS_BIN" ]]; then
  echo "ERROR: gitleaks not found at $GITLEAKS_BIN" >&2
  exit 1
fi

# Structured logging
log_event() {
  local level="$1" event="$2" details="${3:-}"
  local ts
  ts=$(date -Iseconds)
  echo "{\"ts\":\"${ts}\",\"level\":\"${level}\",\"event\":\"${event}\",\"details\":\"${details}\"}" >> "$LOG_FILE"
  echo "[$ts] $level: $event $details" | tee /dev/stderr
}

# Rotate log if > 10MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -c%s "$LOG_FILE" 2>/dev/null || stat -f%z "$LOG_FILE" 2>/dev/null) -gt 10485760 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
  log_event "INFO" "log_rotated" "previous log archived"
fi

log_event "INFO" "scrub_started" "dir=${TRANSCRIPT_DIR}"

# --- Run gitleaks scan ---
# Skip the active (most recently modified) session file to avoid
# redacting tokens mid-conversation — they get scrubbed once the
# session rotates and is no longer the newest file.
ACTIVE_SESSION=$(find "$TRANSCRIPT_DIR" -maxdepth 1 -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)

GITLEAKS_ARGS=(dir "$TRANSCRIPT_DIR" --report-format json --report-path "$REPORT_FILE" --exit-code 1 --no-banner)

if [[ -f "$GITLEAKS_CONFIG" ]]; then
  GITLEAKS_ARGS+=(--config "$GITLEAKS_CONFIG")
fi

set +e
"$GITLEAKS_BIN" "${GITLEAKS_ARGS[@]}" 2>/dev/null
GITLEAKS_EXIT=$?
set -e

if [[ $GITLEAKS_EXIT -eq 0 ]]; then
  log_event "INFO" "no_secrets_found" "gitleaks found nothing to redact"
elif [[ $GITLEAKS_EXIT -eq 1 ]]; then
  log_event "INFO" "secrets_found" "processing gitleaks report"
else
  log_event "ERROR" "gitleaks_error" "exit_code=${GITLEAKS_EXIT}"
  exit 1
fi

# --- Redact secrets from report ---
TOTAL_FILES=0
TOTAL_REDACTED=0

if [[ -f "$REPORT_FILE" ]] && [[ -s "$REPORT_FILE" ]]; then
  if command -v jq &>/dev/null; then
    # Deduplicate by file+secret, then redact (skip active session)
    FILTER='.'
    if [[ -n "${ACTIVE_SESSION:-}" ]]; then
      FILTER="[.[] | select(.File != \"${ACTIVE_SESSION}\")]"
    fi
    jq -r "$FILTER | .[] | [.File, .Secret] | @tsv" "$REPORT_FILE" | sort -u | while IFS=$'\t' read -r filepath secret; do
      if [[ -z "$secret" ]] || [[ -z "$filepath" ]]; then
        continue
      fi

      # Escape special characters for sed
      escaped_secret=$(printf '%s\n' "$secret" | sed 's/[&/\]/\\&/g; s/[[\.*^$()+?{|]/\\&/g')
      escaped_redacted='[REDACTED]'

      if sed -i "s|${escaped_secret}|${escaped_redacted}|g" "$filepath" 2>/dev/null; then
        log_event "SCRUB" "secret_redacted" "file=$(basename "$filepath")"
        TOTAL_REDACTED=$((TOTAL_REDACTED + 1))
      fi
    done

    TOTAL_FILES=$(jq -r '[.[].File] | unique | length' "$REPORT_FILE")
  else
    log_event "WARN" "jq_unavailable" "cannot parse gitleaks report without jq"
  fi

  rm -f "$REPORT_FILE"
fi

log_event "INFO" "scrub_complete" "files=${TOTAL_FILES},secrets_redacted=${TOTAL_REDACTED}"

# --- Purge old archived sub-agent transcripts (older than 7 days) ---
PURGE_COUNT=0
while IFS= read -r -d '' old_file; do
  rm -f "$old_file"
  log_event "PURGE" "transcript_deleted" "file=$(basename "$old_file")"
  PURGE_COUNT=$((PURGE_COUNT + 1))
done < <(find "$TRANSCRIPT_DIR" -name "*.deleted.*" -mtime +7 -type f -print0 2>/dev/null)

log_event "INFO" "purge_complete" "deleted=${PURGE_COUNT}"
