#!/bin/bash
# Keep the router's homelab checkouts at origin/main. Managed by Ansible
# (configure-homelab-services); runs from root's crontab every 5 minutes,
# output goes to syslog under the tag homelab-sync.
#
# Two checkouts, two policies:
#   - OpenClaw workspace: a disposable clone the agent reads. Hard reset to
#     origin/main and drop untracked files, as before.
#   - Router checkout (/root/repos/homelab): where people and agents work and
#     run scripts/approve-root-mr.sh from. Fast-forward only, and only when it
#     is on main with no tracked changes. Untracked files, stashes, other
#     branches and worktrees are never touched.
#
# When both checkouts contain origin/main afterwards, STATE_DIR/last_ok gets
# the current UTC time. telegraf reads it (measurement homelab_repo_sync) and
# the anomaly detector's homelab_repo_sync signal flags it once it is stale,
# so a failing sync or a checkout left behind main shows up within the hour.
set -uo pipefail

WORKSPACE=${WORKSPACE:?}
ROUTER_REPO=${ROUTER_REPO:?}
STATE_DIR=${STATE_DIR:-/var/lib/homelab-repo-sync}

log() { echo "homelab-sync: $*"; }

exec 9>/run/homelab-repo-sync.lock
flock -n 9 || { log "previous run still active; skipped"; exit 0; }

ok=1

# Disposable workspace clone: reset to origin/main.
if cd "$WORKSPACE" \
    && git fetch --quiet origin main \
    && git reset --hard --quiet origin/main \
    && git clean -fd --quiet; then
    :
else
    log "ERROR: could not sync $WORKSPACE"
    ok=0
fi

# Router checkout: fast-forward only, never discard anything.
if cd "$ROUTER_REPO" && git fetch --quiet origin; then
    branch=$(git symbolic-ref --quiet --short HEAD || echo "(detached)")
    if [ "$branch" = main ] && git diff --quiet && git diff --cached --quiet; then
        git merge --ff-only --quiet origin/main || log "ERROR: fast-forward of $ROUTER_REPO failed"
    elif ! git merge-base --is-ancestor origin/main HEAD; then
        if [ "$branch" = main ]; then why="has uncommitted changes to tracked files"; else why="is on $branch"; fi
        log "WARNING: $ROUTER_REPO $why; not updating it"
    fi
    if ! git merge-base --is-ancestor origin/main HEAD; then
        log "ERROR: $ROUTER_REPO is $(git rev-list --count HEAD..origin/main) commits behind origin/main"
        ok=0
    fi
else
    log "ERROR: could not fetch in $ROUTER_REPO"
    ok=0
fi

if [ "$ok" = 1 ]; then
    mkdir -p "$STATE_DIR"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/last_ok.tmp"
    chmod 0644 "$STATE_DIR/last_ok.tmp"
    mv -f "$STATE_DIR/last_ok.tmp" "$STATE_DIR/last_ok"
    exit 0
fi
exit 1
