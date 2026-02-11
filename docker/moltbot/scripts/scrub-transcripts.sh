#!/usr/bin/env bash
# scrub-transcripts.sh — Redact credentials from OpenClaw session transcripts
#
# Usage: scrub-transcripts.sh <transcript_directory>
#
# Scans *.jsonl (and *.deleted.*) files for credential patterns and replaces
# them with [REDACTED] in-place. Also purges archived sub-agent transcripts
# older than 7 days.
#
# Designed to run as a periodic cron job on the host where moltbot persistent
# data is mounted.

set -euo pipefail

TRANSCRIPT_DIR="${1:?Usage: scrub-transcripts.sh <transcript_directory>}"
LOG_DIR="/var/log/scrub-transcripts"
LOG_FILE="${LOG_DIR}/scrub.log"

mkdir -p "$LOG_DIR"

if [[ ! -d "$TRANSCRIPT_DIR" ]]; then
  echo "ERROR: Directory does not exist: $TRANSCRIPT_DIR" >&2
  exit 1
fi

# Structured logging function
# Writes JSON to log file AND human-readable to both stdout and stderr.
# Stderr ensures output is captured by the container's log driver / syslog
# even when the log file volume isn't monitored by Promtail.
log_event() {
  local level="$1"
  local event="$2"
  local details="${3:-}"
  local ts
  ts=$(date -Iseconds)
  local entry="{\"ts\":\"${ts}\",\"level\":\"${level}\",\"event\":\"${event}\",\"details\":\"${details}\"}"
  echo "$entry" >> "$LOG_FILE"
  echo "[$ts] $level: $event $details" | tee /dev/stderr
}

# Rotate log if > 10MB
if [[ -f "$LOG_FILE" ]] && [[ $(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE" 2>/dev/null) -gt 10485760 ]]; then
  mv "$LOG_FILE" "${LOG_FILE}.1"
  log_event "INFO" "log_rotated" "previous log archived"
fi

TOTAL_FILES=0
TOTAL_REDACTED=0

log_event "INFO" "scrub_started" "dir=${TRANSCRIPT_DIR}"

# Build a combined sed expression for all credential patterns.
# We use extended regex (-E) for readability.
# Each pattern replaces the match with [REDACTED].
build_sed_script() {
  cat <<'SEDSCRIPT'
# Vault tokens
s/hvs\.[A-Za-z0-9]+/[REDACTED]/g

# GitLab tokens
s/glpat-[A-Za-z0-9_-]+/[REDACTED]/g

# GitHub tokens
s/ghp_[A-Za-z0-9]+/[REDACTED]/g
s/gho_[A-Za-z0-9]+/[REDACTED]/g
s/ghs_[A-Za-z0-9]+/[REDACTED]/g

# Anthropic keys (must come before generic sk- to avoid partial match)
s/sk-ant-[A-Za-z0-9_-]+/[REDACTED]/g

# OpenAI keys (sk- followed by 20+ alphanum, but not sk-ant which is handled above)
s/sk-[A-Za-z0-9]{20,}/[REDACTED]/g

# Slack tokens
s/xoxb-[A-Za-z0-9-]+/[REDACTED]/g
s/xoxp-[A-Za-z0-9-]+/[REDACTED]/g

# AWS access keys
s/AKIA[A-Z0-9]{16}/[REDACTED]/g

# Telegram bot tokens (digits:AA followed by 30-40 chars)
s/[0-9]+:AA[A-Za-z0-9_-]{30,40}/[REDACTED]/g

# JWT tokens
s/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/[REDACTED]/g

# Private key blocks (literal newlines)
s/-----BEGIN[^-]*PRIVATE KEY-----.*-----END[^-]*PRIVATE KEY-----/[REDACTED]/g

# Private key blocks in JSON strings (newlines encoded as literal \\n)
s/-----BEGIN[^-]*PRIVATE KEY-----([^"]*\\\\n)*[^"]*-----END[^-]*PRIVATE KEY-----/[REDACTED]/g

# Catch any remaining JSON-escaped private key fragments (BEGIN...\\n...END pattern)
s/-----BEGIN[^-]*PRIVATE KEY-----(\\\\n[^"]*)*\\\\n-----END[^-]*PRIVATE KEY-----/[REDACTED]/g

# UUID secrets after keywords (secret_id, secret, password)
s/(secret_id|secret|password)(["':= ]+)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/\1\2[REDACTED]/gI

# Vault AppRole secret_id (UUID format in JSON: "secret_id":"<uuid>" or secret_id = <uuid>)
s/(secret_id)(\\?["'"'"']?\s*[:=]\s*\\?["'"'"']?)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/\1\2[REDACTED]/gI

# Generic long hex strings (>32 chars) after credential keywords
s/(token|password|secret|key)(["':= ]+)[0-9a-fA-F]{32,}/\1\2[REDACTED]/gI

# App password patterns (16-char alpha near app.password / GMAIL_APP_PASSWORD)
s/(app[._]password|GMAIL_APP_PASSWORD)(["':= ]+)[A-Za-z]{16}/\1\2[REDACTED]/gI
SEDSCRIPT
}

SED_SCRIPT=$(build_sed_script)

# Find all transcript files: active .jsonl and archived .deleted. files
while IFS= read -r -d '' file; do
  # Check if file contains any matches before modifying (preserve mtime)
  if grep -qE '(hvs\.[A-Za-z0-9]+|glpat-[A-Za-z0-9_-]+|gh[pos]_[A-Za-z0-9]+|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]+|xox[bp]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16}|[0-9]+:AA[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN.*PRIVATE KEY-----|((secret_id|secret|password|token|key)["'"'"':= ]+[0-9a-fA-F]{32,})|(app[._]password|GMAIL_APP_PASSWORD))' "$file" 2>/dev/null; then
    # Count lines with matches before redaction
    match_count=$(grep -cE '(hvs\.[A-Za-z0-9]+|glpat-[A-Za-z0-9_-]+|gh[pos]_[A-Za-z0-9]+|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]+|xox[bp]-[A-Za-z0-9-]+|AKIA[A-Z0-9]{16}|[0-9]+:AA[A-Za-z0-9_-]{30,}|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|-----BEGIN.*PRIVATE KEY-----|((secret_id|secret|password|token|key)["'"'"':= ]+[0-9a-fA-F]{32,})|(app[._]password|GMAIL_APP_PASSWORD))' "$file" 2>/dev/null || true)

    # Apply redactions in-place
    sed -i -E "$SED_SCRIPT" "$file"

    log_event "SCRUB" "file_redacted" "file=$(basename "$file"),lines=${match_count}"
    TOTAL_FILES=$((TOTAL_FILES + 1))
    TOTAL_REDACTED=$((TOTAL_REDACTED + match_count))
  fi
done < <(find "$TRANSCRIPT_DIR" \( -name "*.jsonl" -o -name "*.deleted.*" \) -type f -print0 2>/dev/null)

log_event "INFO" "scrub_complete" "files=${TOTAL_FILES},lines_redacted=${TOTAL_REDACTED}"

# --- Purge old archived sub-agent transcripts (older than 7 days) ---
PURGE_COUNT=0
while IFS= read -r -d '' old_file; do
  rm -f "$old_file"
  log_event "PURGE" "transcript_deleted" "file=$(basename "$old_file")"
  PURGE_COUNT=$((PURGE_COUNT + 1))
done < <(find "$TRANSCRIPT_DIR" -name "*.deleted.*" -mtime +7 -type f -print0 2>/dev/null)

log_event "INFO" "purge_complete" "deleted=${PURGE_COUNT}"
