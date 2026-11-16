#!/bin/sh
# jitcurl: call a proxied service API with a short-lived JIT grant.
#
#   jitcurl <resource> <template[,template]> <METHOD> <path> [curl args...]
#   jitcurl --revoke <resource> <template[,template]>
#   jitcurl --templates <resource>     (what a resource offers; no grant)
#
# Example:
#   jitcurl radarr read GET '/api/v3/movie/lookup?term=the%20matrix'
#   jitcurl radarr write POST /api/v3/command -d '{"name":"MoviesSearch","movieIds":[12]}'
#
# The services behind the credential proxy (docker/jit-proxy:
# radarr, sonarr, prowlarr, plex, ombi, nzbget, deluge, tautulli, pihole)
# are never called with their own keys. jitcurl asks the JIT broker for a
# grant (credential_type proxy_grant, the templates as scopes; the broker
# applies tiers and Noah's approval as for any JIT request), then sends the
# request to the proxy, which checks it against the templates' rules and
# adds the real credential. The grant never leaves this script: it is cached
# under the credentials directory (mode 0600) until a minute before it
# expires, passed to curl in a header file (not argv), and never printed.
# Single-use grants (destructive templates) are fetched per call.
#
# Output: the response body on stdout. Exit 0 for HTTP 2xx/3xx; 22 for an
# HTTP error (the proxy's reason, e.g. no_rule or denylist, on stderr);
# 3 when the grant is denied or times out; 1 for anything else.
#
# Identity (docs/design/openclaw-identities.md), as jit-gitlab-token:
# JIT_API_KEY_FILE, else the key at JIT_API_KEY_VAULT_PATH read with the
# gateway's AppRole.
set -eu

JIT_URL=${JIT_URL:-https://jit.lab.nkontur.com}
PROXY_URL=${JIT_PROXY_URL:-http://172.30.6.10:8090}
KEY_PATH=${JIT_API_KEY_VAULT_PATH:-homelab/data/agents/jit-api-key}
CACHE_DIR=${JIT_GRANT_CACHE_DIR:-${JIT_GITLAB_CACHE_DIR:-/home/node/.openclaw/credentials}/jit-grants}
MIN_LEFT=60

die() { echo "jitcurl: $*" >&2; exit 1; }

usage() {
  sed -n '2,8p' "$0" >&2
  exit 2
}

umask 077
mkdir -p "$CACHE_DIR"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/jitcurl.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM

api_key() {
  if [ -n "${JIT_API_KEY_FILE:-}" ]; then
    tr -d '\n' < "$JIT_API_KEY_FILE"
    return
  fi
  : "${VAULT_ADDR:?VAULT_ADDR unset}"
  : "${VAULT_APPROLE_ROLE_ID:?VAULT_APPROLE_ROLE_ID unset}"
  : "${VAULT_APPROLE_SECRET_ID:?VAULT_APPROLE_SECRET_ID unset}"
  vt=$(jq -n --arg r "$VAULT_APPROLE_ROLE_ID" --arg s "$VAULT_APPROLE_SECRET_ID" \
         '{role_id: $r, secret_id: $s}' \
       | curl -sf --max-time 10 -X POST --data @- "$VAULT_ADDR/v1/auth/approle/login" \
       | jq -r '.auth.client_token // empty')
  [ -n "$vt" ] || return 1
  printf 'X-Vault-Token: %s\n' "$vt" \
    | curl -sf --max-time 10 -H @- "$VAULT_ADDR/v1/$KEY_PATH" \
    | jq -r '.data.data.api_key // empty'
  printf 'X-Vault-Token: %s\n' "$vt" \
    | curl -sf --max-time 10 -X POST -H @- -o /dev/null "$VAULT_ADDR/v1/auth/token/revoke-self" || true
}

# key_header writes "X-JIT-API-Key: <key>" to $TMP/key (once per run).
key_header() {
  [ -s "$TMP/key" ] && return 0
  k=$(api_key) || k=
  [ -n "$k" ] || die "no JIT API key"
  printf 'X-JIT-API-Key: %s\n' "$k" > "$TMP/key"
}

broker() { # broker <method> <path> [json body]
  key_header
  if [ $# -ge 3 ]; then
    curl -s --max-time 30 -X "$1" -H @"$TMP/key" -H 'Content-Type: application/json' --data "$3" "$JIT_URL$2"
  else
    curl -s --max-time 30 -X "$1" -H @"$TMP/key" "$JIT_URL$2"
  fi
}

cache_file() { printf '%s/%s--%s.json' "$CACHE_DIR" "$1" "$(printf '%s' "$2" | tr ',' '+')"; }

cached_grant() { # cached_grant <file>: prints the token if still usable
  [ -s "$1" ] || return 1
  exp=$(jq -r '.expires // 0' "$1" 2>/dev/null) || return 1
  [ $((exp - $(date +%s))) -gt "$MIN_LEFT" ] || return 1
  jq -r '.token // empty' "$1"
}

# new_grant <resource> <templates>: requests a grant and waits for approval
# when the tier needs it. Writes {token, grant_id, expires, single_use} to
# $TMP/grant.json.
new_grant() {
  res=$1 tmpl=$2
  body=$(jq -n --arg res "$res" --arg t "$tmpl" --arg reason "${JIT_REASON:-jitcurl $1 $2}" --arg ttl "${JIT_TTL:-}" \
    '{resource: $res, tier: 1, reason: $reason, credential_type: "proxy_grant",
      scopes: ($t | split(","))} + (if $ttl == "" then {} else {ttl: $ttl} end)')
  resp=$(broker POST /request "$body")
  err=$(printf '%s' "$resp" | jq -r '.error // empty' 2>/dev/null || echo "bad broker response")
  if [ -n "$err" ]; then
    echo "jitcurl: grant for $res ($tmpl) refused: $err $(printf '%s' "$resp" | jq -r '.message // empty' 2>/dev/null)" >&2
    exit 3
  fi
  if [ "$(printf '%s' "$resp" | jq -r '.credential.token // empty')" = "" ]; then
    id=$(printf '%s' "$resp" | jq -r '.request_id // empty')
    [ -n "$id" ] || die "no request_id from the broker"
    echo "jitcurl: $res ($tmpl) needs Noah's approval; waiting (request $id)" >&2
    delay=2 waited=0
    while :; do
      [ "$waited" -lt 900 ] || { echo "jitcurl: approval timed out" >&2; exit 3; }
      sleep "$delay"; waited=$((waited + delay))
      resp=$(broker GET "/status/$id")
      case $(printf '%s' "$resp" | jq -r '.status // empty') in
        approved) [ -n "$(printf '%s' "$resp" | jq -r '.credential.token // empty')" ] && break ;;
        denied|timeout|error) echo "jitcurl: grant $(printf '%s' "$resp" | jq -r .status)" >&2; exit 3 ;;
      esac
      [ "$delay" -ge 30 ] || delay=$((delay * 2))
    done
  fi
  printf '%s' "$resp" | jq '{token: .credential.token, grant_id: .credential.metadata.grant_id,
      single_use: (.credential.metadata.single_use == "true"),
      expires: (.credential.metadata.expires_at | fromdateiso8601)}' > "$TMP/grant.json"
}

# grant <resource> <templates>: prints the path of a usable grant file.
grant() {
  f=$(cache_file "$1" "$2")
  if tok=$(cached_grant "$f") && [ -n "$tok" ]; then
    printf '%s' "$f"; return 0
  fi
  exec 9>"$f.lock"
  flock -w 900 9 || die "lock timeout"
  if tok=$(cached_grant "$f") && [ -n "$tok" ]; then
    printf '%s' "$f"; return 0
  fi
  new_grant "$1" "$2"
  if [ "$(jq -r .single_use "$TMP/grant.json")" = "true" ]; then
    printf '%s' "$TMP/grant.json"   # used once, never cached
  else
    mv -f "$TMP/grant.json" "$f"
    printf '%s' "$f"
  fi
}

case ${1:-} in
  --revoke)
    [ $# -eq 3 ] || usage
    f=$(cache_file "$2" "$3")
    [ -s "$f" ] || exit 0
    id=$(jq -r '.grant_id // empty' "$f")
    rm -f "$f"
    [ -n "$id" ] && broker POST "/grants/$id/revoke" >/dev/null || true
    exit 0 ;;
  --templates)
    [ $# -eq 2 ] || usage
    resp=$(broker POST /request "$(jq -n --arg r "$2" '{resource: $r, tier: 1, reason: "list templates", credential_type: "proxy_grant", scopes: ["?"]}')")
    printf '%s\n' "$resp" | jq -r '.error // .'
    exit 0 ;;
  ""|-h|--help) usage ;;
esac

[ $# -ge 4 ] || usage
RES=$1 TMPL=$2 METHOD=$3 URLPATH=$4
shift 4
case $URLPATH in /*) ;; *) die "path must start with /" ;; esac

attempt=0
while :; do
  attempt=$((attempt + 1))
  gf=$(grant "$RES" "$TMPL")
  key_header
  { printf 'Authorization: Bearer %s\n' "$(jq -r .token "$gf")"; cat "$TMP/key"; } > "$TMP/auth"
  code=$(curl -s --max-time 150 -o "$TMP/body" -w '%{http_code}' -H @"$TMP/auth" -X "$METHOD" "$@" \
           "$PROXY_URL/r/$RES$URLPATH") || code=000
  rm -f "$TMP/auth"
  case $gf in "$TMP"/*) rm -f "$gf" ;; esac   # single-use: spent
  reason=$(jq -r '.error // empty' "$TMP/body" 2>/dev/null || true)
  case "$code:$reason" in
    401:unknown_grant|401:no_grant|403:expired|403:revoked|403:spent|403:budget)
      # The proxy restarted or the grant ran out: drop it, get a new one once.
      rm -f "$(cache_file "$RES" "$TMPL")"
      [ "$attempt" -lt 2 ] && continue ;;
  esac
  break
done

cat "$TMP/body"
case $code in
  2??|3??) exit 0 ;;
  000) echo "jitcurl: credential proxy unavailable ($PROXY_URL)" >&2; exit 1 ;;
  *) echo "jitcurl: HTTP $code${reason:+ $reason}: $(jq -r '.detail // empty' "$TMP/body" 2>/dev/null || true)" >&2; exit 22 ;;
esac
