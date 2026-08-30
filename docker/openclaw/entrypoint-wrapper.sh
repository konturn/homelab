#!/bin/sh
set -e

# Install npm dependencies for bundled extensions that require them.
# Bundled plugins ship as source under /app/extensions/ but some (like
# diagnostics-otel) have npm dependencies not baked into the image.
# This script runs on every container start to ensure deps are present,
# surviving image upgrades.
#
# Only runs `npm install` when node_modules is missing or stale
# (package.json newer than node_modules).
#
# Runs in a subshell: the image CMD uses a relative path
# (node dist/index.js) that must resolve from the original workdir, so
# the cd below must not leak into the exec'd process.
install_plugin_deps() {
  (
    dir="$1"
    pkg="$dir/package.json"
    nm="$dir/node_modules"

    [ -f "$pkg" ] || exit 0

    # Skip if node_modules exists and is newer than package.json
    if [ -d "$nm" ] && [ "$nm" -nt "$pkg" ]; then
      exit 0
    fi

    echo "[entrypoint] Installing deps for $(basename "$dir")..."
    cd "$dir"
    # The bundled plugin declares a `workspace:*` devDependency
    # (@openclaw/plugin-sdk) that npm cannot resolve outside the upstream
    # monorepo, and npm-shrinkwrap.json pins it too. Strip both, then
    # install production deps only.
    rm -f npm-shrinkwrap.json
    npm pkg delete devDependencies >/dev/null 2>&1 || true
    npm install --ignore-scripts --omit=dev --no-audit --no-fund 2>&1 | tail -1

    # Build only when a real scripts.build entry exists. (A plain grep for
    # '"build"' false-matches the openclaw.build metadata key; TS entries
    # like index.ts are loaded directly by the host and need no build.)
    if node -e 'const p=require("./package.json");process.exit(p.scripts&&p.scripts.build?0:1)'; then
      echo "[entrypoint] Building $(basename "$dir")..."
      npm run build 2>&1 | tail -3
    fi
  ) || echo "[entrypoint] WARNING: dep install failed for $1; starting anyway"
}

# Install deps for extensions that need them
install_plugin_deps /app/extensions/diagnostics-otel

# Security: disable git hooks in workspace to prevent persistence attacks
# (writable .git/hooks/ could be abused via prompt injection)
git config --global core.hooksPath /dev/null 2>/dev/null || true

# Hand off to the original entrypoint
exec docker-entrypoint.sh "$@"
