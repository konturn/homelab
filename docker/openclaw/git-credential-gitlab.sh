#!/bin/sh
# Git credential helper for gitlab.lab.nkontur.com in openclaw-gateway.
#
# entrypoint-wrapper.sh installs it in the container's global git config
# (with useHttpPath, so git passes the repo path). Remote URLs stay plain
# https://gitlab.lab.nkontur.com/<group>/<repo>.git; the token is looked up
# when git needs it and is never written to a repo config.
#
# Token per repo:
#   moltbot/clawd-memory (workspace memory sync): its own project token,
#     1. Vault homelab/openclaw/tokens, field clawd_memory_token
#     2. /home/node/.openclaw/credentials/gitlab-clawd-memory.token (0600)
#   everything else, or clawd-memory when neither is set:
#     $GITLAB_TOKEN, else Vault homelab/openclaw/tokens, field gitlab_token
#
# Vault is read with the gateway's AppRole (VAULT_ADDR, VAULT_APPROLE_ROLE_ID,
# VAULT_APPROLE_SECRET_ID), same as the openclaw-ops policy allows.
# Only "get" is answered; store/erase are no-ops so git never caches tokens.

[ "${1:-}" = get ] || exit 0

host=
path=
while IFS= read -r line && [ -n "$line" ]; do
  case $line in
    host=*) host=${line#host=} ;;
    path=*) path=${line#path=} ;;
  esac
done
[ "$host" = gitlab.lab.nkontur.com ] || exit 0

TOKEN_FILE=/home/node/.openclaw/credentials/gitlab-clawd-memory.token

# vault_field <field>: print a field of homelab/openclaw/tokens, or nothing.
vault_field() {
  [ -n "${VAULT_ADDR:-}" ] || return 0
  [ -n "${VAULT_APPROLE_ROLE_ID:-}" ] || return 0
  [ -n "${VAULT_APPROLE_SECRET_ID:-}" ] || return 0
  vt=$(jq -n --arg r "$VAULT_APPROLE_ROLE_ID" --arg s "$VAULT_APPROLE_SECRET_ID" \
         '{role_id: $r, secret_id: $s}' \
       | curl -sf --max-time 10 -X POST --data @- \
           "$VAULT_ADDR/v1/auth/approle/login" \
       | jq -r '.auth.client_token // empty') || return 0
  [ -n "$vt" ] || return 0
  # Header via stdin so the Vault token is not in curl's argv.
  printf 'X-Vault-Token: %s\n' "$vt" \
    | curl -sf --max-time 10 -H @- "$VAULT_ADDR/v1/homelab/data/openclaw/tokens" \
    | jq -r --arg f "$1" '.data.data[$f] // empty'
  printf 'X-Vault-Token: %s\n' "$vt" \
    | curl -sf --max-time 10 -X POST -H @- -o /dev/null \
        "$VAULT_ADDR/v1/auth/token/revoke-self" || true
}

tok=
case $path in
  moltbot/clawd-memory | moltbot/clawd-memory.git)
    tok=$(vault_field clawd_memory_token 2>/dev/null)
    if [ -z "$tok" ] && [ -r "$TOKEN_FILE" ]; then
      tok=$(tr -d '[:space:]' < "$TOKEN_FILE")
    fi
    ;;
esac
[ -n "$tok" ] || tok=${GITLAB_TOKEN:-}
[ -n "$tok" ] || tok=$(vault_field gitlab_token 2>/dev/null)
[ -n "$tok" ] || exit 0

printf 'username=oauth2\npassword=%s\n' "$tok"
