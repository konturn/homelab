#!/bin/bash
# JIT Credential Helper Library
# Source this in any script or sub-agent task:
#   source /home/node/.openclaw/workspace/tools/jit-lib.sh
#
# Requires: VAULT_APPROLE_ROLE_ID, VAULT_APPROLE_SECRET_ID, VAULT_ADDR env vars

set -euo pipefail

JIT_URL="https://jit.lab.nkontur.com"

# Get a Vault token via AppRole
vault_login() {
  curl -s --request POST \
    --data "{\"role_id\":\"$VAULT_APPROLE_ROLE_ID\",\"secret_id\":\"$VAULT_APPROLE_SECRET_ID\"}" \
    "$VAULT_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token'
}

# Read a Vault secret path, returns .data.data JSON
vault_read() {
  local path=$1
  local token
  token=$(vault_login)
  curl -s -H "X-Vault-Token: $token" "$VAULT_ADDR/v1/$path" | jq '.data.data'
}

# Get JIT API key (cached per shell session)
_jit_key=""
jit_api_key() {
  if [ -z "$_jit_key" ]; then
    local token
    token=$(vault_login)
    _jit_key=$(curl -s -H "X-Vault-Token: $token" \
      "$VAULT_ADDR/v1/homelab/data/agents/jit-api-key" | jq -r '.data.data.api_key')
  fi
  echo "$_jit_key"
}

# Request a JIT credential
# Usage: jit_request <resource> <tier> <reason> [extra_json_fields]
# Returns: full JSON response (check .status and .credential)
jit_request() {
  local resource=$1 tier=$2 reason=$3
  local extra=${4:-}
  local key
  key=$(jit_api_key)
  
  local body="{\"resource\": \"$resource\", \"requester\": \"prometheus\", \"tier\": $tier, \"reason\": \"$reason\"}"
  if [ -n "$extra" ]; then
    body=$(echo "$body" | jq ". + $extra")
  fi
  
  curl -s "$JIT_URL/request" \
    -H "Content-Type: application/json" \
    -H "X-JIT-API-Key: $key" \
    -d "$body"
}

# Poll JIT status
# Usage: jit_status <request_id>
jit_status() {
  local req_id=$1
  local key
  key=$(jit_api_key)
  curl -s "$JIT_URL/status/$req_id" -H "X-JIT-API-Key: $key"
}

# Request and poll until credential is ready (for T1 auto-approve)
# Usage: jit_get <resource> <tier> <reason> [extra_json_fields]
# Returns: credential token on stdout, or exits 1
jit_get() {
  local resource=$1 tier=$2 reason=$3
  local extra=${4:-}
  
  local resp
  resp=$(jit_request "$resource" "$tier" "$reason" "$extra")
  
  # Check if credential is inline (T1 with inline response)
  local token
  token=$(echo "$resp" | jq -r '.credential.token // empty')
  if [ -n "$token" ]; then
    echo "$token"
    return 0
  fi
  
  local req_id
  req_id=$(echo "$resp" | jq -r '.request_id')
  local status
  status=$(echo "$resp" | jq -r '.status')
  
  if [ "$status" = "denied" ]; then
    echo "JIT denied: $(echo "$resp" | jq -r '.error // .message // "unknown"')" >&2
    return 1
  fi
  
  # Poll with exponential backoff
  local delay=2 max_delay=30 elapsed=0 timeout=900
  while [ $elapsed -lt $timeout ]; do
    sleep $delay
    elapsed=$((elapsed + delay))
    
    resp=$(jit_status "$req_id")
    status=$(echo "$resp" | jq -r '.status')
    
    case "$status" in
      approved)
        echo "$resp" | jq -r '.credential.token'
        return 0
        ;;
      denied|timeout)
        echo "JIT $status for $resource" >&2
        return 1
        ;;
      pending)
        delay=$((delay * 2))
        [ $delay -gt $max_delay ] && delay=$max_delay
        ;;
    esac
  done
  
  echo "JIT poll timeout for $resource" >&2
  return 1
}

# Convenience: get a Vault-backed service API key (T1)
# Usage: jit_service_key <service>  (radarr, sonarr, plex, ombi, nzbget, deluge, paperless, prowlarr)
# Returns: API key on stdout
jit_service_key() {
  local service=$1
  local vault_token
  vault_token=$(jit_get "$service" 1 "Access $service API")
  curl -s -H "X-Vault-Token: $vault_token" \
    "$VAULT_ADDR/v1/homelab/data/docker/$service" | jq -r '.data.data.api_key'
}

# Convenience: get a Grafana token (T1 dynamic)
jit_grafana_token() {
  jit_get grafana 1 "Grafana API access"
}

# Convenience: get an InfluxDB token (T1 dynamic)
jit_influxdb_token() {
  jit_get influxdb 1 "InfluxDB query access"
}

echo "JIT lib loaded. Functions: jit_request, jit_status, jit_get, jit_service_key, jit_grafana_token, jit_influxdb_token, vault_login, vault_read" >&2
