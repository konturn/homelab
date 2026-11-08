#!/bin/sh
# jit-gitlab-token: print a short-lived GitLab token for the agent's own
# GitLab user (moltbot), issued by the JIT broker.
#
# Resource "gitlab-moltbot" is tier 1: auto-approved (no Telegram prompt) but
# logged by the broker and in GitLab's audit log. The broker mints an
# impersonation token with moltbot's own permissions and revokes it when the
# TTL (1h) is up (docker/jit-approval-svc/internal/backend/gitlab_agent.go).
#
# The token is cached for every session in this container until 10 minutes
# before it expires, so git and API calls don't hit the broker's rate limit
# (5 requests per resource per 15 min). The cache is mode 0600 under the
# credentials directory, which the transcript scrubber and backups already
# treat as secret.
#
# Used by git-credential-gitlab and by gitlab_token() in tools/jit-lib.sh.
# Prints the token on stdout; never logs it. Exit 1 on failure.
set -eu

CACHE_DIR=${JIT_GITLAB_CACHE_DIR:-/home/node/.openclaw/credentials}
CACHE=$CACHE_DIR/jit-gitlab.json
LOCK=$CACHE_DIR/jit-gitlab.lock
JIT_URL=${JIT_URL:-https://jit.lab.nkontur.com}
TTL_SECONDS=3600
MIN_LEFT=600
REASON=${JIT_GITLAB_REASON:-openclaw: GitLab API and git access as moltbot}

umask 077
mkdir -p "$CACHE_DIR"

cached() {
  [ -s "$CACHE" ] || return 1
  exp=$(jq -r '.expires // 0' "$CACHE" 2>/dev/null) || return 1
  [ $((exp - $(date +%s))) -gt "$MIN_LEFT" ] || return 1
  tok=$(jq -r '.token // empty' "$CACHE" 2>/dev/null) || return 1
  [ -n "$tok" ] || return 1
  printf '%s\n' "$tok"
}

fetch() {
  : "${VAULT_ADDR:?VAULT_ADDR unset}"
  : "${VAULT_APPROLE_ROLE_ID:?VAULT_APPROLE_ROLE_ID unset}"
  : "${VAULT_APPROLE_SECRET_ID:?VAULT_APPROLE_SECRET_ID unset}"
  vt=$(jq -n --arg r "$VAULT_APPROLE_ROLE_ID" --arg s "$VAULT_APPROLE_SECRET_ID" \
         '{role_id: $r, secret_id: $s}' \
       | curl -sf --max-time 10 -X POST --data @- "$VAULT_ADDR/v1/auth/approle/login" \
       | jq -r '.auth.client_token // empty')
  [ -n "$vt" ] || { echo "jit-gitlab-token: vault login failed" >&2; return 1; }
  key=$(printf 'X-Vault-Token: %s\n' "$vt" \
        | curl -sf --max-time 10 -H @- "$VAULT_ADDR/v1/homelab/data/agents/jit-api-key" \
        | jq -r '.data.data.api_key // empty')
  printf 'X-Vault-Token: %s\n' "$vt" \
    | curl -sf --max-time 10 -X POST -H @- -o /dev/null "$VAULT_ADDR/v1/auth/token/revoke-self" || true
  [ -n "$key" ] || { echo "jit-gitlab-token: no JIT API key" >&2; return 1; }

  start=$(date +%s)
  body=$(jq -n --arg reason "$REASON" \
           '{resource: "gitlab-moltbot", requester: "prometheus", tier: 1, reason: $reason, ttl: "1h"}')
  resp=$(printf 'X-JIT-API-Key: %s\n' "$key" \
         | curl -s --max-time 30 -H @- -H 'Content-Type: application/json' \
             --data "$body" "$JIT_URL/request")
  tok=$(printf '%s' "$resp" | jq -r '.credential.token // empty' 2>/dev/null || true)
  if [ -z "$tok" ]; then
    echo "jit-gitlab-token: JIT request failed: $(printf '%s' "$resp" | jq -r '.error // .message // "no credential"' 2>/dev/null | head -c 300)" >&2
    return 1
  fi
  tmp=$(mktemp "$CACHE_DIR/.jit-gitlab.XXXXXX")
  # Token via stdin, not argv, so it never shows up in a process listing.
  printf '%s' "$tok" | jq -R --argjson e $((start + TTL_SECONDS)) '{token: ., expires: $e}' > "$tmp"
  mv -f "$tmp" "$CACHE"
  printf '%s\n' "$tok"
}

cached && exit 0
# One fetch at a time across concurrent sessions; re-check after the lock.
exec 9>"$LOCK"
flock -w 60 9 || { echo "jit-gitlab-token: lock timeout" >&2; exit 1; }
cached && exit 0
fetch
