#!/bin/bash
# Find personal correspondents in Gmail inbox
# Produces a condensed report of unique personal senders
# Usage: find-people.sh [since_date]
# Example: find-people.sh "01-Jan-2020"

set -euo pipefail

GMAIL_USER="${GMAIL_EMAIL:?GMAIL_EMAIL not set}"
GMAIL_PASS="${GMAIL_APP_PASSWORD:?GMAIL_APP_PASSWORD not set}"
IMAP_URL="imaps://imap.gmail.com:993"
SINCE="${1:-01-Jan-2020}"
OUTPUT="/home/node/.openclaw/workspace/memory/email-people-report.md"

curl_imap() {
  curl -s --max-time 30 --url "$1" --user "$GMAIL_USER:$GMAIL_PASS" "${@:2}" 2>/dev/null
}

echo "# Email Correspondents Report" > "$OUTPUT"
echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUTPUT"
echo "Search period: since $SINCE" >> "$OUTPUT"
echo "" >> "$OUTPUT"

# ============================================
# Phase 1: Search SENT mail for recipients
# (People Noah writes to = most important)
# ============================================
echo "## Sent Mail Analysis" >> "$OUTPUT"
echo "" >> "$OUTPUT"

# Get sent mail message IDs
echo "[*] Searching sent mail..." >&2
SENT_IDS=$(curl_imap "$IMAP_URL/%5BGmail%5D/Sent%20Mail" -X "SEARCH SINCE $SINCE" 2>/dev/null | sed 's/\* SEARCH //' | tr ' ' '\n' | grep -E '^[0-9]+$' || true | tail -200)

if [ -z "$SENT_IDS" ]; then
  echo "(No sent messages found or access denied)" >> "$OUTPUT"
  echo "[!] Could not access sent mail" >&2
else
  SENT_COUNT=$(echo "$SENT_IDS" | wc -l)
  echo "Found $SENT_COUNT sent messages (sampling last 200)" >> "$OUTPUT"
  echo "" >> "$OUTPUT"
  
  # Sample ~50 evenly spaced from the sent messages
  SAMPLE=$(echo "$SENT_IDS" | awk 'NR % 4 == 0' | head -50)
  
  declare -A SENT_TO
  for id in $SAMPLE; do
    TO_LINE=$(curl_imap "$IMAP_URL/%5BGmail%5D/Sent%20Mail;MAILINDEX=$id;SECTION=HEADER.FIELDS%20(TO%20CC%20SUBJECT%20DATE)" 2>/dev/null || echo "")
    if [ -n "$TO_LINE" ]; then
      echo "$TO_LINE" >> /tmp/sent_headers_raw.txt
      echo "---" >> /tmp/sent_headers_raw.txt
    fi
    echo -n "." >&2
  done
  echo "" >&2
  
  # Extract unique To addresses
  if [ -f /tmp/sent_headers_raw.txt ]; then
    echo "### Sent Mail Recipients (sampled)" >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    cat /tmp/sent_headers_raw.txt >> "$OUTPUT"
    echo '```' >> "$OUTPUT"
    echo "" >> "$OUTPUT"
    rm -f /tmp/sent_headers_raw.txt
  fi
fi

# ============================================
# Phase 2: Search for personal domain senders
# ============================================
echo "## Personal Email Senders" >> "$OUTPUT"
echo "" >> "$OUTPUT"

# Search for personal email domains (not corporate/automated)
PERSONAL_DOMAINS=("gmail.com" "yahoo.com" "hotmail.com" "outlook.com" "icloud.com" "aol.com" "protonmail.com" "hey.com" "me.com" "live.com")

for domain in "${PERSONAL_DOMAINS[@]}"; do
  echo "[*] Searching FROM $domain since $SINCE..." >&2
  IDS=$(curl_imap "$IMAP_URL/INBOX" -X "SEARCH FROM $domain SINCE $SINCE" 2>/dev/null | sed 's/\* SEARCH //' | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)
  
  if [ -z "$IDS" ]; then
    continue
  fi
  
  COUNT=$(echo "$IDS" | wc -l)
  echo "### $domain ($COUNT messages)" >> "$OUTPUT"
  
  # Sample up to 30 evenly spread
  if [ "$COUNT" -gt 30 ]; then
    STEP=$(( COUNT / 30 ))
    SAMPLE=$(echo "$IDS" | awk "NR % $STEP == 0" | head -30)
  else
    SAMPLE="$IDS"
  fi
  
  echo '```' >> "$OUTPUT"
  for id in $SAMPLE; do
    HEADER=$(curl_imap "$IMAP_URL/INBOX;MAILINDEX=$id;SECTION=HEADER.FIELDS%20(FROM%20SUBJECT%20DATE)" 2>/dev/null || echo "(failed)")
    echo "$HEADER" >> "$OUTPUT"
    echo "---" >> "$OUTPUT"
    echo -n "." >&2
  done
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
  echo "" >&2
done

# ============================================
# Phase 3: Search for specific known contacts
# ============================================
echo "## Known Contact Searches" >> "$OUTPUT"
echo "" >> "$OUTPUT"

KNOWN_NAMES=("patty" "chris" "mom" "dad" "kontur")

for name in "${KNOWN_NAMES[@]}"; do
  echo "[*] Searching FROM $name..." >&2
  IDS=$(curl_imap "$IMAP_URL/INBOX" -X "SEARCH FROM $name SINCE 01-Jan-2018" 2>/dev/null | sed 's/\* SEARCH //' | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)
  
  if [ -z "$IDS" ]; then
    continue
  fi
  
  COUNT=$(echo "$IDS" | wc -l)
  echo "### FROM '$name' ($COUNT messages)" >> "$OUTPUT"
  
  # Get last 10 headers
  LAST_10=$(echo "$IDS" | tail -10)
  
  echo '```' >> "$OUTPUT"
  for id in $LAST_10; do
    HEADER=$(curl_imap "$IMAP_URL/INBOX;MAILINDEX=$id;SECTION=HEADER.FIELDS%20(FROM%20TO%20SUBJECT%20DATE)" 2>/dev/null || echo "(failed)")
    echo "$HEADER" >> "$OUTPUT"
    echo "---" >> "$OUTPUT"
    echo -n "." >&2
  done
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
  echo "" >&2
done

# ============================================
# Phase 4: University/work personal contacts
# ============================================
echo "## University & Work Contacts" >> "$OUTPUT"
echo "" >> "$OUTPUT"

WORK_DOMAINS=("wustl.edu" "nasa.gov" "nvidia.com")

for domain in "${WORK_DOMAINS[@]}"; do
  echo "[*] Searching FROM $domain..." >&2
  IDS=$(curl_imap "$IMAP_URL/INBOX" -X "SEARCH FROM $domain" 2>/dev/null | sed 's/\* SEARCH //' | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true)
  
  if [ -z "$IDS" ]; then
    echo "### $domain: (no messages found)" >> "$OUTPUT"
    continue
  fi
  
  COUNT=$(echo "$IDS" | wc -l)
  echo "### $domain ($COUNT messages)" >> "$OUTPUT"
  
  LAST_10=$(echo "$IDS" | tail -10)
  
  echo '```' >> "$OUTPUT"
  for id in $LAST_10; do
    HEADER=$(curl_imap "$IMAP_URL/INBOX;MAILINDEX=$id;SECTION=HEADER.FIELDS%20(FROM%20TO%20SUBJECT%20DATE)" 2>/dev/null || echo "(failed)")
    echo "$HEADER" >> "$OUTPUT"
    echo "---" >> "$OUTPUT"
    echo -n "." >&2
  done
  echo '```' >> "$OUTPUT"
  echo "" >> "$OUTPUT"
  echo "" >&2
done

echo "" >> "$OUTPUT"
echo "---" >> "$OUTPUT"
echo "*End of report*" >> "$OUTPUT"

echo "" >&2
echo "[✓] Report written to $OUTPUT" >&2
wc -l "$OUTPUT" >&2
