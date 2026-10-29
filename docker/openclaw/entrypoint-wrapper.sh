#!/bin/sh
set -e

# Bundled plugin dependencies (diagnostics-otel) are installed at image build
# time from the upstream lockfile (see Dockerfile); nothing is installed here.
if [ ! -d /app/extensions/diagnostics-otel/node_modules ]; then
  echo "[entrypoint] WARNING: diagnostics-otel has no node_modules; rebuild the image" >&2
fi

# Security: disable git hooks in workspace to prevent persistence attacks
# (writable .git/hooks/ could be abused via prompt injection)
git config --global core.hooksPath /dev/null 2>/dev/null || true

# GitLab auth for git: git-credential-gitlab looks the token up at use time
# (Vault / env), so remote URLs never need to carry credentials.
# useHttpPath lets the helper pick a per-repo token.
git config --global credential.https://gitlab.lab.nkontur.com.helper /usr/local/bin/git-credential-gitlab 2>/dev/null || true
git config --global credential.https://gitlab.lab.nkontur.com.useHttpPath true 2>/dev/null || true

# Hand off to the original entrypoint
exec docker-entrypoint.sh "$@"
