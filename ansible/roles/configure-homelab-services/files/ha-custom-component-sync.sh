#!/bin/bash
# Install one Home Assistant custom integration from a private GitLab project,
# pinned to a full commit id (homeassistant_custom_components.yml,
# docs/HOMEASSISTANT.md "Custom integrations").
#
#   bash -s -- <domain> <repo-url> <sha> <subdir> <dest> [--check] < this
#
# GL_USER / GL_TOKEN (environment): a read_repository deploy token. They are
# passed to git through GIT_CONFIG_* variables, never on a command line, and
# are never printed or stored in the cache.
#
# The commit is fetched into /var/cache/ha-custom-components/<domain> (only
# when it is not already there), its id is checked, <subdir> is exported with
# git archive and rsync'ed into <dest> with --delete (HA's __pycache__ is left
# alone). Prints "changed: <n> paths" or "unchanged". --check: no writes to
# <dest>, prints what would change.
set -euo pipefail

domain=$1 url=$2 sha=$3 sub=$4 dest=$5 mode=${6:-}
fail() { echo "ha-custom-component-sync: $*" >&2; exit 1; }

[[ $domain =~ ^[a-z0-9_]+$ ]] || fail "bad domain '$domain'"
[[ $sha =~ ^[0-9a-f]{40}$ ]] || fail "pin must be a full 40-character commit id"
[[ $url == https://* ]] || fail "repo url must be https"
[ "$(basename "$dest")" = "$domain" ] || fail "dest must end in /$domain"
[ -n "${GL_USER:-}" ] && [ -n "${GL_TOKEN:-}" ] || fail "GL_USER / GL_TOKEN not set"

cache_root=/var/cache/ha-custom-components
repo=$cache_root/$domain
install -d -m 0700 "$cache_root"
[ -d "$repo/.git" ] || git init -q "$repo"

if ! git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null; then
  auth=$(printf '%s:%s' "$GL_USER" "$GL_TOKEN" | base64 -w0)
  GIT_CONFIG_COUNT=2 \
    GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $auth" \
    GIT_CONFIG_KEY_1=credential.helper GIT_CONFIG_VALUE_1= \
    GIT_TERMINAL_PROMPT=0 \
    git -C "$repo" fetch -q --depth 1 --no-tags "$url" "$sha" \
    || fail "fetch of $sha from $url failed"
  unset auth
fi
[ "$(git -C "$repo" rev-parse "$sha^{commit}")" = "$sha" ] || fail "commit id mismatch"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
git -C "$repo" archive --format=tar "$sha" "$sub" | tar -x -C "$stage"
src=$stage/$sub
[ -f "$src/manifest.json" ] || fail "$sub/manifest.json missing at $sha"
got=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["domain"])' "$src/manifest.json")
[ "$got" = "$domain" ] || fail "manifest domain is '$got', expected '$domain'"

opts=(-rlpc --delete --omit-dir-times --chmod=D755,F644 --exclude=__pycache__/ --itemize-changes)
[ "$mode" = --check ] && opts+=(--dry-run)
[ "$mode" = --check ] || install -d -m 0755 "$dest"
[ -d "$dest" ] || { echo "changed: new install (check mode)"; exit 0; }
out=$(rsync "${opts[@]}" "$src/" "$dest/")
if [ -n "$out" ]; then
  [ "$mode" = --check ] || chown -R root:root "$dest"
  echo "changed: $(wc -l <<<"$out") paths"
  printf '%s\n' "$out" | head -50
else
  echo "unchanged"
fi
