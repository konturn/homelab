#!/bin/bash
# Solve hCaptcha/reCAPTCHA/Turnstile using Capsolver (primary) or 2captcha (fallback)
# Usage: solve-captcha.sh <type> <sitekey> <page_url>
#   type: hcaptcha | recaptcha | turnstile
#   sitekey: the captcha site key from the page
#   page_url: the URL where the captcha appears
# Returns: solution token on stdout
# Exit codes: 0=success, 1=failure

set -euo pipefail

TYPE="${1:-}"
SITEKEY="${2:-}"
PAGE_URL="${3:-}"

if [ -z "$TYPE" ] || [ -z "$SITEKEY" ] || [ -z "$PAGE_URL" ]; then
  echo "Usage: solve-captcha.sh <hcaptcha|recaptcha|turnstile> <sitekey> <page_url>" >&2
  exit 1
fi

source /home/node/.openclaw/workspace/tools/jit-lib.sh 2>/dev/null

# --- Capsolver (primary) ---
capsolver_solve() {
  local API_KEY
  API_KEY=$(vault_read homelab/data/agents/capsolver 2>/dev/null | jq -r '.api_key // empty' 2>/dev/null)
  # Fallback: env var or local config
  API_KEY="${API_KEY:-${CAPSOLVER_API_KEY:-}}"
  if [ -z "$API_KEY" ] && [ -f "/home/node/.openclaw/workspace/.keys/capsolver" ]; then
    API_KEY=$(cat /home/node/.openclaw/workspace/.keys/capsolver)
  fi
  if [ -z "$API_KEY" ]; then
    echo "WARN: No Capsolver API key found (Vault, env, or .keys/)" >&2
    return 1
  fi

  local TASK_TYPE
  case "$TYPE" in
    hcaptcha)    TASK_TYPE="HCaptchaTaskProxyless" ;;
    recaptcha)   TASK_TYPE="ReCaptchaV2TaskProxyLess" ;;
    turnstile)   TASK_TYPE="AntiTurnstileTaskProxyless" ;;
    *)
      echo "ERROR: Unknown captcha type '$TYPE'" >&2
      return 1
      ;;
  esac

  # Create task
  local RESP
  RESP=$(curl -s -X POST "https://api.capsolver.com/createTask" \
    -H "Content-Type: application/json" \
    -d "{\"clientKey\":\"${API_KEY}\",\"task\":{\"type\":\"${TASK_TYPE}\",\"websiteURL\":\"${PAGE_URL}\",\"websiteKey\":\"${SITEKEY}\"}}")

  local ERROR_ID
  ERROR_ID=$(echo "$RESP" | jq -r '.errorId // 0')
  if [ "$ERROR_ID" != "0" ]; then
    echo "WARN: Capsolver createTask failed: $(echo "$RESP" | jq -r '.errorDescription // "unknown"')" >&2
    return 1
  fi

  local TASK_ID
  TASK_ID=$(echo "$RESP" | jq -r '.taskId // empty')
  if [ -z "$TASK_ID" ]; then
    echo "WARN: Capsolver returned no taskId" >&2
    return 1
  fi

  echo "Capsolver task created: $TASK_ID" >&2

  # Poll for result (max 120s)
  local ELAPSED=0
  local POLL_INTERVAL=3
  local MAX_WAIT=120

  while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    RESP=$(curl -s -X POST "https://api.capsolver.com/getTaskResult" \
      -H "Content-Type: application/json" \
      -d "{\"clientKey\":\"${API_KEY}\",\"taskId\":\"${TASK_ID}\"}")

    local STATUS
    STATUS=$(echo "$RESP" | jq -r '.status // "unknown"')

    if [ "$STATUS" = "ready" ]; then
      local TOKEN
      TOKEN=$(echo "$RESP" | jq -r '.solution.gRecaptchaResponse // .solution.token // empty')
      if [ -n "$TOKEN" ]; then
        echo "Solved in ${ELAPSED}s via Capsolver" >&2
        echo "$TOKEN"
        return 0
      fi
    elif [ "$STATUS" = "processing" ]; then
      echo "Waiting... (${ELAPSED}s)" >&2
      continue
    else
      echo "WARN: Capsolver status=$STATUS: $(echo "$RESP" | jq -r '.errorDescription // "unknown"')" >&2
      return 1
    fi
  done

  echo "WARN: Capsolver timed out after ${MAX_WAIT}s" >&2
  return 1
}

# --- 2captcha (fallback) ---
twocaptcha_solve() {
  local API_KEY
  API_KEY=$(vault_read homelab/data/agents/2captcha | jq -r '.api_key')
  if [ -z "$API_KEY" ] || [ "$API_KEY" = "null" ]; then
    echo "WARN: No 2captcha API key in Vault" >&2
    return 1
  fi

  local SUBMIT_DATA
  case "$TYPE" in
    hcaptcha)
      SUBMIT_DATA="key=${API_KEY}&method=hcaptcha&sitekey=${SITEKEY}&pageurl=${PAGE_URL}&json=1"
      ;;
    recaptcha)
      SUBMIT_DATA="key=${API_KEY}&method=userrecaptcha&googlekey=${SITEKEY}&pageurl=${PAGE_URL}&json=1"
      ;;
    turnstile)
      SUBMIT_DATA="key=${API_KEY}&method=turnstile&sitekey=${SITEKEY}&pageurl=${PAGE_URL}&json=1"
      ;;
    *)
      echo "ERROR: Unknown captcha type '$TYPE'" >&2
      return 1
      ;;
  esac

  local RESP
  RESP=$(curl -s "https://2captcha.com/in.php?${SUBMIT_DATA}")
  local STATUS
  STATUS=$(echo "$RESP" | jq -r '.status // 0')
  local REQUEST_ID
  REQUEST_ID=$(echo "$RESP" | jq -r '.request // empty')

  if [ "$STATUS" != "1" ] || [ -z "$REQUEST_ID" ]; then
    echo "WARN: 2captcha submit failed: $RESP" >&2
    return 1
  fi

  echo "2captcha task submitted: $REQUEST_ID" >&2

  local ELAPSED=0
  local POLL_INTERVAL=5
  local MAX_WAIT=120

  while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep $POLL_INTERVAL
    ELAPSED=$((ELAPSED + POLL_INTERVAL))

    RESP=$(curl -s "https://2captcha.com/res.php?key=${API_KEY}&action=get&id=${REQUEST_ID}&json=1")
    STATUS=$(echo "$RESP" | jq -r '.status // 0')
    local REQUEST
    REQUEST=$(echo "$RESP" | jq -r '.request // empty')

    if [ "$STATUS" = "1" ]; then
      echo "Solved in ${ELAPSED}s via 2captcha" >&2
      echo "$REQUEST"
      return 0
    elif [ "$REQUEST" = "CAPCHA_NOT_READY" ]; then
      echo "Waiting... (${ELAPSED}s)" >&2
      continue
    else
      echo "WARN: 2captcha returned: $RESP" >&2
      return 1
    fi
  done

  echo "WARN: 2captcha timed out after ${MAX_WAIT}s" >&2
  return 1
}

# --- Main: try Capsolver first, fall back to 2captcha ---
echo "Solving $TYPE captcha (sitekey=${SITEKEY:0:16}...)" >&2

TOKEN=$(capsolver_solve) && { echo "$TOKEN"; exit 0; }
echo "Capsolver failed, trying 2captcha fallback..." >&2

TOKEN=$(twocaptcha_solve) && { echo "$TOKEN"; exit 0; }

echo "ERROR: All solvers failed" >&2
exit 1
