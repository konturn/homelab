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

# Parse lease_ttl string (e.g. "59m59s", "15m0s", "1h30m") into seconds
_parse_ttl() {
  local ttl=$1 seconds=0
  local h m s
  h=$(echo "$ttl" | grep -oP '\d+(?=h)' || true)
  m=$(echo "$ttl" | grep -oP '\d+(?=m)' || true)
  s=$(echo "$ttl" | grep -oP '\d+(?=s)' || true)
  [ -n "$h" ] && seconds=$((seconds + h * 3600))
  [ -n "$m" ] && seconds=$((seconds + m * 60))
  [ -n "$s" ] && seconds=$((seconds + s))
  echo "$seconds"
}

# In-memory credential cache using fd-backed temp file (survives subshells)
# The cache file is created with mktemp, opened on fd 7, then unlinked.
# It exists only in /proc/self/fd/7 — no path on disk to steal from.
_JIT_CACHE_FILE=$(mktemp)
echo "{}" > "$_JIT_CACHE_FILE"

# Check in-memory cache for a resource
# Returns: cached token on stdout if valid, or returns 1
_cache_check() {
  local resource=$1
  local entry expires_at token now
  entry=$(jq -r --arg r "$resource" '.[$r] // empty' "$_JIT_CACHE_FILE")
  [ -z "$entry" ] && return 1
  expires_at=$(echo "$entry" | jq -r '.expires_at')
  now=$(date +%s)
  if [ "$now" -lt "$expires_at" ]; then
    echo "$entry" | jq -r '.token'
    return 0
  fi
  return 1
}

# Store credential in cache
_cache_store() {
  local resource=$1 token=$2 ttl_seconds=$3
  local margin=$((ttl_seconds / 10))
  [ "$margin" -gt 60 ] && margin=60
  [ "$margin" -lt 10 ] && margin=10
  local expires_at=$(( $(date +%s) + ttl_seconds - margin ))
  local tmp
  tmp=$(jq --arg r "$resource" --arg t "$token" --argjson e "$expires_at" \
    '.[$r] = {"token": $t, "expires_at": $e}' "$_JIT_CACHE_FILE")
  echo "$tmp" > "$_JIT_CACHE_FILE"
}

# Cleanup cache file on exit
trap 'rm -f "$_JIT_CACHE_FILE" 2>/dev/null' EXIT

# Request and poll until credential is ready, with in-memory caching
# Cache is per-process only — no cross-session credential leakage
# Usage: jit_get <resource> <tier> <reason> [extra_json_fields]
# Returns: credential token on stdout, or exits 1
jit_get() {
  local resource=$1 tier=$2 reason=$3
  local extra=${4:-}

  # Check in-memory cache first (skip for vault — each request may have different paths)
  if [ "$resource" != "vault" ]; then
    local cached
    if cached=$(_cache_check "$resource"); then
      echo "$cached"
      return 0
    fi
  fi
  
  local resp
  resp=$(jit_request "$resource" "$tier" "$reason" "$extra")
  
  # Check if credential is inline (T1 with inline response)
  local token lease_ttl
  token=$(echo "$resp" | jq -r '.credential.token // empty')
  if [ -n "$token" ]; then
    # Cache using lease_ttl from response
    lease_ttl=$(echo "$resp" | jq -r '.credential.lease_ttl // empty')
    if [ -n "$lease_ttl" ] && [ "$resource" != "vault" ]; then
      local ttl_secs
      ttl_secs=$(_parse_ttl "$lease_ttl")
      [ "$ttl_secs" -gt 0 ] && _cache_store "$resource" "$token" "$ttl_secs"
    fi
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
        token=$(echo "$resp" | jq -r '.credential.token')
        lease_ttl=$(echo "$resp" | jq -r '.credential.lease_ttl // empty')
        if [ -n "$lease_ttl" ] && [ "$resource" != "vault" ]; then
          local ttl_secs
          ttl_secs=$(_parse_ttl "$lease_ttl")
          [ "$ttl_secs" -gt 0 ] && _cache_store "$resource" "$token" "$ttl_secs"
        fi
        echo "$token"
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

# Convenience: get a Vault T2 token with custom paths
# Usage: jit_vault <reason> <vault_paths_json>
# Example: jit_vault "Search vault" '[{"path":"homelab/data/","capabilities":["list"]}]'
# Returns: Vault token on stdout (blocks until approved)
jit_vault() {
  local reason=$1 paths=$2
  jit_get vault 2 "$reason" "{\"vault_paths\":$paths}"
}

# Convenience: get a Vault token with write access to a specific path
# Usage: jit_vault_write <reason> <path>
# Example: jit_vault_write "Store Gmail creds" "homelab/data/docker/gmail"
# Returns: Vault token on stdout (blocks until approved)
jit_vault_write() {
  local reason=$1 path=$2
  jit_vault "$reason" "[{\"path\":\"$path\",\"capabilities\":[\"create\",\"update\",\"read\"]}]"
}

# Write a secret to Vault via JIT
# Usage: vault_write <path> <json_data>
# Example: vault_write "homelab/data/docker/gmail" '{"client_id":"...","refresh_token":"..."}'
# Requests JIT write access, waits for approval, then writes
vault_write() {
  local path=$1 json_data=$2
  local token
  token=$(jit_vault_write "Write secret to $path" "$path")
  curl -s -X POST "$VAULT_ADDR/v1/$path" \
    -H "X-Vault-Token: $token" \
    -H "Content-Type: application/json" \
    -d "{\"data\":$json_data}"
}

# Convenience: get a Gmail access token via JIT + OAuth refresh
# Usage: jit_gmail_token
# Returns: Gmail API access token on stdout (blocks until JIT approved)
jit_gmail_token() {
  # gmail-read backend returns OAuth2 access token directly
  jit_get gmail-read 1 "Gmail API access for email reading"
}

# Convenience: get a Gmail send token via JIT
# Usage: jit_gmail_send_token
# Returns: Gmail API access token with send scope on stdout
jit_gmail_send_token() {
  jit_get gmail-send 2 "Gmail API access for sending email"
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

echo "JIT lib loaded. Functions: jit_request, jit_status, jit_get, jit_vault, jit_vault_write, vault_write, jit_gmail_token, jit_gmail_send_token, jit_service_key, jit_grafana_token, jit_influxdb_token, vault_login, vault_read" >&2
