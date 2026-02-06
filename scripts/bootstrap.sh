#!/usr/bin/env bash
# bootstrap.sh — Pre-CI bootstrap for bare-metal to "CI pipeline works"
#
# Run directly on the router as root. Idempotent (safe to re-run).
#
# What this does:
#   1. Installs Docker + docker-compose plugin
#   2. Creates required Docker macvlan networks
#   3. Templates and starts a minimal GitLab container
#   4. Waits for GitLab to be healthy
#   5. Installs gitlab-runner
#   6. Prints next steps

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PERSISTENT_DATA="/persistent_data/application"
GITLAB_DATA="${PERSISTENT_DATA}/gitlab"
GITLAB_IMAGE="gitlab/gitlab-ee:latest"
GITLAB_CONTAINER="gitlab-bootstrap"
GITLAB_HTTP_PORT=80

# Network interface for macvlan (bond0 with VLANs)
BOND_IFACE="bond0"

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
# Step 1: Install Docker
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
}

# ---------------------------------------------------------------------------
# Step 2: Install docker-compose plugin
# ---------------------------------------------------------------------------
install_compose() {
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
# Step 3: Create Docker macvlan networks
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
# Step 4: Start minimal GitLab container
# ---------------------------------------------------------------------------
start_gitlab() {
  if docker ps --format '{{.Names}}' | grep -q "^${GITLAB_CONTAINER}$"; then
    log "GitLab container '${GITLAB_CONTAINER}' is already running."
    return
  fi

  # Remove stopped container if it exists
  docker rm -f "${GITLAB_CONTAINER}" 2>/dev/null || true

  # Ensure data directories exist
  mkdir -p "${GITLAB_DATA}/config" "${GITLAB_DATA}/logs" "${GITLAB_DATA}/data"

  log "Starting minimal GitLab container..."
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
# Step 5: Wait for GitLab to be healthy
# ---------------------------------------------------------------------------
wait_for_gitlab() {
  local max_wait=600  # 10 minutes
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
# Step 6: Install gitlab-runner
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
# Step 7: Optional data restore from Backblaze B2
# ---------------------------------------------------------------------------
install_restic() {
  if command -v restic &>/dev/null; then
    log "restic already installed: $(restic version)"
    return
  fi

  log "Installing restic..."
  apt-get update -qq
  apt-get install -y -qq restic
  log "restic installed: $(restic version)"
}

restore_from_backup() {
  log ""
  log "=== Optional: Restore Data from Backblaze B2 ==="
  log ""
  log "If this is a fresh install, you may need to restore persistent data"
  log "(GitLab data, configs, databases) before GitLab can serve the repo."
  log ""

  read -rp "[bootstrap] Restore data from Backblaze B2 backup? [y/N] " answer
  if [[ ! "$answer" =~ ^[Yy]$ ]]; then
    log "Skipping restore."
    return
  fi

  install_restic

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

  # B2 uses S3-compatible API for restic
  export AWS_ACCESS_KEY_ID="${B2_ACCOUNT_ID}"
  export AWS_SECRET_ACCESS_KEY="${B2_ACCOUNT_KEY}"

  log "Connecting to backup repository..."
  if ! restic snapshots --latest 5; then
    err "Failed to connect to backup repository. Check credentials."
    return 1
  fi

  log ""
  log "The following restore paths are available (in recommended order):"
  log "  1. /persistent_data/application  — Service configs (GitLab, HA, Vault, etc.)"
  log "  2. /persistent_data/docker/volumes — Docker volumes (databases)"
  log "  3. /mpool/nextcloud              — Nextcloud data (large)"
  log "  4. /mpool/plex/config            — Plex metadata"
  log "  5. /mpool/plex/Photos            — Photos (large, optional)"
  log "  6. /mpool/plex/Family            — Family videos (large, optional)"
  log ""
  warn "This will OVERWRITE existing files at the restore paths."
  read -rp "[bootstrap] Restore /persistent_data/application (service configs)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /persistent_data/application..."
    restic restore latest --target / --include /persistent_data/application
    log "Done."
  fi

  read -rp "[bootstrap] Restore /persistent_data/docker/volumes (databases)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /persistent_data/docker/volumes..."
    restic restore latest --target / --include /persistent_data/docker/volumes
    log "Done."
  fi

  read -rp "[bootstrap] Restore /mpool/nextcloud (Nextcloud data)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /mpool/nextcloud (this may take a while)..."
    restic restore latest --target / --include /mpool/nextcloud
    log "Done."
  fi

  read -rp "[bootstrap] Restore /mpool/plex (config + media metadata)? [y/N] " answer
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    log "Restoring /mpool/plex/config..."
    restic restore latest --target / --include /mpool/plex/config
    log "Done."
  fi

  log "Restore complete. Restart containers to pick up restored data:"
  log "  cd ${PERSISTENT_DATA}/ansible_state && docker compose down && docker compose up -d"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "=== Homelab Bootstrap ==="
  log ""

  require_root
  install_docker
  install_compose
  create_networks

  # Offer restore before starting GitLab — restored data includes GitLab config
  restore_from_backup

  start_gitlab
  wait_for_gitlab
  install_runner

  log ""
  log "=== Bootstrap Complete ==="
  log ""
  log "Next steps:"
  log "  1. Set the GitLab root password (first login at http://<router-ip>)"
  log "  2. Create the 'root/homelab' project in GitLab"
  log "  3. Register the runner:"
  log "       gitlab-runner register \\"
  log "         --url https://gitlab.lab.nkontur.com/ \\"
  log "         --executor docker \\"
  log "         --docker-image ubuntu:20.04 \\"
  log "         --docker-network-mode host"
  log "  4. Push this repo to GitLab:"
  log "       git remote add origin https://gitlab.lab.nkontur.com/root/homelab.git"
  log "       git push -u origin main"
  log "  5. Configure CI/CD variables in GitLab (Settings > CI/CD > Variables)"
  log "  6. Trigger the 'router:bootstrap' CI job (manual) to bring up core services"
  log ""
}

main "$@"
