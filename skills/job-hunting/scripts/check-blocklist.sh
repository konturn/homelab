#!/bin/bash
# Check if a company is on the blocklist
# Usage: ./check-blocklist.sh <company-name>
# Exit 0 if NOT blocked (safe to proceed), exit 1 if blocked

COMPANY="$1"
BLOCKLIST_FILE="/home/node/clawd/skills/job-hunting/references/blocklist.md"

if [ -z "$COMPANY" ]; then
    echo "usage: check-blocklist.sh <company-name>" >&2
    exit 1
fi

if [ ! -f "$BLOCKLIST_FILE" ]; then
    echo "not-blocked"
    exit 0
fi

# Case-insensitive search for company name in blocklist
if grep -qi "$COMPANY" "$BLOCKLIST_FILE"; then
    echo "blocked"
    exit 1
fi

echo "not-blocked"
exit 0
