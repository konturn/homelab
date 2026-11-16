#!/bin/sh
# er-action: ask the event router's executor for a fixed action, as yourself.
#
#   er-action collect host | container:<name> | unit:<unit>
#   er-action restart <container> "<reason>" [inc-<id>]
#   er-action reset-failed <unit> "<reason>"
#
# collect: read-only (container state, health, last 200 log lines, unit
# status, zpool status -x, df), redacted. restart: waits for Noah's tap when
# your tier needs one (up to 10 min; ER_NO_WAIT=1 returns at once with the
# approval id, and `ER_APPROVAL=apr-... er-action restart ...` asks again).
# reset-failed: clears a failed unit's state, never starts it.
#
# Identity (docs/design/openclaw-identities.md): a token derived from your
# own JIT key (JIT_API_KEY_FILE, else JIT_API_KEY_VAULT_PATH read with the
# gateway's AppRole), sent in a header file, never on a command line and
# never printed. The router and the executor check it, the allowlists, the
# tier and every executor guard (docker/event-router/README.md).
#
# Exit: 0 done; 3 refused, denied or expired (reason on stderr: a refusal is
# final for this run); 4 still waiting for a tap; 2 usage; 1 anything else.
set -eu

URL=${EVENT_ROUTER_URL:-http://event-router:8080}
KEY_PATH=${JIT_API_KEY_VAULT_PATH:-homelab/data/agents/openclaw-noah}
LABEL=event-router-agent-v1

die() { echo "er-action: $*" >&2; exit 1; }
usage() { sed -n '4,6p' "$0" >&2; exit 2; }

umask 077
TMP=$(mktemp -d "${TMPDIR:-/tmp}/er-action.XXXXXX")
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

auth_header() {
  k=$(api_key) || k=
  [ -n "$k" ] || die "no JIT key"
  # printf is a shell builtin: the key never appears in a process list.
  tok=$(printf '%s:%s' "$k" "$LABEL" | sha256sum | cut -c1-64)
  printf 'Authorization: Bearer %s\n' "$tok" > "$TMP/auth"
}

# post <action> <json body>: sets CODE, writes the reply to $TMP/reply
post() {
  CODE=$(curl -s --max-time 200 -o "$TMP/reply" -w '%{http_code}' -X POST -H @"$TMP/auth" \
           -H 'Content-Type: application/json' --data "$2" "$URL/agent/$1") || CODE=000
}

field() { jq -r "$1 // empty" "$TMP/reply" 2>/dev/null || true; }

finish() {
  case $CODE in
    200)
      if [ "$ACTION" = collect ]; then field .output; [ "$(field .truncated)" = true ] && echo "[output truncated]" >&2
      else field .detail; fi
      exit 0 ;;
    202) echo "er-action: waiting for Noah's tap: approval $(field .approval)" >&2; exit 4 ;;
    000) die "event router unreachable ($URL)" ;;
    400|401) echo "er-action: HTTP $CODE: $(field .error)" >&2; exit 3 ;;
    *) ra=$(field .retry_after)
       echo "er-action: refused ($(field .refused)$(field .error)): $(field .detail)${ra:+ (retry after ${ra}s)}" >&2
       exit 3 ;;
  esac
}

[ $# -ge 2 ] || usage
case $1 in
  collect) ACTION=collect; [ $# -eq 2 ] || usage ;;
  restart) ACTION=restart_container; [ $# -ge 3 ] && [ $# -le 4 ] || usage ;;
  reset-failed) ACTION=reset_failed; [ $# -eq 3 ] || usage ;;
  *) usage ;;
esac
TARGET=$2 REASON=${3:-} INCIDENT=${4:-}
command -v jq >/dev/null || die "jq missing"
auth_header

body() {
  jq -n --arg t "$TARGET" --arg r "$REASON" --arg i "$INCIDENT" --arg a "${1:-}" \
    '{target: $t} + (if $r == "" then {} else {reason: $r} end)
     + (if $i == "" then {} else {incident: $i} end)
     + (if $a == "" then {} else {approval: $a} end)'
}

post "$ACTION" "$(body "${ER_APPROVAL:-}")"
if [ "$CODE" = 202 ] && [ -z "${ER_NO_WAIT:-}" ]; then
  APR=$(field .approval)
  echo "er-action: $TARGET needs Noah's tap; waiting (approval $APR)" >&2
  waited=0
  while [ "$CODE" = 202 ] && [ "$waited" -lt 660 ]; do
    sleep 15; waited=$((waited + 15))
    post "$ACTION" "$(body "$APR")"
  done
fi
finish
