#!/usr/bin/env bash
# set-deadman-ping-url.sh: store the healthchecks.io ping URL for the router
# heartbeat in Vault and start the heartbeat now (docs/DEADMAN.md).
#
# Run on the router as root, from the repo checkout. The URL is read from
# stdin (or a hidden prompt), never from argv, so it stays out of shell
# history and the process list. It is never printed.
#
#   scripts/set-deadman-ping-url.sh            # prompts
#   printf '%s' "$URL" | scripts/set-deadman-ping-url.sh
#
# Steps: write Vault homelab/monitoring/healthchecks (ping_url), write
# /etc/deadman-check.env (0600) the way ansible/roles/deadman does, enable
# deadman-check.timer and run one check. The next router deploy renders the
# same file from Vault, so nothing drifts.
set -euo pipefail

ENV_FILE=/etc/deadman-check.env
export VAULT_ADDR="${VAULT_ADDR:-https://10.3.32.6:8200}"
export VAULT_SKIP_VERIFY="${VAULT_SKIP_VERIFY:-1}"

[ "$(id -u)" -eq 0 ] || { echo "run as root on the router" >&2; exit 1; }
[ -x /usr/local/sbin/deadman-check ] || { echo "deadman-check is not installed; deploy ansible/roles/deadman first" >&2; exit 1; }

if [ -t 0 ]; then
    read -rsp "healthchecks.io ping URL: " url; echo
else
    IFS= read -r url || true
fi
url="${url%/}"
[[ "$url" =~ ^https://[A-Za-z0-9.-]+/[A-Za-z0-9/_-]+$ ]] \
    || { echo "that does not look like a ping URL (https://hc-ping.com/<uuid>)" >&2; exit 1; }

if [ -z "${VAULT_TOKEN:-}" ]; then
    VAULT_TOKEN=$(glab variable get VAULT_TOKEN)
    export VAULT_TOKEN
fi

printf '%s' "$url" | vault kv put -mount=homelab monitoring/healthchecks ping_url=- >/dev/null
echo "stored in Vault: homelab/monitoring/healthchecks (ping_url)"

umask 077
printf 'DEADMAN_PING_URL=%s\n' "$url" >"$ENV_FILE.tmp"
chown root:root "$ENV_FILE.tmp"
chmod 0600 "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
echo "wrote $ENV_FILE"

systemctl enable --now deadman-check.timer
systemctl start deadman-check.service || true
journalctl -u deadman-check.service -n 15 --no-pager -o cat
systemctl list-timers deadman-check.timer --no-pager
