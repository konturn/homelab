#!/bin/bash
# Record a successful job application
# Usage: ./record-application.sh --id <id> --company <company> --role <role> --url <url> --app-url <app-url> --salary <salary>

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
APPLIED_FILE="$SKILL_DIR/references/applied.json"
TRACKER_FILE="$SKILL_DIR/references/tracker.md"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --id) JOB_ID="$2"; shift 2 ;;
        --company) COMPANY="$2"; shift 2 ;;
        --role) ROLE="$2"; shift 2 ;;
        --url) URL="$2"; shift 2 ;;
        --app-url) APP_URL="$2"; shift 2 ;;
        --salary) SALARY="$2"; shift 2 ;;
        --notes) NOTES="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# Validate required fields
if [ -z "$JOB_ID" ] || [ -z "$COMPANY" ] || [ -z "$ROLE" ]; then
    echo "Required: --id, --company, --role" >&2
    exit 1
fi

DATE=$(TZ=America/New_York date +%Y-%m-%d)

# Update applied.json
if [ ! -f "$APPLIED_FILE" ]; then
    echo '{"jobs": []}' > "$APPLIED_FILE"
fi

# Create new entry
NEW_ENTRY=$(jq -n \
    --arg id "$JOB_ID" \
    --arg company "$COMPANY" \
    --arg role "$ROLE" \
    --arg url "${URL:-}" \
    --arg appUrl "${APP_URL:-}" \
    --arg salary "${SALARY:-}" \
    --arg applied "$DATE" \
    --arg notes "${NOTES:-}" \
    '{id: $id, company: $company, role: $role, url: $url, applicationUrl: $appUrl, salary: $salary, applied: $applied, notes: $notes}')

# Append to jobs array
jq ".jobs += [$NEW_ENTRY]" "$APPLIED_FILE" > "$APPLIED_FILE.tmp" && mv "$APPLIED_FILE.tmp" "$APPLIED_FILE"

# Update tracker.md (append to Active Applications table)
if [ -f "$TRACKER_FILE" ]; then
    # Check if company already exists in tracker
    if ! grep -q "| $COMPANY |" "$TRACKER_FILE"; then
        # Find the Active Applications table and append
        # Format: | Company | Role | Applied | Status | Notes |
        echo "| $COMPANY | $ROLE | $DATE | Applied | ${NOTES:-} |" >> "$TRACKER_FILE"
    fi
fi

echo "recorded: $COMPANY - $ROLE ($DATE)"
