#!/bin/sh
set -e

# Install npm dependencies for bundled extensions that require them.
# Bundled plugins ship as source under /app/extensions/ but some (like
# diagnostics-otel) have npm dependencies not baked into the image.
# This script runs on every container start to ensure deps are present,
# surviving image upgrades.  It also runs the build step for extensions
# that ship as TypeScript source (e.g. diagnostics-otel).
#
# Only runs `npm install` when node_modules is missing or stale
# (package.json newer than node_modules).

install_plugin_deps() {
  local dir="$1"
  local pkg="$dir/package.json"
  local nm="$dir/node_modules"

  [ -f "$pkg" ] || return 0

  # Skip if node_modules exists and is newer than package.json
  if [ -d "$nm" ] && [ "$nm" -nt "$pkg" ]; then
    return 0
  fi

  echo "[entrypoint] Installing deps for $(basename "$dir")..."
  cd "$dir" && npm install --ignore-scripts --no-audit --no-fund 2>&1 | tail -1

  # Build if build script exists (needed for TypeScript extensions)
  if grep -q '"build"' "$pkg" 2>/dev/null; then
    echo "[entrypoint] Building $(basename "$dir")..."
    npm run build 2>&1 | tail -3
  fi
}

# Install deps for extensions that need them
install_plugin_deps /app/extensions/diagnostics-otel

# Hand off to the original entrypoint
exec docker-entrypoint.sh "$@"
