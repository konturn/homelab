#!/bin/bash
# Solve hCaptcha/reCAPTCHA using 2captcha service
# Usage: solve-captcha.sh <type> <sitekey> <page_url>
#   type: hcaptcha | recaptcha
#   sitekey: the captcha site key from the page
#   page_url: the URL where the captcha appears
# Returns: solution token on stdout
# Exit codes: 0=success, 1=failure

set -euo pipefail

TYPE="${1:-}"
SITEKEY="${2:-}"
PAGE_URL="${3:-}"

if [ -z "$TYPE" ] || [ -z "$SITEKEY" ] || [ -z "$PAGE_URL" ]; then
  echo "Usage: solve-captcha.sh <hcaptcha|recaptcha> <sitekey> <page_url>" >&2
  exit 1
fi

# Get API key from Vault
source /home/node/.openclaw/workspace/tools/jit-lib.sh 2>/dev/null
API_KEY=$(vault_read homelab/data/agents/2captcha | jq -r '.api_key')

if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
  echo "ERROR: Could not read 2captcha API key from Vault" >&2
  exit 1
fi

# Map type to 2captcha method
case "$TYPE" in
  hcaptcha)
    METHOD="hcaptcha"
    SUBMIT_DATA="key=${API_KEY}&method=${METHOD}&sitekey=${SITEKEY}&pageurl=${PAGE_URL}&json=1"
    ;;
  recaptcha)
    METHOD="userrecaptcha"
    SUBMIT_DATA="key=${API_KEY}&method=${METHOD}&googlekey=${SITEKEY}&pageurl=${PAGE_URL}&json=1"
    ;;
  *)
    echo "ERROR: Unknown captcha type '$TYPE'. Use 'hcaptcha' or 'recaptcha'" >&2
    exit 1
    ;;
esac

# Submit captcha
RESP=$(curl -s "https://2captcha.com/in.php?${SUBMIT_DATA}")
STATUS=$(echo "$RESP" | jq -r '.status // 0')
REQUEST_ID=$(echo "$RESP" | jq -r '.request // empty')

if [ "$STATUS" != "1" ] || [ -z "$REQUEST_ID" ]; then
  echo "ERROR: Failed to submit captcha: $RESP" >&2
  exit 1
fi

echo "Submitted captcha, request ID: $REQUEST_ID" >&2

# Poll for solution (max 120s)
ELAPSED=0
POLL_INTERVAL=5
MAX_WAIT=120

while [ $ELAPSED -lt $MAX_WAIT ]; do
  sleep $POLL_INTERVAL
  ELAPSED=$((ELAPSED + POLL_INTERVAL))

  RESP=$(curl -s "https://2captcha.com/res.php?key=${API_KEY}&action=get&id=${REQUEST_ID}&json=1")
  STATUS=$(echo "$RESP" | jq -r '.status // 0')
  REQUEST=$(echo "$RESP" | jq -r '.request // empty')

  if [ "$STATUS" = "1" ]; then
    echo "Solved in ${ELAPSED}s" >&2
    echo "$REQUEST"
    exit 0
  elif [ "$REQUEST" = "CAPCHA_NOT_READY" ]; then
    echo "Waiting... (${ELAPSED}s)" >&2
    continue
  else
    echo "ERROR: 2captcha returned: $RESP" >&2
    exit 1
  fi
done

echo "ERROR: Timed out after ${MAX_WAIT}s" >&2
exit 1
