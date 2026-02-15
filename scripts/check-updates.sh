#!binbash
# check-updates.sh — Check Docker images in docker-compose for newer versions
# Output: JSON array of {image, current_tag, current_digest, latest_tag, latest_digest, update_type}
# Deterministic, no LLM needed. Designed for cron consumption.

set -uo pipefail

REPO_DIR="${HOMELAB_REPO:-homenode.openclawworkspacehomelab}"
COMPOSE_FILE="$REPO_DIRdockerdocker-compose.yml"
STATE_FILE="${STATE_FILE:-homenode.openclawworkspacememoryimage-update-state.json}"
CURL_TIMEOUT=15
ERRORS=0

# Initialize state file if missing
if [ ! -f "$STATE_FILE" ]; then
  echo '{"lastCheck":null,"lastMR":null,"suppressedImages":[],"neverAutoMerge":["moltbot-gateway","moltbot"]}' > "$STATE_FILE"
fi

# Extract image references from docker-compose (Jinja2 template — grep for image:)
extract_compose_images() {
  grep -E '^\s+image:' "$COMPOSE_FILE" \
    | sed 's.*image:\s*' \
    | sed 's["'"'"']g' \
    | sed 's\s*#.*' \
    | grep -v '{{' \
    | sort -u
}

# Extract FROM base images from Dockerfiles
# Output format: image_ref|dockerfile_path (so we can track source)
extract_dockerfile_images() {
  find "$REPO_DIRdocker" -name "Dockerfile*" -type f 2>devnull | while read -r df; do
    grep -E '^FROM ' "$df" | sed 's^FROM\s*' | sed 's\s*[aA][sS]\s.*$' | while read -r img; do
      # Skip scratch and ARG-based images
      [[ "$img" == "scratch" ]] && continue
      [[ "$img" == *'$'* ]] && continue
      echo "${img}|${df}"
    done
  done | sort -u
}

# Combined: extract all image references
extract_images() {
  # Compose images (no source path)
  extract_compose_images | while read -r img; do echo "${img}|compose"; done
  # Dockerfile images
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
    https:registry-1.docker.io)
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https:auth.docker.iotoken?service=registry.docker.io&scope=repository:${repo}:pull" 2>devnull \
        | jq -r '.token  empty'
      ;;
    https:ghcr.io)
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https:ghcr.iotoken?scope=repository:${repo}:pull" 2>devnull \
        | jq -r '.token  empty'
      ;;
    https:lscr.io)
      # LSCR uses GHCR backend
      curl -sf --max-time "$CURL_TIMEOUT" \
        "https:ghcr.iotoken?scope=repository:${repo}:pull" 2>devnull \
        | jq -r '.token  empty'
      ;;
    *)
      # Generic: try the v2 auth challenge
      local www_auth
      www_auth=$(curl -sf --max-time "$CURL_TIMEOUT" -I "${registry}v2" 2>devnull \
        | grep -i 'www-authenticate' | head -1)
      if [[ "$www_auth" == *"Bearer"* ]]; then
        local realm service scope
        realm=$(echo "$www_auth" | sed 's.*realm="\([^"]*\)".*\1')
        service=$(echo "$www_auth" | sed 's.*service="\([^"]*\)".*\1')
        scope="repository:${repo}:pull"
        curl -sf --max-time "$CURL_TIMEOUT" "${realm}?service=${service}&scope=${scope}" 2>devnull \
          | jq -r '.token  empty'
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
  [[ "$registry" == "https:lscr.io" ]] && api_base="https:ghcr.io"

  local token
  token=$(get_token "$registry" "$repo")
  [ -z "$token" ] && echo "" && return

  curl -sf --max-time "$CURL_TIMEOUT" \
    -H "Authorization: Bearer $token" \
    -H "Accept: applicationvnd.docker.distribution.manifest.list.v2+json, applicationvnd.docker.distribution.manifest.v2+json, applicationvnd.oci.image.index.v1+json" \
    -I "${api_base}v2${repo}manifests${tag}" 2>devnull \
    | grep -i 'docker-content-digest' | awk '{print $2}' | tr -d '\r'
}

# Get tags list from OCI v2 API
# Usage: get_tags_v2 <registry_base_url> <repo>
get_tags_v2() {
  local registry="$1" repo="$2"
  local api_base="$registry"
  [[ "$registry" == "https:lscr.io" ]] && api_base="https:ghcr.io"

  local token
  token=$(get_token "$registry" "$repo")
  [ -z "$token" ] && echo "" && return

  curl -sf --max-time "$CURL_TIMEOUT" \
    -H "Authorization: Bearer $token" \
    "${api_base}v2${repo}tagslist" 2>devnull
}

# Get latest semver tag from Docker Hub v2 API (has richer metadata)
get_latest_dockerhub_api() {
  local repo="$1" current_suffix="$2"
  local response
  response=$(curl -sf --max-time "$CURL_TIMEOUT" \
    "https:hub.docker.comv2repositories${repo}tags?page_size=100&ordering=last_updated" 2>devnull) || { echo ""; return; }

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

# Get latest semver tag from OCI v2 tagslist
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

  # Clean suffix: remove build hashes and numeric-only segments to get the
  # meaningful variant identifier for tag matching
  # Examples:
  #   -2b1ba6e69           → "" (pure build hash)
  #   -21693836646-ubuntu  → "-ubuntu" (strip numeric build ID, keep variant)
  #   -php8.3-apache       → "-php8.3-apache" (keep as-is, meaningful variant)
  #   -ubi10               → "-ubi" (strip trailing version number from variant)
  #   -fpm                 → "-fpm" (keep as-is)
  #   -fpm-alpine          → "-fpm-alpine" (keep as-is)
  #   -ee.0                → "-ee" (strip version from variant)

  # Step 1: if entire suffix is a build hash, clear it
  if echo "$suffix" | grep -qE '^-[0-9a-f]{7,}$'; then
    suffix=""
  fi

  # Step 2: strip leading numeric-only segments (build IDs like -21693836646-)
  suffix=$(echo "$suffix" | sed -E 's^-[0-9]+--')

  # Step 3: for suffix matching, extract just the "variant name" (strip version numbers)
  # This becomes the "match_suffix" used for finding similar tags
  local match_suffix
  match_suffix=$(echo "$suffix" | sed -E 's[0-9]+(\.[0-9]+)*g' | sed 's--*-g' | sed 's-$')

  echo "$version|$suffix|$match_suffix"
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

  local cur_ver="${cur_split%%|*}"
  local cur_rest="${cur_split#*|}"
  local cur_suffix="${cur_rest%%|*}"
  local cur_match="${cur_rest#*|}"

  local lat_ver="${lat_split%%|*}"
  local lat_rest="${lat_split#*|}"
  local lat_suffix="${lat_rest%%|*}"
  local lat_match="${lat_rest#*|}"

  # If we couldn't extract versions from either, unknown
  if [ -z "$cur_ver" ] || [ -z "$lat_ver" ]; then
    echo "unknown"
    return
  fi

  # Check for variant changes using the cleaned match_suffix
  if [ "$cur_match" != "$lat_match" ] && [ -n "$cur_match" ] && [ -n "$lat_match" ]; then
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

      if [ "$lat_major" -gt "$cur_major" ] 2>devnull; then
        echo "major"
      elif [ "$lat_minor" -gt "$cur_minor" ] 2>devnull; then
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

  if [[ "$image_no_digest" == ghcr.io* ]]; then
    IMG_REGISTRY="https:ghcr.io"
    IMG_REPO="${image_no_digest#ghcr.io}"
    IMG_REGISTRY_TYPE="ghcr"
  elif [[ "$image_no_digest" == lscr.io* ]]; then
    IMG_REGISTRY="https:lscr.io"
    IMG_REPO="${image_no_digest#lscr.io}"
    IMG_REGISTRY_TYPE="lscr"
  elif [[ "$image_no_digest" == quay.io* ]]; then
    IMG_REGISTRY="https:quay.io"
    IMG_REPO="${image_no_digest#quay.io}"
    IMG_REGISTRY_TYPE="quay"
  elif [[ "$image_no_digest" == *"."*""* ]]; then
    # Other registry with domain (e.g., registry.example.comrepo)
    local domain="${image_no_digest%%*}"
    IMG_REGISTRY="https:${domain}"
    IMG_REPO="${image_no_digest#*}"
    IMG_REGISTRY_TYPE="generic"
  else
    # Docker Hub
    IMG_REGISTRY="https:registry-1.docker.io"
    if [[ "$image_no_digest" == *""* ]]; then
      IMG_REPO="$image_no_digest"
    else
      IMG_REPO="library$image_no_digest"
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
# JSON output helper (uses variables from enclosing scope)
##############################################################################
# Usage: emit_json <update_type> <cur_tag> <cur_digest> <lat_tag> <lat_digest>
emit_json() {
  local utype="$1" ctag="$2" cdig="$3" ltag="$4" ldig="$5"
  local auto_val
  auto_val=$([ "$no_auto_merge" = "true" ] && echo true || echo false)
  # source_path comes from the outer loop
  local src="${source_path:-compose}"
  printf '{"image":"%s","current_tag":"%s","current_digest":"%s","latest_tag":"%s","latest_digest":"%s","update_type":"%s","never_auto_merge":%s,"source":"%s"}\n' \
    "$image" "$ctag" "$cdig" "$ltag" "$ldig" "$utype" "$auto_val" "$src"
}

##############################################################################
# Main
##############################################################################
cd "$REPO_DIR"
git pull origin main --quiet 2>devnull || true

suppressed=$(jq -r '(.suppressedImages  [])[]' "$STATE_FILE" 2>devnull)
never_auto_merge=$(jq -r '(.neverAutoMerge  [])[]' "$STATE_FILE" 2>devnull)
updates="[]"

while IFS= read -r line; do
  (
    image_ref="${line%%|*}"
    source_path="${line#*|}"

    # Strip existing digest if present
    image_no_digest="${image_ref%%@*}"

    # Split image:tag — handle images with port numbers (e.g., registry:5000repo)
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

    # Skip suppressed images
    if echo "$suppressed" | grep -qF "$image"; then
      exit 0
    fi

    # Check if this is a never-auto-merge image
    no_auto_merge="false"
    if echo "$never_auto_merge" | grep -qF "$(basename "$image")"; then
      no_auto_merge="true"
    fi

    # Parse registry info
    parse_image_ref "$image" "$current_tag"

    # Extract current suffix for tag matching
    local_split=$(split_version_suffix "$current_tag")
    local_rest="${local_split#*|}"
    current_suffix="${local_rest%%|*}"
    match_suffix="${local_rest#*|}"

    # For :latest images, just get the digest for pinning
    if [ "$current_tag" = "latest" ]; then
      latest_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "latest")
      actual_tag=$(get_latest_tag "")

      emit_json "unpinned" "latest" "${current_digest}" "${actual_tag:-latest}" "${latest_digest}"
      exit 0
    fi

    # Fetch latest version
    latest_tag=$(get_latest_tag "$match_suffix")

    if [ -z "$latest_tag" ]; then
      # Can't determine latest, but still get digest for current tag if missing
      if [ -z "$current_digest" ]; then
        pin_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$current_tag")
        if [ -n "$pin_digest" ]; then
          emit_json "needs_pin" "$current_tag" "" "$current_tag" "$pin_digest"
        fi
      fi
      exit 0
    fi

    if [ "$current_tag" = "$latest_tag" ]; then
      # Same tag — check if digest pin is missing
      if [ -z "$current_digest" ]; then
        pin_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$current_tag")
        if [ -n "$pin_digest" ]; then
          emit_json "needs_pin" "$current_tag" "" "$current_tag" "$pin_digest"
        fi
      fi
      exit 0
    fi

    update_type=$(classify_update "$current_tag" "$latest_tag")
    latest_digest=$(get_digest "$IMG_REGISTRY" "$IMG_REPO" "$latest_tag")

    emit_json "$update_type" "$current_tag" "${current_digest}" "$latest_tag" "${latest_digest}"
  ) || ((ERRORS++))
done <<< "$(extract_images)" | jq -s '.' > tmpcheck-updates-result.json

# Update state
jq --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.lastCheck = $ts' "$STATE_FILE" > "${STATE_FILE}.tmp" \
  && mv "${STATE_FILE}.tmp" "$STATE_FILE"

# Output results
cat tmpcheck-updates-result.json

if [ "$ERRORS" -gt 0 ]; then
  echo "WARNING: $ERRORS image(s) failed to check" >&2
fi
