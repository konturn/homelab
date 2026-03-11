#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/etc/grafana-cleanup.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found"
  exit 1
fi
# shellcheck source=/dev/null
. "$ENV_FILE"

: "${GRAFANA_URL:?missing GRAFANA_URL}"
: "${GRAFANA_ADMIN_TOKEN:?missing GRAFANA_ADMIN_TOKEN}"
: "${GRAFANA_SA_ID:?missing GRAFANA_SA_ID}"

# List all tokens on the service account
tokens=$(curl -sf -H "Authorization: Bearer ${GRAFANA_ADMIN_TOKEN}" \
  "${GRAFANA_URL}/api/serviceaccounts/${GRAFANA_SA_ID}/tokens") || {
  echo "ERROR: failed to list tokens"
  exit 1
}

now=$(date -u +%s)
deleted=0
total=$(echo "$tokens" | jq 'length')

# Delete expired tokens
echo "$tokens" | jq -c '.[] | select(.expiration != null) | {id: .id, expiration: .expiration}' | while read -r entry; do
  exp=$(echo "$entry" | jq -r '.expiration')
  tid=$(echo "$entry" | jq -r '.id')

  # Parse ISO8601 expiration to epoch
  exp_epoch=$(date -d "$exp" +%s 2>/dev/null) || continue

  if (( exp_epoch < now )); then
    if curl -sf -X DELETE -H "Authorization: Bearer ${GRAFANA_ADMIN_TOKEN}" \
      "${GRAFANA_URL}/api/serviceaccounts/${GRAFANA_SA_ID}/tokens/${tid}" >/dev/null 2>&1; then
      deleted=$((deleted + 1))
    fi
  fi
done

# Note: deleted count from subshell won't propagate. Re-count.
remaining=$(curl -sf -H "Authorization: Bearer ${GRAFANA_ADMIN_TOKEN}" \
  "${GRAFANA_URL}/api/serviceaccounts/${GRAFANA_SA_ID}/tokens" | jq 'length')
cleaned=$((total - remaining))

if (( cleaned > 0 )); then
  echo "Cleaned $cleaned expired Grafana SA tokens ($remaining remaining)"
else
  echo "No expired tokens found ($total total)"
fi
