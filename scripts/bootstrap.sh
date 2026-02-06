#!/usr/bin/env bash
# bootstrap.sh — Single-shot bootstrap: bare-metal to green pipeline
#
# Run directly on the router as root. Idempotent (safe to re-run).
#
# Usage:
#   bootstrap.sh --restore              # Restore from Backblaze B2 backup
#   bootstrap.sh --fresh                # Clean install, no backup
#   bootstrap.sh --fresh --secrets-file /path/to/secrets.env.gpg
#
# Steps (in order):
#   1.  Install Docker + docker-compose plugin
#   2.  Create required Docker macvlan networks
#   3.  Install restic
#   4.  Restore persistent data from Backblaze B2 (--restore only)
#   5.  Start GitLab container
#   6.  Wait for GitLab to be healthy
#   7.  Install gitlab-runner
#   8.  Start Vault container
#   9.  Bootstrap Vault (init/unseal + optional secret seeding)
#   10. Set CI/CD variables via GitLab API
#   11. Register GitLab runner
#   12. Trigger first pipeline and wait for green
#
# After completion the first CI pipeline on main should be fully green
# with ZERO manual intervention.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PERSISTENT_DATA="/persistent_data/application"
GITLAB_DATA="${PERSISTENT_DATA}/gitlab"
GITLAB_IMAGE="gitlab/gitlab-ee:latest"
GITLAB_CONTAINER="gitlab-bootstrap"
GITLAB_HTTP_PORT=80
VAULT_DATA="${PERSISTENT_DATA}/vault"
VAULT_IMAGE="hashicorp/vault:1.21"
VAULT_CONTAINER="vault-bootstrap"
VAULT_ADDR="https://vault.lab.nkontur.com:8200"
VAULT_INTERNAL_ADDR="https://127.0.0.1:8200"
VAULT_IP="10.3.32.6"
PROJECT_ID=4
TOTAL_STEPS=12

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MODE=""
SECRETS_FILE=""
SKIP_PIPELINE_WAIT=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [--restore | --fresh] [OPTIONS]

Modes:
  --restore          Restore from Backblaze B2 backup (Vault data included)
  --fresh            Fresh install — no backup, seed Vault from scratch

Options:
  --secrets-file PATH   GPG-encrypted KEY=VALUE file for non-interactive secret seeding
  --skip-pipeline-wait  Don't wait for the first pipeline to finish
  -h, --help            Show this help
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restore) MODE="restore"; shift ;;
    --fresh)   MODE="fresh";   shift ;;
    --secrets-file)
      SECRETS_FILE="$2"; shift 2 ;;
    --skip-pipeline-wait)
      SKIP_PIPELINE_WAIT=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo -e "\033[1;32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap]\033[0m WARNING: $*"; }
err()  { echo -e "\033[1;31m[bootstrap]\033[0m ERROR: $*" >&2; }
die()  { err "$@"; exit 1; }

step() {
  local n="$1"; shift
  echo ""
  log "Step ${n}/${TOTAL_STEPS}: $*"
}

require_root() {
  [[ $EUID -eq 0 ]] || die "This script must be run as root."
}

prompt_mode() {
  if [[ -z "$MODE" ]]; then
    echo ""
    log "No mode specified. Choose one:"
    log "  1) restore — Restore from Backblaze B2 backup"
    log "  2) fresh   — Clean install, no backup"
    echo ""
    read -rp "[bootstrap] Mode [1/2]: " choice
    case "$choice" in
      1|restore) MODE="restore" ;;
      2|fresh)   MODE="fresh" ;;
      *) die "Invalid choice. Use --restore or --fresh." ;;
    esac
  fi
  log "Mode: ${MODE}"
}

# Prompt for a value if not already set; mask input for secrets.
# Usage: prompt_var VARNAME "prompt text" [secret]
prompt_var() {
  local varname="$1" prompt="$2" secret="${3:-}"
  if [[ -n "${!varname:-}" ]]; then
    return
  fi
  if [[ "$secret" == "secret" ]]; then
    read -rsp "[bootstrap] ${prompt}: " "$varname"
    echo
  else
    read -rp "[bootstrap] ${prompt}: " "$varname"
  fi
  export "$varname"
}

# Load secrets from GPG-encrypted file into environment.
load_secrets_file() {
  if [[ -z "$SECRETS_FILE" ]]; then return; fi
  if [[ ! -f "$SECRETS_FILE" ]]; then
    die "Secrets file not found: ${SECRETS_FILE}"
  fi
  log "Decrypting secrets file: ${SECRETS_FILE}"
  local decrypted
  decrypted=$(gpg --quiet --batch --decrypt "$SECRETS_FILE" 2>/dev/null) \
    || die "Failed to decrypt secrets file. Check GPG key."
  while IFS='=' read -r key value; do
    # Skip blanks and comments
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    export "$key"="$value"
    log "  Loaded: ${key}"
  done <<< "$decrypted"
}

# Wait for an HTTP endpoint to respond with a given status code.
# Usage: wait_for_http URL MAX_WAIT_SECS [EXPECTED_STATUS]
wait_for_http() {
  local url="$1" max_wait="${2:-300}" expected="${3:-200}"
  local interval=10 elapsed=0
  while [[ $elapsed -lt $max_wait ]]; do
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' "$url" 2>/dev/null) || true
    if [[ "$code" == "$expected" ]]; then
      return 0
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
    # Exponential-ish backoff capped at 30s
    [[ $interval -lt 30 ]] && interval=$((interval + 5))
    log "  ... waiting (${elapsed}s / ${max_wait}s)"
  done
  return 1
}

# ---------------------------------------------------------------------------
# Step 1: Install Docker + compose plugin
# ---------------------------------------------------------------------------
install_docker() {
  if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
  else
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    log "Docker installed: $(docker --version)"
  fi

  if docker compose version &>/dev/null; then
    log "Docker Compose plugin already installed: $(docker compose version)"
  else
    log "Installing Docker Compose plugin..."
    apt-get update -qq
    apt-get install -y -qq docker-compose-plugin
    log "Docker Compose plugin installed: $(docker compose version)"
  fi
}

# ---------------------------------------------------------------------------
# Step 2: Create Docker macvlan networks
# ---------------------------------------------------------------------------
create_networks() {
  local -A networks=(
    [internal]="bond0.3|10.3.0.0/16|10.3.0.0/18"
    [external]="bond0.2|10.2.0.0/16|10.2.0.0/18"
    [iot]="bond0.6|10.6.0.0/16|10.6.0.0/18"
    [mgmt]="bond0.4|10.4.0.0/16|10.4.0.0/18"
  )

  for net in "${!networks[@]}"; do
    if docker network inspect "$net" &>/dev/null; then
      log "Network '$net' already exists."
    else
      IFS='|' read -r parent subnet ip_range <<<"${networks[$net]}"
      log "Creating macvlan network '$net' (parent=$parent, subnet=$subnet)..."
      docker network create \
        --driver macvlan \
        --subnet="$subnet" \
        --ip-range="$ip_range" \
        -o parent="$parent" \
        "$net"
    fi
  done
}

# ---------------------------------------------------------------------------
# Step 3: Install restic
# ---------------------------------------------------------------------------
install_restic() {
  if command -v restic &>/dev/null; then
    log "restic already installed: $(restic version)"
  else
    log "Installing restic..."
    apt-get update -qq
    apt-get install -y -qq restic
    log "restic installed: $(restic version)"
  fi
}

# ---------------------------------------------------------------------------
# Step 4: Restore persistent data from Backblaze B2
# ---------------------------------------------------------------------------
restore_from_backup() {
  if [[ "$MODE" != "restore" ]]; then
    log "Skipping restore (--fresh mode)."
    return
  fi

  log ""
  log "=== Data Restore from Backblaze B2 ==="
  log ""
  log "Restoring persistent data including GitLab, Vault, and service configs."
  log ""

  # Prompt for credentials if not already set
  prompt_var B2_ACCOUNT_ID "B2_ACCOUNT_ID"
  prompt_var B2_ACCOUNT_KEY "B2_ACCOUNT_KEY" secret
  prompt_var RESTIC_REPOSITORY "RESTIC_REPOSITORY [s3:s3.us-east-005.backblazeb2.com/nkontur-homelab]"
  RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-s3:s3.us-east-005.backblazeb2.com/nkontur-homelab}"
  export RESTIC_REPOSITORY
  prompt_var RESTIC_PASSWORD "RESTIC_PASSWORD" secret

  export AWS_ACCESS_KEY_ID="${B2_ACCOUNT_ID}"
  export AWS_SECRET_ACCESS_KEY="${B2_ACCOUNT_KEY}"

  log "Connecting to backup repository..."
  if ! restic snapshots --latest 5; then
    die "Failed to connect to backup repository. Check credentials."
  fi

  log ""
  log "Restore paths (in recommended order):"
  log "  1. /persistent_data/application   — Service configs (GitLab, HA, Vault, etc.)"
  log "  2. /persistent_data/docker/volumes — Docker volumes (databases)"
  log "  3. /mpool/nextcloud               — Nextcloud data (large)"
  log "  4. /mpool/plex/config             — Plex metadata"
  log "  5. /mpool/plex/Photos             — Photos (large, optional)"
  log "  6. /mpool/plex/Family             — Family videos (large, optional)"
  log ""
  warn "This will OVERWRITE existing files at the restore paths."

  read -rp "[bootstrap] Restore /persistent_data/application (service configs incl. GitLab + Vault)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn]$ ]]; then
    log "Restoring /persistent_data/application..."
    restic restore latest --target / --include /persistent_data/application --verbose
    log "Done."
  fi

  read -rp "[bootstrap] Restore /persistent_data/docker/volumes (databases incl. Vault data)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn]$ ]]; then
    log "Restoring /persistent_data/docker/volumes..."
    restic restore latest --target / --include /persistent_data/docker/volumes --verbose
    log "Done."
  fi

  read -rp "[bootstrap] Restore /mpool/nextcloud (Nextcloud data)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /mpool/nextcloud (this may take a while)..."
    restic restore latest --target / --include /mpool/nextcloud --verbose
    log "Done."
  fi

  read -rp "[bootstrap] Restore /mpool/plex (config + media metadata)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /mpool/plex/config..."
    restic restore latest --target / --include /mpool/plex/config --verbose
    log "Done."
  fi

  log ""
  log "Restore complete."
}

# ---------------------------------------------------------------------------
# Step 5: Start GitLab container
# ---------------------------------------------------------------------------
start_gitlab() {
  if docker ps --format '{{.Names}}' | grep -q "^${GITLAB_CONTAINER}$"; then
    log "GitLab container '${GITLAB_CONTAINER}' is already running."
    return
  fi

  docker rm -f "${GITLAB_CONTAINER}" 2>/dev/null || true

  mkdir -p "${GITLAB_DATA}/config" "${GITLAB_DATA}/logs" "${GITLAB_DATA}/data"

  log "Starting GitLab container..."
  docker run -d \
    --name "${GITLAB_CONTAINER}" \
    --restart unless-stopped \
    --network internal \
    --memory 8g \
    -p "${GITLAB_HTTP_PORT}:80" \
    -v "${GITLAB_DATA}/config:/etc/gitlab" \
    -v "${GITLAB_DATA}/logs:/var/log/gitlab" \
    -v "${GITLAB_DATA}/data:/var/opt/gitlab" \
    "${GITLAB_IMAGE}"

  log "GitLab container started."
}

# ---------------------------------------------------------------------------
# Step 6: Wait for GitLab to be healthy
# ---------------------------------------------------------------------------
wait_for_gitlab() {
  local max_wait=600
  local interval=15
  local elapsed=0

  log "Waiting for GitLab to become healthy (up to ${max_wait}s)..."

  while [[ $elapsed -lt $max_wait ]]; do
    if docker exec "${GITLAB_CONTAINER}" curl -sf http://localhost:80/-/health &>/dev/null; then
      log "GitLab is healthy after ${elapsed}s."
      return
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
    log "  ... still waiting (${elapsed}s elapsed)"
  done

  warn "GitLab did not become healthy within ${max_wait}s."
  warn "Check logs: docker logs ${GITLAB_CONTAINER}"
}

# ---------------------------------------------------------------------------
# Step 7: Install gitlab-runner
# ---------------------------------------------------------------------------
install_runner() {
  if command -v gitlab-runner &>/dev/null; then
    log "gitlab-runner already installed: $(gitlab-runner --version 2>&1 | head -1)"
    return
  fi

  log "Installing gitlab-runner..."
  curl -fsSL "https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh" | bash
  apt-get install -y -qq gitlab-runner
  log "gitlab-runner installed: $(gitlab-runner --version 2>&1 | head -1)"
}

# ---------------------------------------------------------------------------
# Step 8: Start Vault container
# ---------------------------------------------------------------------------
start_vault() {
  if docker ps --format '{{.Names}}' | grep -q "^${VAULT_CONTAINER}$"; then
    log "Vault container '${VAULT_CONTAINER}' is already running."
    return
  fi

  docker rm -f "${VAULT_CONTAINER}" 2>/dev/null || true

  mkdir -p "${VAULT_DATA}/config" "${VAULT_DATA}/scripts" "${VAULT_DATA}/unseal"
  mkdir -p "${PERSISTENT_DATA}/certs"

  # For fresh mode, we don't use auto-unseal — we need to capture init output.
  # For restore mode, the auto-unseal script and unseal keys should already exist.
  local entrypoint_args=()
  if [[ "$MODE" == "fresh" ]]; then
    entrypoint_args=(--entrypoint vault)
    local cmd_args=(server -config=/vault/config/config.hcl)
  else
    entrypoint_args=(--entrypoint /bin/sh)
    local cmd_args=(/vault/scripts/auto-unseal.sh)
  fi

  log "Starting Vault container..."
  docker run -d \
    --name "${VAULT_CONTAINER}" \
    --restart unless-stopped \
    --network internal \
    --ip "${VAULT_IP}" \
    --memory 768m \
    --cap-add IPC_LOCK \
    -p 8200:8200 \
    -v "${VAULT_DATA}/config:/vault/config:ro" \
    -v "${PERSISTENT_DATA}/certs:/vault/certs:ro" \
    -v "${VAULT_DATA}/scripts/auto-unseal.sh:/vault/scripts/auto-unseal.sh:ro" \
    -v "${VAULT_DATA}/unseal:/vault/unseal:ro" \
    -v vault_data:/vault/file \
    -e "VAULT_ADDR=https://127.0.0.1:8200" \
    -e "VAULT_API_ADDR=https://${VAULT_IP}:8200" \
    -e "VAULT_SKIP_VERIFY=true" \
    -e "VAULT_UNSEAL_KEYS_FILE=/vault/unseal/unseal-keys" \
    "${entrypoint_args[@]}" \
    "${VAULT_IMAGE}" \
    "${cmd_args[@]}"

  log "Vault container started. Waiting for API..."

  # Wait for Vault API (sealed=exit 2 or unsealed=exit 0 both mean API is up)
  local max_wait=120 elapsed=0 interval=5
  while [[ $elapsed -lt $max_wait ]]; do
    local rc=0
    docker exec "${VAULT_CONTAINER}" vault status &>/dev/null || rc=$?
    if [[ $rc -eq 0 || $rc -eq 2 ]]; then
      log "Vault API is ready after ${elapsed}s."
      return
    fi
    sleep "$interval"
    elapsed=$((elapsed + interval))
  done

  warn "Vault API did not become available within ${max_wait}s."
  warn "Check logs: docker logs ${VAULT_CONTAINER}"
}

# ---------------------------------------------------------------------------
# Step 9: Bootstrap Vault
# ---------------------------------------------------------------------------
bootstrap_vault() {
  local vault_exec="docker exec ${VAULT_CONTAINER} vault"

  if [[ "$MODE" == "restore" ]]; then
    bootstrap_vault_restore "$vault_exec"
  else
    bootstrap_vault_fresh "$vault_exec"
  fi
}

bootstrap_vault_restore() {
  local vault_exec="$1"
  log "=== Vault Bootstrap (restore mode) ==="
  log "Vault data was restored from backup — verifying status..."

  # Check if already unsealed (auto-unseal script should handle this)
  local rc=0
  $vault_exec status &>/dev/null || rc=$?
  if [[ $rc -eq 0 ]]; then
    log "Vault is already unsealed. ✓"
    return
  fi

  if [[ $rc -eq 2 ]]; then
    log "Vault is sealed. Attempting unseal..."
    # If unseal keys file exists in the restored data, auto-unseal should work.
    # If not, prompt the user.
    if [[ -f "${VAULT_DATA}/unseal/unseal-keys" ]]; then
      log "Unseal keys file found in restored data. Restarting to trigger auto-unseal..."
      docker restart "${VAULT_CONTAINER}"
      sleep 15
      rc=0
      $vault_exec status &>/dev/null || rc=$?
      if [[ $rc -eq 0 ]]; then
        log "Vault unsealed successfully via auto-unseal. ✓"
        return
      fi
    fi

    # Manual unseal fallback
    log "Auto-unseal did not succeed. Manual unseal required."
    local i
    for i in 1 2 3; do
      local key
      read -rsp "[bootstrap] Unseal key ${i} of 3: " key
      echo
      $vault_exec operator unseal "$key" >/dev/null 2>&1 || true
      rc=0
      $vault_exec status &>/dev/null || rc=$?
      if [[ $rc -eq 0 ]]; then
        log "Vault unsealed after ${i} key(s). ✓"
        return
      fi
    done

    die "Failed to unseal Vault after 3 keys."
  else
    die "Vault is not initialized or API is unreachable (exit code: $rc)."
  fi
}

bootstrap_vault_fresh() {
  local vault_exec="$1"
  log "=== Vault Bootstrap (fresh mode) ==="

  # Check if Vault is already initialized
  local rc=0
  $vault_exec status &>/dev/null || rc=$?
  if [[ $rc -eq 0 ]]; then
    log "Vault is already initialized and unsealed. Skipping init."
    return
  fi

  # rc=2 means sealed (already initialized), rc=1 means not initialized
  if [[ $rc -eq 2 ]]; then
    log "Vault is already initialized but sealed. Prompting for unseal keys..."
    local i
    for i in 1 2 3; do
      local key
      read -rsp "[bootstrap] Unseal key ${i} of 3: " key
      echo
      $vault_exec operator unseal "$key" >/dev/null 2>&1 || true
      local check_rc=0
      $vault_exec status &>/dev/null || check_rc=$?
      if [[ $check_rc -eq 0 ]]; then
        log "Vault unsealed. ✓"
        return
      fi
    done
    die "Failed to unseal Vault."
  fi

  # Initialize Vault
  log "Initializing Vault (5 key shares, 3 key threshold)..."
  local init_output
  init_output=$($vault_exec operator init -key-shares=5 -key-threshold=3 -format=json)

  # Extract keys and root token
  VAULT_UNSEAL_KEYS_JSON="$init_output"
  VAULT_ROOT_TOKEN=$(echo "$init_output" | python3 -c "import sys,json; print(json.load(sys.stdin)['root_token'])" 2>/dev/null) \
    || VAULT_ROOT_TOKEN=$(echo "$init_output" | grep -o '"root_token":"[^"]*"' | cut -d'"' -f4)

  local keys_b64
  keys_b64=$(echo "$init_output" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for k in data.get('unseal_keys_b64', []):
    print(k)
" 2>/dev/null)

  local keys_hex
  keys_hex=$(echo "$init_output" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for k in data.get('unseal_keys_hex', []):
    print(k)
" 2>/dev/null)

  echo ""
  echo "╔══════════════════════════════════════════════════════════════════╗"
  echo "║                    ⚠️  VAULT INITIALIZED  ⚠️                     ║"
  echo "║                                                                  ║"
  echo "║  SAVE THESE VALUES IMMEDIATELY. THEY CANNOT BE RECOVERED.        ║"
  echo "║  Store them in 1Password, a safe deposit box, or encrypted       ║"
  echo "║  cloud storage. DO NOT rely solely on this machine.              ║"
  echo "╚══════════════════════════════════════════════════════════════════╝"
  echo ""
  echo "Root Token: ${VAULT_ROOT_TOKEN}"
  echo ""
  echo "Unseal Keys (base64):"
  echo "$keys_b64" | nl -ba
  echo ""
  echo "Unseal Keys (hex):"
  echo "$keys_hex" | nl -ba
  echo ""

  # Save unseal keys for auto-unseal (hex format, 3 of 5)
  mkdir -p "${VAULT_DATA}/unseal"
  echo "$keys_hex" | head -3 > "${VAULT_DATA}/unseal/unseal-keys"
  chmod 600 "${VAULT_DATA}/unseal/unseal-keys"
  log "Saved 3 unseal keys to ${VAULT_DATA}/unseal/unseal-keys for auto-unseal."

  # Unseal Vault
  log "Unsealing Vault..."
  local i=0
  while IFS= read -r key; do
    [[ -z "$key" ]] && continue
    i=$((i + 1))
    $vault_exec operator unseal "$key" >/dev/null 2>&1
    local check_rc=0
    $vault_exec status &>/dev/null || check_rc=$?
    if [[ $check_rc -eq 0 ]]; then
      log "Vault unsealed after ${i} key(s). ✓"
      break
    fi
  done <<< "$keys_hex"

  # Verify
  rc=0
  $vault_exec status &>/dev/null || rc=$?
  [[ $rc -ne 0 ]] && die "Vault failed to unseal after init."

  # Now configure Vault (fresh install)
  export VAULT_TOKEN="$VAULT_ROOT_TOKEN"
  setup_vault_fresh "$vault_exec"
}

setup_vault_fresh() {
  local vault_exec="$1"

  log "Configuring fresh Vault..."

  # Enable KV v2 secrets engine at homelab/
  log "Enabling KV v2 secrets engine at homelab/..."
  $vault_exec secrets enable -path=homelab -version=2 kv 2>/dev/null \
    || log "  (already enabled)"

  # Enable AppRole auth
  log "Enabling AppRole auth backend..."
  $vault_exec auth enable approle 2>/dev/null \
    || log "  (already enabled)"

  # Enable JWT auth (for GitLab CI)
  log "Enabling JWT auth backend..."
  $vault_exec auth enable jwt 2>/dev/null \
    || log "  (already enabled)"

  # Seed secrets
  seed_vault_secrets "$vault_exec"

  # Run Terraform for policies and roles
  run_vault_terraform
}

# ---------------------------------------------------------------------------
# Vault secret seeding (fresh mode only)
# ---------------------------------------------------------------------------
seed_vault_secrets() {
  local vault_exec="$1"

  log ""
  log "=== Seeding Vault Secrets ==="
  log ""
  log "All Vault secret paths must be populated for the pipeline to work."
  log "Secrets can come from: --secrets-file, environment variables, or interactive prompts."
  log ""

  # Define all secret paths and their fields.
  # Format: "vault_path|field1,field2,..."
  local -a secret_defs=(
    "api-keys/aclawdemy|api_key"
    "api-keys/anthropic|api_key"
    "api-keys/brave|api_key"
    "api-keys/openai|api_key"
    "docker/audioserve|secret"
    "docker/deluge|password"
    "docker/grafana|admin_password,smtp_password,token"
    "docker/homeassistant|token"
    "docker/influxdb|admin_token,password,token,telegraf_token"
    "docker/nextcloud|db_password"
    "docker/nzbget|password,username"
    "docker/ombi|api_key"
    "docker/paperless|token"
    "docker/plex|token"
    "docker/prowlarr|api_key"
    "docker/radarr|api_key"
    "docker/sonarr|api_key"
    "docker/wordpress|db_password"
    "cameras/doorbell|password"
    "email/gmail|app_password,email"
    "infrastructure/ipmi|password,user"
    "infrastructure/pihole|password"
    "infrastructure/tailscale|api_token,auth_key"
    "infrastructure/luks|password_base64"
    "infrastructure/omapi|secret"
    "infrastructure/router|private_key_base64"
    "infrastructure/snmp|password"
    "infrastructure/spotify|sp_dc,sp_key"
    "mqtt/mosquitto|password"
    "moltbot/tokens|gateway_token,gitlab_token,telegram_token"
    "backup/backblaze|access_key_id,secret_access_key"
    "backup/restic|password"
    "networking/cloudflare|api_key,zone_id"
    "networking/namesilo|api_key"
  )

  for def in "${secret_defs[@]}"; do
    IFS='|' read -r path fields <<< "$def"
    IFS=',' read -ra field_arr <<< "$fields"

    log "Secret: homelab/${path}"

    # Build JSON payload
    local json_payload="{"
    local first=true
    for field in "${field_arr[@]}"; do
      # Env var name: path + field, normalized. e.g. api-keys/aclawdemy:api_key → VAULT_SEED_API_KEYS_ACLAWDEMY_API_KEY
      local env_key
      env_key="VAULT_SEED_$(echo "${path}/${field}" | tr '[:lower:]/-' '[:upper:]__')"

      local value="${!env_key:-}"

      if [[ -z "$value" ]]; then
        # Try simpler env var name (just field): useful for common secrets
        local simple_key
        simple_key="VAULT_SEED_$(echo "${field}" | tr '[:lower:]' '[:upper:]')"
        value="${!simple_key:-}"
      fi

      if [[ -z "$value" ]]; then
        read -rsp "  ${field}: " value
        echo
      fi

      if [[ -z "$value" ]]; then
        warn "  Empty value for ${field} — will be stored as empty string."
      fi

      # Escape value for JSON
      local escaped_value
      escaped_value=$(printf '%s' "$value" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()), end='')" 2>/dev/null) \
        || escaped_value="\"$(printf '%s' "$value" | sed 's/\\/\\\\/g; s/"/\\"/g')\""

      if [[ "$first" == "true" ]]; then
        first=false
      else
        json_payload+=","
      fi
      json_payload+="\"${field}\":${escaped_value}"
    done
    json_payload+="}"

    # Write to Vault
    $vault_exec kv put "homelab/${path}" - <<< "$json_payload" >/dev/null 2>&1 \
      && log "  ✓ Written." \
      || warn "  Failed to write homelab/${path}. You may need to set this manually."
  done

  log ""
  log "Secret seeding complete."
}

# ---------------------------------------------------------------------------
# Run Terraform for Vault policies and roles
# ---------------------------------------------------------------------------
run_vault_terraform() {
  log ""
  log "=== Applying Vault Terraform (policies, roles, auth) ==="
  log ""

  # Check for Terraform
  if ! command -v terraform &>/dev/null; then
    log "Installing Terraform..."
    local tf_version="1.7.5"
    wget -q "https://releases.hashicorp.com/terraform/${tf_version}/terraform_${tf_version}_linux_amd64.zip" -O /tmp/terraform.zip
    unzip -o /tmp/terraform.zip -d /usr/local/bin/
    chmod +x /usr/local/bin/terraform
    rm -f /tmp/terraform.zip
  fi

  cd "$(dirname "$(readlink -f "$0")")/../terraform/vault"

  # For bootstrap, use a local backend (no GitLab state backend available yet for fresh)
  log "Initializing Terraform with local backend..."
  cat > /tmp/vault-bootstrap-override.tf <<'EOF'
terraform {
  backend "local" {
    path = "/tmp/vault-bootstrap-terraform.tfstate"
  }
}
EOF

  # Copy override into the terraform dir temporarily
  cp /tmp/vault-bootstrap-override.tf ./override.tf

  terraform init -reconfigure
  terraform apply \
    -var="vault_addr=${VAULT_ADDR}" \
    -var="vault_token=${VAULT_ROOT_TOKEN:-${VAULT_TOKEN:-}}" \
    -auto-approve

  log "Terraform apply complete. ✓"

  # Clean up override
  rm -f ./override.tf

  # Return to original directory
  cd - >/dev/null
}

# ---------------------------------------------------------------------------
# Step 10: Set CI/CD variables via GitLab API
# ---------------------------------------------------------------------------
set_cicd_variables() {
  log ""
  log "=== Setting CI/CD Variables via GitLab API ==="
  log ""

  # We need a GitLab personal access token to set CI/CD vars.
  # In restore mode, the root PAT may already exist in restored data.
  # Otherwise, prompt the user.
  prompt_var GITLAB_BOOTSTRAP_TOKEN "GitLab personal access token (with api scope)" secret

  local gitlab_api="http://localhost:${GITLAB_HTTP_PORT}/api/v4"

  # Verify API access
  log "Verifying GitLab API access..."
  local api_check
  api_check=$(curl -s -o /dev/null -w '%{http_code}' \
    -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
    "${gitlab_api}/projects/${PROJECT_ID}") || true
  if [[ "$api_check" != "200" ]]; then
    die "GitLab API returned HTTP ${api_check}. Check your token and project ID (${PROJECT_ID})."
  fi
  log "GitLab API accessible. ✓"

  # Determine values for each CI/CD variable
  local vault_token_val=""
  local vault_role_id=""
  local vault_secret_id=""
  local vault_unseal_keys_val=""

  if [[ "$MODE" == "fresh" ]]; then
    vault_token_val="${VAULT_ROOT_TOKEN}"

    # Get role_id from Vault (Terraform should have created the approle)
    local vault_exec="docker exec ${VAULT_CONTAINER} vault"
    vault_role_id=$($vault_exec read -field=role_id auth/approle/role/moltbot/role-id 2>/dev/null) || true

    # Generate a secret_id
    vault_secret_id=$($vault_exec write -field=secret_id -f auth/approle/role/moltbot/secret-id 2>/dev/null) || true

    # Unseal keys (hex, newline-separated) from the init output
    vault_unseal_keys_val=$(echo "$VAULT_UNSEAL_KEYS_JSON" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(','.join(data.get('unseal_keys_hex', [])))
" 2>/dev/null) || true

  else
    # Restore mode — prompt for values or read from Vault
    prompt_var VAULT_TOKEN "Vault root/admin token" secret
    vault_token_val="${VAULT_TOKEN}"

    # Try to read from a running Vault
    local vault_exec="docker exec -e VAULT_TOKEN=${vault_token_val} ${VAULT_CONTAINER} vault"
    vault_role_id=$($vault_exec read -field=role_id auth/approle/role/moltbot/role-id 2>/dev/null) || true
    if [[ -z "$vault_role_id" ]]; then
      prompt_var VAULT_APPROLE_ROLE_ID "Vault AppRole Role ID"
      vault_role_id="${VAULT_APPROLE_ROLE_ID}"
    fi

    vault_secret_id=$($vault_exec write -field=secret_id -f auth/approle/role/moltbot/secret-id 2>/dev/null) || true
    if [[ -z "$vault_secret_id" ]]; then
      prompt_var VAULT_APPROLE_SECRET_ID "Vault AppRole Secret ID" secret
      vault_secret_id="${VAULT_APPROLE_SECRET_ID}"
    fi

    # Unseal keys
    if [[ -f "${VAULT_DATA}/unseal/unseal-keys" ]]; then
      vault_unseal_keys_val=$(paste -sd',' "${VAULT_DATA}/unseal/unseal-keys")
    else
      prompt_var VAULT_UNSEAL_KEYS "Vault unseal keys (comma-separated hex)" secret
      vault_unseal_keys_val="${VAULT_UNSEAL_KEYS}"
    fi
  fi

  # Helper to set or update a CI/CD variable
  set_ci_var() {
    local key="$1" value="$2" masked="${3:-true}" protected="${4:-false}"
    local data
    data=$(python3 -c "
import json, sys
print(json.dumps({
    'key': sys.argv[1],
    'value': sys.argv[2],
    'masked': sys.argv[3] == 'true',
    'protected': sys.argv[4] == 'true',
    'variable_type': 'env_var'
}))
" "$key" "$value" "$masked" "$protected")

    # Try PUT (update) first, then POST (create)
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
      -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$data" \
      "${gitlab_api}/projects/${PROJECT_ID}/variables/${key}") || true

    if [[ "$code" == "200" ]]; then
      log "  Updated: ${key} ✓"
      return
    fi

    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
      -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$data" \
      "${gitlab_api}/projects/${PROJECT_ID}/variables") || true

    if [[ "$code" == "201" ]]; then
      log "  Created: ${key} ✓"
    else
      warn "  Failed to set ${key} (HTTP ${code}). Set it manually in GitLab."
    fi
  }

  log "Setting CI/CD variables on project ${PROJECT_ID}..."
  set_ci_var "VAULT_ADDR"              "${VAULT_ADDR}"              "false"  "false"
  set_ci_var "VAULT_TOKEN"             "${vault_token_val}"         "true"   "true"
  set_ci_var "VAULT_APPROLE_ROLE_ID"   "${vault_role_id}"           "true"   "false"
  set_ci_var "VAULT_APPROLE_SECRET_ID" "${vault_secret_id}"         "true"   "false"
  set_ci_var "VAULT_UNSEAL_KEYS"       "${vault_unseal_keys_val}"   "true"   "true"

  # Runner token is set in step 11 after registration
  log "CI/CD variables set. ✓"
}

# ---------------------------------------------------------------------------
# Step 11: Register GitLab runner
# ---------------------------------------------------------------------------
register_runner() {
  # Check if runner is already registered
  if gitlab-runner list 2>&1 | grep -q "https://gitlab.lab.nkontur.com"; then
    log "Runner already registered. ✓"
    # Update the CI variable with the existing token
    local existing_token
    existing_token=$(grep -m1 'token' /etc/gitlab-runner/config.toml 2>/dev/null | sed 's/.*= "//;s/"//' || true)
    if [[ -n "$existing_token" && -n "${GITLAB_BOOTSTRAP_TOKEN:-}" ]]; then
      set_ci_var "GITLAB_RUNNER_TOKEN" "$existing_token" "true" "false"
    fi
    return
  fi

  log "Registering GitLab runner..."

  # Need a runner registration token from GitLab
  # In newer GitLab (16+), use the Runners API
  local gitlab_api="http://localhost:${GITLAB_HTTP_PORT}/api/v4"

  # Try to create a project runner via API
  local runner_response
  runner_response=$(curl -s -X POST \
    -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{
      "runner_type": "project_type",
      "project_id": '"${PROJECT_ID}"',
      "description": "router-runner",
      "run_untagged": true,
      "locked": true
    }' \
    "${gitlab_api}/user/runners") || true

  local runner_token
  runner_token=$(echo "$runner_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null) || true

  if [[ -z "$runner_token" ]]; then
    warn "Could not create runner via API. Falling back to manual registration."
    log "Get a runner registration token from GitLab → Settings → CI/CD → Runners"
    prompt_var RUNNER_REG_TOKEN "Runner registration token"
    runner_token="${RUNNER_REG_TOKEN}"
  fi

  gitlab-runner register \
    --non-interactive \
    --url "http://localhost:${GITLAB_HTTP_PORT}/" \
    --token "$runner_token" \
    --executor docker \
    --docker-image ubuntu:20.04 \
    --docker-network-mode host \
    --description "router-runner"

  log "Runner registered. ✓"

  # Store runner token as CI variable
  if [[ -n "${GITLAB_BOOTSTRAP_TOKEN:-}" ]]; then
    set_ci_var "GITLAB_RUNNER_TOKEN" "$runner_token" "true" "false"
  fi
}

# set_ci_var needs to be available in step 11 context too (redeclare for scope)
set_ci_var() {
  local key="$1" value="$2" masked="${3:-true}" protected="${4:-false}"
  local gitlab_api="http://localhost:${GITLAB_HTTP_PORT}/api/v4"
  local data
  data=$(python3 -c "
import json, sys
print(json.dumps({
    'key': sys.argv[1],
    'value': sys.argv[2],
    'masked': sys.argv[3] == 'true',
    'protected': sys.argv[4] == 'true',
    'variable_type': 'env_var'
}))
" "$key" "$value" "$masked" "$protected")

  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT \
    -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$data" \
    "${gitlab_api}/projects/${PROJECT_ID}/variables/${key}") || true

  if [[ "$code" == "200" ]]; then
    log "  Updated: ${key} ✓"
    return
  fi

  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$data" \
    "${gitlab_api}/projects/${PROJECT_ID}/variables") || true

  if [[ "$code" == "201" ]]; then
    log "  Created: ${key} ✓"
  else
    warn "  Failed to set ${key} (HTTP ${code})"
  fi
}

# ---------------------------------------------------------------------------
# Step 12: Trigger first pipeline
# ---------------------------------------------------------------------------
trigger_pipeline() {
  local gitlab_api="http://localhost:${GITLAB_HTTP_PORT}/api/v4"

  log "Triggering pipeline on main branch..."

  # Push current repo state if needed (ensure HEAD is on main)
  local pipeline_response
  pipeline_response=$(curl -s -X POST \
    -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
    -H "Content-Type: application/json" \
    -d '{"ref": "main"}' \
    "${gitlab_api}/projects/${PROJECT_ID}/pipeline") || true

  local pipeline_id
  pipeline_id=$(echo "$pipeline_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null) || true

  if [[ -z "$pipeline_id" ]]; then
    warn "Could not trigger pipeline via API."
    log "Trigger manually: push a commit or go to CI/CD → Pipelines → Run pipeline"
    return
  fi

  local pipeline_url
  pipeline_url=$(echo "$pipeline_response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('web_url',''))" 2>/dev/null) || true
  log "Pipeline #${pipeline_id} triggered."
  [[ -n "$pipeline_url" ]] && log "URL: ${pipeline_url}"

  if [[ "$SKIP_PIPELINE_WAIT" == "true" ]]; then
    log "Skipping pipeline wait (--skip-pipeline-wait)."
    return
  fi

  # Wait for pipeline to complete
  log "Waiting for pipeline to complete (this may take 10-20 minutes)..."
  local max_wait=1200 elapsed=0 interval=15
  while [[ $elapsed -lt $max_wait ]]; do
    local status
    status=$(curl -s \
      -H "PRIVATE-TOKEN: ${GITLAB_BOOTSTRAP_TOKEN}" \
      "${gitlab_api}/projects/${PROJECT_ID}/pipelines/${pipeline_id}" \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null) || true

    case "$status" in
      success)
        log "🎉 Pipeline #${pipeline_id} passed! First run is GREEN. ✓"
        return
        ;;
      failed)
        warn "Pipeline #${pipeline_id} failed."
        log "Check: ${pipeline_url:-http://localhost/root/homelab/-/pipelines/${pipeline_id}}"
        log "Fix any issues and re-run the pipeline."
        return 1
        ;;
      canceled)
        warn "Pipeline #${pipeline_id} was canceled."
        return 1
        ;;
    esac

    sleep "$interval"
    elapsed=$((elapsed + interval))
    # Exponential backoff capped at 60s
    [[ $interval -lt 60 ]] && interval=$((interval + 5))
    log "  ... pipeline status: ${status:-unknown} (${elapsed}s / ${max_wait}s)"
  done

  warn "Pipeline did not complete within ${max_wait}s. Check GitLab UI."
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "=== Homelab Bootstrap ==="
  log ""

  require_root
  prompt_mode

  # Load secrets file if provided
  load_secrets_file

  step 1 "Docker"
  install_docker

  step 2 "Networks"
  create_networks

  step 3 "Restic"
  install_restic

  step 4 "Data restore"
  restore_from_backup

  step 5 "GitLab"
  start_gitlab

  step 6 "Waiting for GitLab"
  wait_for_gitlab

  step 7 "Runner (install)"
  install_runner

  step 8 "Vault"
  start_vault

  step 9 "Vault bootstrap"
  bootstrap_vault

  step 10 "CI/CD variables"
  set_cicd_variables

  step 11 "Runner (register)"
  register_runner

  step 12 "First pipeline"
  trigger_pipeline

  log ""
  log "╔══════════════════════════════════════════════════════════════════╗"
  log "║              🎉 BOOTSTRAP COMPLETE 🎉                          ║"
  log "╚══════════════════════════════════════════════════════════════════╝"
  log ""
  log "All steps completed. Your homelab should now be fully operational."
  log ""
  if [[ "$MODE" == "fresh" ]]; then
    log "⚠️  REMINDER: Save your Vault unseal keys and root token securely!"
    log "   They were displayed during step 9 and saved to:"
    log "   ${VAULT_DATA}/unseal/unseal-keys (3 of 5 keys for auto-unseal)"
    log ""
  fi
  log "Services:"
  log "  GitLab:  http://$(hostname -I | awk '{print $1}'):${GITLAB_HTTP_PORT}"
  log "  Vault:   ${VAULT_ADDR}"
  log ""
  log "If the pipeline is still running, monitor at:"
  log "  http://localhost/root/homelab/-/pipelines"
  log ""
}

main "$@"
