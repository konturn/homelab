#!/bin/bash
# check-updates.sh — Check Docker images in docker-compose for newer versions
# Output: JSON array of {image, current_tag, current_digest, latest_tag, latest_digest, update_type}
# Deterministic, no LLM needed. Designed for cron consumption.

set -euo pipefail

REPO_DIR="${HOMELAB_REPO:-/home/node/.openclaw/workspace/homelab}"
COMPOSE_FILE="$REPO_DIR/docker/docker-compose.yml"
STATE_FILE="${STATE_FILE:-/home/node/.openclaw/workspace/memory/image-update-state.json}"

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"lastCheck":null,"lastMR":null,"suppressedImages":[],"neverAutoMerge":["moltbot-gateway","moltbot"]}' > "$STATE_FILE"
fi

# Extract image references from docker-compose (Jinja2 template — grep for image:)
# Handles: image: name:tag, image: name:tag@sha256:..., image: name (implies :latest), image: "{{ var }}"
extract_images() {
  grep -E '^\s+image:' "$COMPOSE_FILE" \
    | sed 's/.*image:\s*//' \
    | sed 's/["'"'"']//g' \
    | sed 's/\s*#.*//' \
    | grep -v '{{' \
    | sort -u
}

# Get Docker Hub auth token for a repo
get_docker_hub_token() {
  local repo="$1"
  curl -sf --max-time 10 \
    "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" 2>/dev/null \
    | jq -r '.token // empty'
}

# Get digest for a specific tag from Docker Hub
get_docker_hub_digest() {
  local repo="$1"
  local tag="$2"
  local token
  token=$(get_docker_hub_token "$repo")
  [ -z "$token" ] && echo "" && return

  # Try multi-arch manifest first, then regular
  local digest
  digest=$(curl -sf --max-time 10 \
    -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json" \
    -I "https://registry-1.docker.io/v2/${repo}/manifests/${tag}" 2>/dev/null \
    | grep -i 'docker-content-digest' | awk '{print $2}' | tr -d '\r')

  echo "$digest"
}

# Get latest tag from Docker Hub API (v2)
get_latest_docker_hub() {
  local image="$1"
  local repo
  if [[ "$image" == *"/"* ]]; then
    repo="$image"
  else
    repo="library/$image"
  fi

  local response
  response=$(curl -sf --max-time 10 \
    "https://hub.docker.com/v2/repositories/${repo}/tags?page_size=25&ordering=last_updated" 2>/dev/null) || echo ""

  if [ -z "$response" ]; then
    echo ""
    return
  fi

  echo "$response" | jq -r '
    [.results[]
     | select(.name != "latest")
     | select(.name | test("^[0-9]"))
     | select(.name | test("rc|alpha|beta|dev|nightly|edge") | not)
     | .name
    ] | first // empty'
}

# Get latest tag from GHCR
get_latest_ghcr() {
  local image="$1"
  local token
  token=$(curl -sf "https://ghcr.io/token?scope=repository:${image}:pull" 2>/dev/null | jq -r '.token // empty')

  if [ -z "$token" ]; then
    echo ""
    return
  fi

  curl -sf -H "Authorization: Bearer $token" \
    "https://ghcr.io/v2/${image}/tags/list" 2>/dev/null \
    | jq -r '[.tags[] | select(test("^[0-9]")) | select(test("rc|alpha|beta|dev") | not)] | sort_by(. | split(".") | map(tonumber? // 999)) | last // empty'
}

# Get GHCR digest
get_ghcr_digest() {
  local image="$1"
  local tag="$2"
  local token
  token=$(curl -sf "https://ghcr.io/token?scope=repository:${image}:pull" 2>/dev/null | jq -r '.token // empty')
  [ -z "$token" ] && echo "" && return

  curl -sf --max-time 10 \
    -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json" \
    -I "https://ghcr.io/v2/${image}/manifests/${tag}" 2>/dev/null \
    | grep -i 'docker-content-digest' | awk '{print $2}' | tr -d '\r'
}

# Determine update type: patch, minor, major
classify_update() {
  local current="$1"
  local latest="$2"

  current="${current#v}"
  latest="${latest#v}"

  local cur_major cur_minor cur_patch
  local lat_major lat_minor lat_patch

  IFS='.' read -r cur_major cur_minor cur_patch <<< "$current"
  IFS='.' read -r lat_major lat_minor lat_patch <<< "$latest"

  cur_major="${cur_major%%[!0-9]*}"; cur_minor="${cur_minor%%[!0-9]*}"; cur_patch="${cur_patch%%[!0-9]*}"
  lat_major="${lat_major%%[!0-9]*}"; lat_minor="${lat_minor%%[!0-9]*}"; lat_patch="${lat_patch%%[!0-9]*}"

  cur_major="${cur_major:-0}"; cur_minor="${cur_minor:-0}"; cur_patch="${cur_patch:-0}"
  lat_major="${lat_major:-0}"; lat_minor="${lat_minor:-0}"; lat_patch="${lat_patch:-0}"

  if [ "${lat_major}" -gt "${cur_major}" ] 2>/dev/null; then
    echo "major"
  elif [ "${lat_minor}" -gt "${cur_minor}" ] 2>/dev/null; then
    echo "minor"
  elif [ "${lat_patch}" -gt "${cur_patch}" ] 2>/dev/null; then
    echo "patch"
  else
    echo "unknown"
  fi
}

# Main
cd "$REPO_DIR"
git pull origin main --quiet 2>/dev/null || true

suppressed=$(jq -r '.suppressedImages[]' "$STATE_FILE" 2>/dev/null)
never_auto_merge=$(jq -r '.neverAutoMerge[]' "$STATE_FILE" 2>/dev/null)
updates="[]"

while IFS= read -r image_ref; do
  # Strip existing digest if present
  image_no_digest="${image_ref%%@*}"

  # Split image:tag
  image="${image_no_digest%%:*}"
  current_tag="${image_no_digest#*:}"

  # Extract current digest if present
  current_digest=""
  if [[ "$image_ref" == *"@sha256:"* ]]; then
    current_digest="${image_ref#*@}"
  fi

  if [ "$image" = "$current_tag" ]; then
    current_tag="latest"
  fi

  # Skip suppressed images
  if echo "$suppressed" | grep -qF "$image"; then
    continue
  fi

  # Check if this is a never-auto-merge image
  no_auto_merge="false"
  if echo "$never_auto_merge" | grep -qF "$(basename "$image")"; then
    no_auto_merge="true"
  fi

  # Determine registry
  registry="dockerhub"
  if [[ "$image" == ghcr.io/* ]]; then
    registry="ghcr"
  elif [[ "$image" == *".io/"* ]] || [[ "$image" == *".com/"* ]]; then
    # Custom/private registry — skip
    continue
  fi

  # For :latest images, just get the digest for pinning
  if [ "$current_tag" = "latest" ]; then
    latest_digest=""
    if [ "$registry" = "dockerhub" ]; then
      repo="$image"
      [[ "$image" != *"/"* ]] && repo="library/$image"
      latest_digest=$(get_docker_hub_digest "$repo" "latest")
    elif [ "$registry" = "ghcr" ]; then
      latest_digest=$(get_ghcr_digest "${image#ghcr.io/}" "latest")
    fi

    # Also try to find the actual version tag
    actual_tag=""
    if [ "$registry" = "dockerhub" ]; then
      actual_tag=$(get_latest_docker_hub "$image")
    elif [ "$registry" = "ghcr" ]; then
      actual_tag=$(get_latest_ghcr "${image#ghcr.io/}")
    fi

    updates=$(echo "$updates" | jq \
      --arg img "$image" \
      --arg cur "latest" \
      --arg curdig "${current_digest}" \
      --arg lat "${actual_tag:-latest}" \
      --arg latdig "${latest_digest}" \
      --arg noauto "$no_auto_merge" \
      '. += [{"image": $img, "current_tag": $cur, "current_digest": $curdig, "latest_tag": $lat, "latest_digest": $latdig, "update_type": "unpinned", "never_auto_merge": ($noauto == "true")}]')
    continue
  fi

  # Fetch latest version
  latest_tag=""
  if [ "$registry" = "dockerhub" ]; then
    latest_tag=$(get_latest_docker_hub "$image")
  elif [ "$registry" = "ghcr" ]; then
    latest_tag=$(get_latest_ghcr "${image#ghcr.io/}")
  fi

  if [ -z "$latest_tag" ]; then
    # Can't determine latest, but still get digest for current tag if missing
    if [ -z "$current_digest" ]; then
      if [ "$registry" = "dockerhub" ]; then
        repo="$image"
        [[ "$image" != *"/"* ]] && repo="library/$image"
        current_digest=$(get_docker_hub_digest "$repo" "$current_tag")
      elif [ "$registry" = "ghcr" ]; then
        current_digest=$(get_ghcr_digest "${image#ghcr.io/}" "$current_tag")
      fi

      if [ -n "$current_digest" ]; then
        updates=$(echo "$updates" | jq \
          --arg img "$image" \
          --arg cur "$current_tag" \
          --arg curdig "$current_digest" \
          --arg noauto "$no_auto_merge" \
          '. += [{"image": $img, "current_tag": $cur, "current_digest": "", "latest_tag": $cur, "latest_digest": $curdig, "update_type": "needs_pin", "never_auto_merge": ($noauto == "true")}]')
      fi
    fi
    continue
  fi

  if [ "$current_tag" = "$latest_tag" ]; then
    # Same tag — check if digest pin is missing
    if [ -z "$current_digest" ]; then
      if [ "$registry" = "dockerhub" ]; then
        repo="$image"
        [[ "$image" != *"/"* ]] && repo="library/$image"
        digest=$(get_docker_hub_digest "$repo" "$current_tag")
      elif [ "$registry" = "ghcr" ]; then
        digest=$(get_ghcr_digest "${image#ghcr.io/}" "$current_tag")
      fi

      if [ -n "$digest" ]; then
        updates=$(echo "$updates" | jq \
          --arg img "$image" \
          --arg cur "$current_tag" \
          --arg dig "$digest" \
          --arg noauto "$no_auto_merge" \
          '. += [{"image": $img, "current_tag": $cur, "current_digest": "", "latest_tag": $cur, "latest_digest": $dig, "update_type": "needs_pin", "never_auto_merge": ($noauto == "true")}]')
      fi
    fi
    continue
  fi

  update_type=$(classify_update "$current_tag" "$latest_tag")

  # Get digest for the latest tag
  latest_digest=""
  if [ "$registry" = "dockerhub" ]; then
    repo="$image"
    [[ "$image" != *"/"* ]] && repo="library/$image"
    latest_digest=$(get_docker_hub_digest "$repo" "$latest_tag")
  elif [ "$registry" = "ghcr" ]; then
    latest_digest=$(get_ghcr_digest "${image#ghcr.io/}" "$latest_tag")
  fi

  updates=$(echo "$updates" | jq \
    --arg img "$image" \
    --arg cur "$current_tag" \
    --arg curdig "$current_digest" \
    --arg lat "$latest_tag" \
    --arg latdig "$latest_digest" \
    --arg type "$update_type" \
    --arg noauto "$no_auto_merge" \
    '. += [{"image": $img, "current_tag": $cur, "current_digest": $curdig, "latest_tag": $lat, "latest_digest": $latdig, "update_type": $type, "never_auto_merge": ($noauto == "true")}]')

done <<< "$(extract_images)"

# Update state
jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.lastCheck = $ts' "$STATE_FILE" > "${STATE_FILE}.tmp" \
  && mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Output results
echo "$updates" | jq .
