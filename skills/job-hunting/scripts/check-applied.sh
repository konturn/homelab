#!/bin/bash
# Check if a job has already been applied to
# Usage: ./check-applied.sh <job-id>
# Exit 0 if NOT applied (safe to proceed), exit 1 if already applied

JOB_ID="$1"
APPLIED_FILE="/home/node/clawd/skills/job-hunting/references/applied.json"
BLOCKLIST_FILE="/home/node/clawd/skills/job-hunting/references/blocklist.md"

if [ -z "$JOB_ID" ]; then
    echo "usage: check-applied.sh <job-id>" >&2
    exit 1
fi

# Check applied.json
if [ -f "$APPLIED_FILE" ]; then
    if jq -e ".[] | select(.id == \"$JOB_ID\")" "$APPLIED_FILE" > /dev/null 2>&1; then
        echo "already-applied"
        exit 1
    fi
fi

echo "not-applied"
exit 0
