#!/bin/bash
# Keep the router's homelab checkouts at origin/main. Managed by Ansible
# (configure-homelab-services); runs from root's crontab every 5 minutes,
# output goes to syslog under the tag homelab-sync.
#
# Two kinds of checkout, two policies:
#   - OpenClaw workspaces (WORKSPACE, one path or several separated by
#     whitespace: the noah and ops agents each have one): disposable clones
#     the agents read. Hard reset to origin/main and drop untracked files.
#   - Router checkout (/root/repos/homelab): where people and agents work and
#     run scripts/approve-root-mr.sh from. Fast-forward only, and only when it
#     is on main with no tracked changes. Untracked files, stashes, other
#     branches and worktrees are never touched. One exception: a detached HEAD
#     that is already on origin/main, with no tracked changes and no rebase,
#     merge, cherry-pick or bisect in progress, is switched back to main.
#     That is what `git checkout origin/main` leaves behind. Switching loses
#     nothing, because the commit is on main. From 2026-11-16 the checkout was
#     re-detached that way several times, each time within minutes of being
#     fixed, and the stale signal stayed severe for a week.
#
# When every checkout contains origin/main afterwards, STATE_DIR/last_ok gets
# the current UTC time. telegraf reads it (measurement homelab_repo_sync) and
# the anomaly detector's homelab_repo_sync signal flags it once it is stale,
# so a failing sync or a checkout left behind main shows up within the hour.
set -uo pipefail

WORKSPACE=${WORKSPACE:?}
ROUTER_REPO=${ROUTER_REPO:?}
STATE_DIR=${STATE_DIR:-/var/lib/homelab-repo-sync}

log() { echo "homelab-sync: $*"; }

# A detached HEAD in the middle of one of these is someone's work in progress.
in_progress() {
    local p
    for p in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
        [ -e "$(git rev-parse --git-path "$p")" ] && return 0
    done
    return 1
}

exec 9>/run/homelab-repo-sync.lock
flock -n 9 || { log "previous run still active; skipped"; exit 0; }

ok=1

# Disposable workspace clones: reset to origin/main.
for ws in $WORKSPACE; do
    if cd "$ws" \
        && git fetch --quiet origin main \
        && git reset --hard --quiet origin/main \
        && git clean -fd --quiet; then
        :
    else
        log "ERROR: could not sync $ws"
        ok=0
    fi
done

# Router checkout: fast-forward only, never discard anything.
if cd "$ROUTER_REPO" && git fetch --quiet origin; then
    branch=$(git symbolic-ref --quiet --short HEAD || echo "(detached)")
    if [ "$branch" = "(detached)" ] \
        && git diff --quiet && git diff --cached --quiet \
        && git merge-base --is-ancestor HEAD origin/main \
        && ! in_progress; then
        at=$(git rev-parse --short HEAD)
        if git checkout --quiet main; then
            branch=main
            log "NOTICE: $ROUTER_REPO was detached at $at (on origin/main, no changes); switched back to main"
        else
            log "WARNING: $ROUTER_REPO is detached at $at; could not switch back to main"
        fi
    fi
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
