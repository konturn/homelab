#!/bin/bash
# check-updates.sh — Check Docker images in docker-compose for newer versions
# Output: JSON array of {image, current_tag, current_digest, latest_tag, latest_digest, update_type}
# Deterministic, no LLM needed. Designed for cron consumption.

set -uo pipefail

REPO_DIR="${HOMELAB_REPO:-/home/node/.openclaw/workspace/homelab}"
COMPOSE_FILE="$REPO_DIR/docker/docker-compose.yml"
STATE_FILE="${STATE_FILE:-/home/node/.openclaw/workspace/memory/image-update-state.json}"
CURL_TIMEOUT=15
ERRORS=0

# Images to permanently skip update checks for (with reason).
# These are distinct from suppressedImages in the state file, which is for
# temporary/user-driven suppression.
SKIP_IMAGES=(
  "grafana/promtail"  # 3.6.0+ Docker images don't include systemd journal support (grafana/loki#19911)
)

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"lastCheck":null,"lastMR":null,"suppressedImages":[],"neverAutoMerge":["openclaw-gateway","openclaw"]}' > "$STATE_FILE"
fi

# Extract image references from docker-compose (Jinja2 template — grep for image:)
# Output: image_ref|source
extract_compose_images() {
  grep -E '^\s+image:' "$COMPOSE_FILE" \
    | sed 's/.*image:\s*//' \
    | sed 's/["'"'"']//g' \
    | sed 's/\s*#.*//' \
    | grep -v '{{' \
    | sort -u \
    | while IFS= read -r img; do
        echo "${img}|compose:docker/docker-compose.yml"
      done
}

# Extract FROM image references from Dockerfiles under docker/
# Output: image_ref|source
extract_dockerfile_images() {
  local docker_dir="$REPO_DIR/docker"
  find "$docker_dir" -name 'Dockerfile*' -type f 2>/dev/null | while IFS= read -r df; do
    local rel_path="${df#$REPO_DIR/}"
    grep -E '^FROM\s+' "$df" 2>/dev/null \
      | sed 's/^FROM\s\+//' \
      | sed 's/\s\+AS\s\+.*//i' \
      | sed 's/\s*#.*//' \
      | sed 's/\s*$//' \
      | grep -v '^\$' \
      | grep -v '^scratch$' \
      | while IFS= read -r img; do
          echo "${img}|Dockerfile:${rel_path}"
        done
  done | sort -u
}

# Combined: all image references with source
extract_all_images() {
  extract_compose_images
  extract_dockerfile_images
}

##############################################################################
# Registry-agnostic helpers
##############################################################################

# Get an auth token for any OCI registry
# Usage: get_token <registry_base_url> <repo>
get_token() {
  local registry="$1" repo="$2"
  case "$registry" in
    https://registry-1.docker.io)
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" 2>/dev/null \
        | jq -r '.token // empty'
      ;;
    https://ghcr.io)
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https://ghcr.io/token?scope=repository:${repo}:pull" 2>/dev/null \
        | jq -r '.token // empty'
      ;;
    https://lscr.io)
      # LSCR uses GHCR backend
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https://ghcr.io/token?scope=repository:${repo}:pull" 2>/dev/null \
        | jq -r '.token // empty'
      ;;
    *)
      # Generic: try the v2 auth challenge
      local www_auth
      www_auth=$(curl -sf --max-time "$CURL_TIMEOUT" -I "${registry}/v2/" 2>/dev/null \
        | grep -i 'www-authenticate' | head -1)
      if [[ "$www_auth" == *"Bearer"* ]]; then
        local realm service scope
        realm=$(echo "$www_auth" | sed 's/.*realm="\([^"]*\)".*/\1/')
        service=$(echo "$www_auth" | sed 's/.*service="\([^"]*\)".*/\1/')
        scope="repository:${repo}:pull"
        curl -sf --max-time "$CURL_TIMEOUT" "${realm}?service=${service}&scope=${scope}" 2>/dev/null \
          | jq -r '.token // empty'
      fi
      ;;
  esac
}

# Get digest for a tag from any OCI registry
# Usage: get_digest <registry_base_url> <repo> <tag>
get_digest() {
  local registry="$1" repo="$2" tag="$3"
  local api_base="$registry"

  # LSCR proxies to GHCR
  [[ "$registry" == "https://lscr.io" ]] && api_base="https://ghcr.io"

  local token
  token=$(get_token "$registry" "$repo")
  [ -z "$token" ] && echo "" && return

  curl -sf --max-time "$CURL_TIMEOUT" \
    -H "Authorization: Bearer $token" \
    -H "Accept: application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.index.v1+json" \
    -I "${api_base}/v2/${repo}/manifests/${tag}" 2>/dev/null \
    | grep -i 'docker-content-digest' | awk '{print $2}' | tr -d '\r'
}

# Get tags list from OCI v2 API
# Usage: get_tags_v2 <registry_base_url> <repo>
get_tags_v2() {
  local registry="$1" repo="$2"
  local api_base="$registry"
  [[ "$registry" == "https://lscr.io" ]] && api_base="https://ghcr.io"

  local token
  token=$(get_token "$registry" "$repo")
  [ -z "$token" ] && echo "" && return

  curl -sf --max-time "$CURL_TIMEOUT" \
    -H "Authorization: Bearer $token" \
    "${api_base}/v2/${repo}/tags/list" 2>/dev/null
}

# Get latest semver tag from Docker Hub v2 API (has richer metadata)
get_latest_dockerhub_api() {
  local repo="$1" current_suffix="$2"
  local response
  response=$(curl -sf --max-time "$CURL_TIMEOUT" \
    "https://hub.docker.com/v2/repositories/${repo}/tags?page_size=100&ordering=last_updated" 2>/dev/null) || { echo ""; return; }

  [ -z "$response" ] && { echo ""; return; }

  # Extract all valid tags, filter arch suffixes, sort by version
  local all_tags
  all_tags=$(echo "$response" | jq -r '
    [.results[]
     | select(.name != "latest")
     | select(.name | test("^[0-9]"))
     | select(.name | test("rc|alpha|beta|dev|nightly|edge") | not)
     | select(.name | test("-(arm64|armhf|arm|amd64|s390x|ppc64le|i386|386)$") | not)
     | .name
    ] | .[]')

  [ -z "$all_tags" ] && { echo ""; return; }

  # If we have a suffix preference, try matching first
  if [ -n "$current_suffix" ]; then
    local matched
    matched=$(echo "$all_tags" | grep -F -- "$current_suffix" | sort -V | tail -1)
    if [ -n "$matched" ]; then
      echo "$matched"
      return
    fi
  fi

  # Fall back to highest version overall
  echo "$all_tags" | sort -V | tail -1
}

# Get latest semver tag from OCI v2 tags/list
get_latest_from_tags() {
  local registry="$1" repo="$2" current_suffix="$3"
  local tags_json
  tags_json=$(get_tags_v2 "$registry" "$repo")
  [ -z "$tags_json" ] && { echo ""; return; }

  local all_tags
  all_tags=$(echo "$tags_json" | jq -r '
    [.tags[]
     | select(test("^[0-9]"))
     | select(test("rc|alpha|beta|dev|nightly|edge") | not)
     | select(test("-(arm64|armhf|arm|amd64|s390x|ppc64le|i386|386)$") | not)
    ] | .[]')

  [ -z "$all_tags" ] && { echo ""; return; }

  if [ -n "$current_suffix" ]; then
    local matched
    matched=$(echo "$all_tags" | grep -F -- "$current_suffix" | sort -V | tail -1)
    if [ -n "$matched" ]; then
      echo "$matched"
      return
    fi
  fi

  echo "$all_tags" | sort -V | tail -1
}

##############################################################################
# Version comparison
##############################################################################

# Extract the version-number prefix and the suffix from a tag
# e.g., "6.9.1-php8.3-apache" → version="6.9.1" suffix="-php8.3-apache"
# e.g., "3.6" → version="3.6" suffix=""
split_version_suffix() {
  local tag="$1"
  tag="${tag#v}"
  # Match leading digits and dots as the version
  local version suffix
  version=$(echo "$tag" | grep -oE '^[0-9]+(\.[0-9]+)*')
  suffix="${tag#"$version"}"

  # If suffix looks like a build hash (e.g., -2b1ba6e69, -121068a07),
  # treat it as non-meaningful (don't use for suffix matching)
  if echo "$suffix" | grep -qE '^-[0-9a-f]{7,}$'; then
    suffix=""
  fi

  echo "$version|$suffix"
}

# Compare two semver strings using sort -V
# Returns: "newer", "older", "equal"
compare_versions() {
  local v1="$1" v2="$2"
  if [ "$v1" = "$v2" ]; then
    echo "equal"
    return
  fi
  local sorted
  sorted=$(printf '%s\n%s\n' "$v1" "$v2" | sort -V | head -1)
  if [ "$sorted" = "$v1" ]; then
    echo "newer"  # v2 is newer (v1 sorts first = v1 is smaller)
  else
    echo "older"  # v2 is older (v2 sorts first = v2 is smaller)
  fi
}

# Classify update type between two tags
# Outputs: major, minor, patch, downgrade, variant_change, unknown
classify_update() {
  local current="$1" latest="$2"

  local cur_split lat_split
  cur_split=$(split_version_suffix "$current")
  lat_split=$(split_version_suffix "$latest")

  local cur_ver="${cur_split%%|*}" cur_suffix="${cur_split#*|}"
  local lat_ver="${lat_split%%|*}" lat_suffix="${lat_split#*|}"

  # If we couldn't extract versions from either, unknown
  if [ -z "$cur_ver" ] || [ -z "$lat_ver" ]; then
    echo "unknown"
    return
  fi

  # Check for suffix/variant changes
  if [ "$cur_suffix" != "$lat_suffix" ] && [ -n "$cur_suffix" ] && [ -n "$lat_suffix" ]; then
    # Both have suffixes but they differ
    echo "variant_change"
    return
  fi

  # Compare versions
  local cmp
  cmp=$(compare_versions "$cur_ver" "$lat_ver")

  case "$cmp" in
    equal)
      # Same version, maybe different build metadata
      echo "unknown"
      ;;
    older)
      echo "downgrade"
      ;;
    newer)
      # Determine magnitude
      local cur_major cur_minor lat_major lat_minor
      IFS='.' read -r cur_major cur_minor _ <<< "$cur_ver"
      IFS='.' read -r lat_major lat_minor _ <<< "$lat_ver"
      cur_major="${cur_major:-0}"; cur_minor="${cur_minor:-0}"
      lat_major="${lat_major:-0}"; lat_minor="${lat_minor:-0}"

      if [ "$lat_major" -gt "$cur_major" ] 2>/dev/null; then
        echo "major"
      elif [ "$lat_minor" -gt "$cur_minor" ] 2>/dev/null; then
        echo "minor"
      else
        echo "patch"
      fi
      ;;
  esac
}

##############################################################################
# Registry detection
##############################################################################

# Parse image reference into registry_url, repo, tag
# Sets global vars: IMG_REGISTRY, IMG_REPO, IMG_TAG, IMG_REGISTRY_TYPE
parse_image_ref() {
  local image_no_digest="$1" tag="$2"

  if [[ "$image_no_digest" == ghcr.io/* ]]; then
    IMG_REGISTRY="https://ghcr.io"
    IMG_REPO="${image_no_digest#ghcr.io/}"
    IMG_REGISTRY_TYPE="ghcr"
  elif [[ "$image_no_digest" == lscr.io/* ]]; then
    IMG_REGISTRY="https://lscr.io"
    IMG_REPO="${image_no_digest#lscr.io/}"
    IMG_REGISTRY_TYPE="lscr"
  elif [[ "$image_no_digest" == quay.io/* ]]; then
    IMG_REGISTRY="https://quay.io"
    IMG_REPO="${image_no_digest#quay.io/}"
    IMG_REGISTRY_TYPE="quay"
  elif [[ "$image_no_digest" == *"."*"/"* ]]; then
    # Other registry with domain (e.g., registry.example.com/repo)
    local domain="${image_no_digest%%/*}"
    IMG_REGISTRY="https://${domain}"
    IMG_REPO="${image_no_digest#*/}"
    IMG_REGISTRY_TYPE="generic"
  else
    # Docker Hub
    IMG_REGISTRY="https://registry-1.docker.io"
    if [[ "$image_no_digest" == *"/"* ]]; then
      IMG_REPO="$image_no_digest"
    else
      IMG_REPO="library/$image_no_digest"
    fi
    IMG_REGISTRY_TYPE="dockerhub"
  fi
  IMG_TAG="$tag"
}

# Get latest tag for an image (uses best available method per registry)
get_latest_tag() {
  local current_suffix="$1"

  if [ "$IMG_REGISTRY_TYPE" = "dockerhub" ]; then
    get_latest_dockerhub_api "$IMG_REPO" "$current_suffix"
  else
    get_latest_from_tags "$IMG_REGISTRY" "$IMG_REPO" "$current_suffix"
  fi
}

##############################################################################
# Main
##############################################################################
cd "$REPO_DIR"
git pull origin main --quiet 2>/dev/null || true

suppressed=$(jq -r '(.suppressedImages // [])[]' "$STATE_FILE" 2>/dev/null)
never_auto_merge=$(jq -r '(.neverAutoMerge // [])[]' "$STATE_FILE" 2>/dev/null)
updates="[]"

while IFS='|' read -r image_ref source; do
  (
    # Default source if not provided (backward compat)
    source="${source:-compose:docker/docker-compose.yml}"

    # Strip existing digest if present
    image_no_digest="${image_ref%%@*}"

    # Split image:tag — handle images with port numbers (e.g., registry:5000/repo)
    # Strategy: if last colon-segment looks like a tag (starts with digit or "latest"), split there
    image="$image_no_digest"
    current_tag="latest"
    if [[ "$image_no_digest" == *":"* ]]; then
      local_tag="${image_no_digest##*:}"
      if [[ "$local_tag" =~ ^[0-9] ]] || [ "$local_tag" = "latest" ]; then
        image="${image_no_digest%:*}"
        current_tag="$local_tag"
      fi
    fi

    # Extract current digest if present
    current_digest=""
    if [[ "$image_ref" == *"@sha256:"* ]]; then
      current_digest="${image_ref#*@}"
    fi

    # Skip permanently excluded images (hardcoded in SKIP_IMAGES)
    for skip in "${SKIP_IMAGES[@]}"; do
      if [ "$image" = "$skip" ]; then
        exit 0
      fi
    done

    # Skip suppressed images (from state file)
    if echo "$suppressed" | grep -qF "$image"; then
      exit 0
    fi

    # Check if this is a never-auto-merge image
    no_auto_merge="false"
    if echo "$never_auto_merge" | grep -qF "$(basename "$image")"; then
      no_auto_merge="true"
    fi
    # Always mark moltbot Dockerfile images as never_auto_merge
    if [[ "$source" == Dockerfile:docker/openclaw/* ]]; then
      no_auto_merge="true"
    fi

    # Parse registry info
    parse_image_ref "$image" "$current_tag"

    # Extract current suffix for tag matching
    local_split=$(split_version_suffix "$current_tag")
    current_suffix="${local_split#*|}"

    # For :latest images, just get the digest for pinning
    if [ "$current_tag" = "latest" ]; then
      latest_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "latest")
      actual_tag=$(get_latest_tag "")

      echo "{\"image\": \"$image\", \"current_tag\": \"latest\", \"current_digest\": \"${current_digest}\", \"latest_tag\": \"${actual_tag:-latest}\", \"latest_digest\": \"${latest_digest}\", \"update_type\": \"unpinned\", \"source\": \"$source\", \"never_auto_merge\": $([ "$no_auto_merge" = "true" ] && echo true || echo false)}"
      exit 0
    fi

    # Fetch latest version
    latest_tag=$(get_latest_tag "$current_suffix")

    if [ -z "$latest_tag" ]; then
      # Can't determine latest, but still get digest for current tag if missing
      if [ -z "$current_digest" ]; then
        pin_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$current_tag")
        if [ -n "$pin_digest" ]; then
          echo "{\"image\": \"$image\", \"current_tag\": \"$current_tag\", \"current_digest\": \"\", \"latest_tag\": \"$current_tag\", \"latest_digest\": \"$pin_digest\", \"update_type\": \"needs_pin\", \"source\": \"$source\", \"never_auto_merge\": $([ "$no_auto_merge" = "true" ] && echo true || echo false)}"
        fi
      fi
      exit 0
    fi

    if [ "$current_tag" = "$latest_tag" ]; then
      # Same tag — check if digest pin is missing
      if [ -z "$current_digest" ]; then
        pin_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$current_tag")
        if [ -n "$pin_digest" ]; then
          echo "{\"image\": \"$image\", \"current_tag\": \"$current_tag\", \"current_digest\": \"\", \"latest_tag\": \"$current_tag\", \"latest_digest\": \"$pin_digest\", \"update_type\": \"needs_pin\", \"source\": \"$source\", \"never_auto_merge\": $([ "$no_auto_merge" = "true" ] && echo true || echo false)}"
        fi
      fi
      exit 0
    fi

    update_type=$(classify_update "$current_tag" "$latest_tag")
    latest_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$latest_tag")

    echo "{\"image\": \"$image\", \"current_tag\": \"$current_tag\", \"current_digest\": \"${current_digest}\", \"latest_tag\": \"$latest_tag\", \"latest_digest\": \"${latest_digest}\", \"update_type\": \"$update_type\", \"source\": \"$source\", \"never_auto_merge\": $([ "$no_auto_merge" = "true" ] && echo true || echo false)}"
  ) || ((ERRORS++))
done <<< "$(extract_all_images)" | jq -s '.' > /tmp/check-updates-result.json

# Update state
jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.lastCheck = $ts' "$STATE_FILE" > "${STATE_FILE}.tmp" \
  && mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Output results
cat /tmp/check-updates-result.json

if [ "$ERRORS" -gt 0 ]; then
  echo "WARNING: $ERRORS image(s) failed to check" >&2
fi
