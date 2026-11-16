#!/bin/bash
# openclaw-claude: the claude-cli backend command (openclaw.json.j2,
# agents.defaults.cliBackends.claude-cli.command). Baked into the image,
# root-owned.
#
# Every agent but `ops` gets the Claude CLI unchanged, as the gateway's user
# (node). The `ops` agent runs crons and the event-router hook, which read
# untrusted input, so its CLI runs as the OS user `ops` instead
# (docs/design/openclaw-identities.md):
#
#   - setpriv-ops (a setuid/setgid-ops copy of setpriv) switches to ops with
#     no-new-privileges. node cannot signal ops, so ops-launch stops the CLI's
#     process group itself once this wrapper is gone: the gateway still stops
#     a run by signalling this process;
#   - the environment is rebuilt from an allowlist: none of the gateway's
#     secrets (gateway/hook/Telegram tokens, the Vault AppRole, API keys)
#     reaches ops;
#   - files OpenClaw hands the CLI from its private temp dir (system prompt,
#     skills plugin) are copied to a fresh staging dir ops can read;
#   - the loopback MCP bridge to the gateway's tools is dropped
#     (--mcp-config): those tools run inside the gateway as node, and its
#     token is shared by all sessions. --strict-mcp-config stays, so the CLI
#     loads no MCP server at all.
#
# OpenClaw sets OPENCLAW_MCP_AGENT_ID for the run it is starting; the agent
# cannot set it for the gateway. Anything unexpected for ops fails closed.
set -u

CLAUDE=/usr/local/bin/claude
SETPRIV_OPS=/usr/local/libexec/openclaw/setpriv-ops
OPS_LAUNCH=/usr/local/libexec/openclaw/ops-launch
STAGE_ROOT=/tmp/openclaw-ops-stage

if [ "${OPENCLAW_MCP_AGENT_ID:-}" != ops ]; then
  exec "$CLAUDE" "$@"
fi

die() { echo "openclaw-claude (ops): $*" >&2; exit 126; }

[ -x "$SETPRIV_OPS" ] || die "missing $SETPRIV_OPS"
[ -x "$OPS_LAUNCH" ] || die "missing $OPS_LAUNCH"

# Stage the private temp files. The root is world-traversable but not
# listable; each run gets an unguessable directory, removed on exit.
umask 022
mkdir -p "$STAGE_ROOT" 2>/dev/null
chmod 0711 "$STAGE_ROOT" 2>/dev/null
[ -d "$STAGE_ROOT" ] && [ ! -L "$STAGE_ROOT" ] && [ "$(stat -c %u "$STAGE_ROOT")" = "$(id -u)" ] \
  || die "unsafe staging root $STAGE_ROOT"
STAGE=$(mktemp -d "$STAGE_ROOT/run.XXXXXXXXXXXX") || die "mktemp failed"
chmod 0755 "$STAGE"
child=
cleanup() { rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'exit 143' TERM INT HUP

stage_file() { # <path> <index> -> prints staged path
  local src=$1 dst
  dst="$STAGE/$2-$(basename -- "$src")"
  if [ -d "$src" ]; then
    cp -a -- "$src" "$dst" || return 1
    chmod -R a+rX -- "$dst" || return 1
  else
    cp -- "$src" "$dst" || return 1
    chmod 0644 -- "$dst" || return 1
  fi
  printf '%s' "$dst"
}

n=0
args=()
while [ $# -gt 0 ]; do
  a=$1; shift
  case $a in
    --mcp-config)
      shift ;;                         # drop the gateway tool bridge
    --mcp-config=*) ;;
    --append-system-prompt-file|--system-prompt-file|--plugin-dir|--settings)
      [ $# -gt 0 ] || die "$a without a value"
      v=$1; shift
      if [ -e "$v" ]; then
        n=$((n + 1))
        s=$(stage_file "$v" "$n") || die "cannot stage $a"
        args+=("$a" "$s")
      else
        args+=("$a" "$v")
      fi ;;
    *) args+=("$a") ;;
  esac
done

# Environment for ops: an allowlist. OPENCLAW_MCP_* is deliberately absent.
envv=()
while IFS= read -r -d '' kv; do
  k=${kv%%=*}
  case $k in
    PATH|LANG|LC_*|TZ|TERM|NO_COLOR|FORCE_COLOR|CLAUDE_*|DISABLE_*|MAX_THINKING_TOKENS|BASH_MAX_TIMEOUT_MS|BASH_DEFAULT_TIMEOUT_MS)
      envv+=("$kv") ;;
  esac
done < <(env -0)

# The ops side runs in the background so this wrapper can take the gateway's
# signals: on TERM/INT/HUP it exits, and ops-launch then ends the CLI.
env -i "${envv[@]}" "$SETPRIV_OPS" --reuid=ops --regid=ops --keep-groups --nnp \
  -- "$OPS_LAUNCH" "${args[@]}" <&0 &
child=$!
wait "$child"
