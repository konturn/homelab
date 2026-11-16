#!/bin/bash
# ops-launch: second half of openclaw-claude for the `ops` agent. Runs as the
# OS user ops (via setpriv-ops) with the environment openclaw-claude built,
# adds ops's own settings and runs the Claude CLI. Baked into the image,
# root-owned. docs/design/openclaw-identities.md
set -u
[ "$(id -un)" = ops ] || { echo "ops-launch: not running as ops" >&2; exit 126; }

export HOME=/home/ops USER=ops LOGNAME=ops SHELL=/bin/bash
export TMPDIR=/home/ops/tmp
# JIT broker identity: the key file is ops's own (0400); the broker derives
# the requester from it. No Vault credential is given to this user.
export JIT_URL=https://jit.lab.nkontur.com
export JIT_REQUESTER=openclaw-ops
export JIT_API_KEY_FILE=/home/ops/.config/openclaw/jit-api-key
export JIT_GITLAB_CACHE_DIR=/home/ops/.cache/jit
export VAULT_ADDR=https://vault.lab.nkontur.com:8200
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 007
mkdir -p "$TMPDIR" "$JIT_GITLAB_CACHE_DIR" 2>/dev/null

# The gateway stops a run by signalling openclaw-claude (user node), which
# cannot signal this user. So the CLI runs in its own process group and a
# watcher ends that group once openclaw-claude is gone.
parent=$PPID
pstart=$(cut -d' ' -f22 "/proc/$parent/stat" 2>/dev/null)
[ -n "$pstart" ] || { echo "ops-launch: no parent" >&2; exit 126; }
set -m
/usr/local/bin/claude "$@" <&0 &
cpid=$!
set +m
(
  while kill -0 "$cpid" 2>/dev/null \
      && [ "$(cut -d' ' -f22 "/proc/$parent/stat" 2>/dev/null)" = "$pstart" ]; do
    sleep 2
  done
  if kill -0 "$cpid" 2>/dev/null; then
    kill -TERM -- "-$cpid" 2>/dev/null
    sleep 10
    kill -KILL -- "-$cpid" 2>/dev/null
  fi
) </dev/null >/dev/null 2>&1 &
watcher=$!
wait "$cpid"
rc=$?
kill "$watcher" 2>/dev/null
exit "$rc"
