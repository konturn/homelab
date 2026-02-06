#!/usr/bin/env bash
# bootstrap.sh — Pre-CI bootstrap for bare-metal to "CI pipeline works"
#
# Run directly on the router as root. Idempotent (safe to re-run).
#
# Steps (in order):
#   1. Install Docker + docker-compose plugin
#   2. Create required Docker macvlan networks
#   3. Install restic
#   4. Prompt to restore persistent data from Backblaze B2
#   5. Start GitLab container (using restored data if available)
#   6. Wait for GitLab to be healthy
#   7. Install gitlab-runner
#
# Data restore MUST happen before GitLab starts — GitLab's own data
# (repos, users, CI config) lives in the backup.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PERSISTENT_DATA="/persistent_data/application"
GITLAB_DATA="${PERSISTENT_DATA}/gitlab"
GITLAB_IMAGE="gitlab/gitlab-ee:latest"
GITLAB_CONTAINER="gitlab-bootstrap"
GITLAB_HTTP_PORT=80

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo -e "\033[1;32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap]\033[0m WARNING: $*"; }
err()  { echo -e "\033[1;31m[bootstrap]\033[0m ERROR: $*" >&2; }
die()  { err "$@"; exit 1; }

require_root() {
  [[ $EUID -eq 0 ]] || die "This script must be run as root."
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
  log ""
  log "=== Data Restore from Backblaze B2 ==="
  log ""
  log "GitLab's data (repos, users, CI config) lives in the backup."
  log "If this is a fresh install, you MUST restore before GitLab can start"
  log "with your existing projects and configuration."
  log ""

  read -rp "[bootstrap] Restore data from Backblaze B2 backup? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    log "Skipping restore. GitLab will start with empty data."
    return
  fi

  # Prompt for credentials if not already set
  if [[ -z "${B2_ACCOUNT_ID:-}" ]]; then
    read -rp "[bootstrap] B2_ACCOUNT_ID: " B2_ACCOUNT_ID
    export B2_ACCOUNT_ID
  fi
  if [[ -z "${B2_ACCOUNT_KEY:-}" ]]; then
    read -rsp "[bootstrap] B2_ACCOUNT_KEY: " B2_ACCOUNT_KEY
    echo
    export B2_ACCOUNT_KEY
  fi
  if [[ -z "${RESTIC_REPOSITORY:-}" ]]; then
    read -rp "[bootstrap] RESTIC_REPOSITORY [s3:s3.us-east-005.backblazeb2.com/nkontur-homelab]: " RESTIC_REPOSITORY
    RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-s3:s3.us-east-005.backblazeb2.com/nkontur-homelab}"
    export RESTIC_REPOSITORY
  fi
  if [[ -z "${RESTIC_PASSWORD:-}" ]]; then
    read -rsp "[bootstrap] RESTIC_PASSWORD: " RESTIC_PASSWORD
    echo
    export RESTIC_PASSWORD
  fi

  export AWS_ACCESS_KEY_ID="${B2_ACCOUNT_ID}"
  export AWS_SECRET_ACCESS_KEY="${B2_ACCOUNT_KEY}"

  log "Connecting to backup repository..."
  if ! restic snapshots --latest 5; then
    err "Failed to connect to backup repository. Check credentials."
    return 1
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

  read -rp "[bootstrap] Restore /persistent_data/application (service configs incl. GitLab)? [Y/n] " answer
  if [[ ! "$answer" =~ ^[Nn]$ ]]; then
    log "Restoring /persistent_data/application..."
    restic restore latest --target / --include /persistent_data/application --verbose
    log "Done."
  fi

  read -rp "[bootstrap] Restore /persistent_data/docker/volumes (databases)? [Y/n] " answer
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
# Main
# ---------------------------------------------------------------------------
main() {
  log "=== Homelab Bootstrap ==="
  log ""

  require_root

  log "Step 1/7: Docker"
  install_docker

  log "Step 2/7: Networks"
  create_networks

  log "Step 3/7: Restic"
  install_restic

  log "Step 4/7: Data restore"
  restore_from_backup

  log "Step 5/7: GitLab"
  start_gitlab

  log "Step 6/7: Waiting for GitLab"
  wait_for_gitlab

  log "Step 7/7: Runner"
  install_runner

  log ""
  log "=== Bootstrap Complete ==="
  log ""
  log "Next steps:"
  log "  1. If data was restored, GitLab should have your existing projects."
  log "     If not, set the root password at http://<router-ip> and create"
  log "     the 'root/homelab' project manually."
  log "  2. Register the runner (if not already registered):"
  log "       gitlab-runner register \\"
  log "         --url https://gitlab.lab.nkontur.com/ \\"
  log "         --executor docker \\"
  log "         --docker-image ubuntu:20.04 \\"
  log "         --docker-network-mode host"
  log "  3. Push this repo to GitLab:"
  log "       git remote set-url origin https://gitlab.lab.nkontur.com/root/homelab.git"
  log "       git push -u origin main"
  log "  4. Configure CI/CD variables in GitLab (Settings > CI/CD > Variables)"
  log "  5. Trigger the 'router:bootstrap' CI job (manual) to bring up core services"
  log ""
}

main "$@"
