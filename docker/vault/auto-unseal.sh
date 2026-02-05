#!/bin/sh
# Vault Auto-Unseal Entrypoint
#
# Starts Vault server, waits for the API to be ready, then automatically
# unseals using keys from a mounted file. This eliminates the need for
# manual unsealing after container restarts.
#
# Required environment:
#   VAULT_UNSEAL_KEYS_FILE - Path to file containing unseal keys (one hex key per line)
#   VAULT_ADDR - Vault API address (set in docker-compose)
#
# The unseal keys file should contain hex-encoded keys, one per line.
# Only the threshold number of keys (3 of 5) are needed.

UNSEAL_KEYS_FILE="${VAULT_UNSEAL_KEYS_FILE:-/vault/unseal/unseal-keys}"
MAX_RETRIES=30
RETRY_INTERVAL=2

log() {
    echo "[auto-unseal] $(date '+%Y-%m-%d %H:%M:%S') $*"
}

# Start Vault server in the background
log "Starting Vault server..."
vault server -config=/vault/config/config.hcl &
VAULT_PID=$!

# Wait for Vault API to become available
# vault status exit codes: 0 = unsealed, 1 = error, 2 = sealed
log "Waiting for Vault API to be ready..."
retries=0
api_ready=false
while [ $retries -lt $MAX_RETRIES ]; do
    rc=0
    vault status > /dev/null 2>&1 || rc=$?
    if [ "$rc" = "0" ]; then
        log "Vault is already unsealed. Nothing to do."
        api_ready=true
        break
    elif [ "$rc" = "2" ]; then
        log "Vault API is ready (sealed). Proceeding to unseal..."
        api_ready=true
        break
    fi
    retries=$((retries + 1))
    sleep $RETRY_INTERVAL
done

if [ "$api_ready" = "false" ]; then
    log "ERROR: Vault API did not become available after $((MAX_RETRIES * RETRY_INTERVAL)) seconds."
    log "Vault will continue running. Manual unseal required."
    wait $VAULT_PID
    exit $?
fi

# Check if Vault is already unsealed (exit code 0 means unsealed)
rc=0
vault status > /dev/null 2>&1 || rc=$?
if [ "$rc" = "0" ]; then
    log "Vault is already unsealed."
    wait $VAULT_PID
    exit $?
fi

# Check for unseal keys file
if [ ! -f "$UNSEAL_KEYS_FILE" ]; then
    log "WARNING: Unseal keys file not found at $UNSEAL_KEYS_FILE"
    log "Vault will continue running in sealed state. Manual unseal required."
    wait $VAULT_PID
    exit $?
fi

# Read and apply unseal keys
log "Unsealing Vault..."
unseal_count=0
while IFS= read -r key || [ -n "$key" ]; do
    # Skip empty lines and comments
    case "$key" in
        ''|\#*) continue ;;
    esac

    unseal_count=$((unseal_count + 1))
    log "Applying unseal key $unseal_count..."

    if ! vault operator unseal "$key" > /dev/null 2>&1; then
        log "WARNING: Failed to apply unseal key $unseal_count"
    fi

    # Check if we've reached the threshold (exit code 0 = unsealed)
    rc=0
    vault status > /dev/null 2>&1 || rc=$?
    if [ "$rc" = "0" ]; then
        log "Vault successfully unsealed after $unseal_count key(s)."
        break
    fi
done < "$UNSEAL_KEYS_FILE"

# Final status check
rc=0
vault status > /dev/null 2>&1 || rc=$?
if [ "$rc" = "0" ]; then
    log "Vault is running and unsealed."
else
    log "WARNING: Vault is still sealed after applying all available keys."
    log "Manual unseal may be required."
fi

# Wait for Vault process (keep container running)
wait $VAULT_PID
