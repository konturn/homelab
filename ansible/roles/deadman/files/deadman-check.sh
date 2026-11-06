#!/bin/bash
# Off-box heartbeat for the router (ansible/roles/deadman, docs/DEADMAN.md).
#
# Runs every 5 minutes from deadman-check.timer. Checks the services that
# matter and pings a healthchecks.io check: the success URL only when every
# check passes, otherwise <url>/fail with the failed checks in the body. The
# check emails when pings stop or a /fail arrives, so it covers power loss,
# WAN loss and a host that is up with its services down.
#
# Lives on the root filesystem and uses no container, so it keeps working
# when /persistent_data (Docker data-root, Grafana alerting) is gone.
#
# Usage: deadman-check [--dry-run] [--force-fail]
#   --dry-run     run the checks and print the results; no ping
#   --force-fail  add a failing check (also DEADMAN_FORCE_FAIL=1), to test
#                 the alert path end to end
#
# Config: DEADMAN_PING_URL from the environment, else from
# DEADMAN_ENV_FILE (default /etc/deadman-check.env, root-only).
set -u
export LC_ALL=C PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

env_file=${DEADMAN_ENV_FILE:-/etc/deadman-check.env}
check_timeout=${DEADMAN_CHECK_TIMEOUT:-20}
persistent_path=${DEADMAN_PERSISTENT_PATH:-/persistent_data}
zpool_name=${DEADMAN_ZPOOL:-mpool}
containers=${DEADMAN_CONTAINERS:-nginx lab_nginx grafana homeassistant vault gitlab}
# Grafana has no fixed address; it is reached through lab_nginx, which is
# what users and alert links go through anyway. --resolve skips DNS (pihole).
grafana_url=${DEADMAN_GRAFANA_URL:-https://grafana.lab.nkontur.com/api/health}
grafana_resolve=${DEADMAN_GRAFANA_RESOLVE:-grafana.lab.nkontur.com:443:10.3.32.1}
vault_url=${DEADMAN_VAULT_URL:-https://10.3.32.6:8200/v1/sys/health}

dry_run=0
force_fail=${DEADMAN_FORCE_FAIL:-0}
for arg in "$@"; do
    case $arg in
        --dry-run) dry_run=1 ;;
        --force-fail) force_fail=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

workdir=$(mktemp -d /run/deadman-check.XXXXXX) || exit 1
trap 'rm -rf "$workdir"' EXIT

# deadline CMD...: run CMD under `timeout`, but never wait longer than
# check_timeout + 10s for it. A process stuck in uninterruptible I/O (a disk
# that hangs instead of erroring) ignores SIGKILL, and `timeout` would wait
# for it forever; here it is abandoned and the check fails instead.
# Output goes to $workdir/out.
deadline() {
    local pid waited=0 limit=$(( (check_timeout + 10) * 2 ))
    timeout -k 5 "$check_timeout" "$@" >"$workdir/out" 2>&1 &
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge "$limit" ]; then
            kill -KILL "$pid" 2>/dev/null
            echo "hung (no exit after $(( limit / 2 ))s)" >"$workdir/out"
            return 124
        fi
        sleep 0.5
        waited=$(( waited + 1 ))
    done
    wait "$pid"
}

# First line of the last command's output, trimmed for the report.
detail() {
    head -c 300 "$workdir/out" | tr '\n' ' ' | sed 's/[[:space:]]*$//'
}

results=()
failures=0
record() {  # record ok|FAIL name detail
    results+=("$(printf '%-4s %-24s %s' "$1" "$2" "$3")")
    [ "$1" = ok ] || failures=$(( failures + 1 ))
}

# --- /persistent_data mounted and writable ---------------------------------
check_persistent() {
    if ! deadline mountpoint -q "$persistent_path"; then
        record FAIL persistent_data "not mounted at $persistent_path"
        return
    fi
    # shellcheck disable=SC2016  # $1 and $f are for the inner sh
    if deadline sh -c 'f=$(mktemp -p "$1" .deadman-check.XXXXXX) && rm -f "$f"' sh "$persistent_path"; then
        record ok persistent_data "mounted, writable"
    else
        record FAIL persistent_data "not writable: $(detail)"
    fi
}

# --- dockerd and the critical containers ------------------------------------
check_docker() {
    if ! deadline docker info --format '{{.ServerVersion}}'; then
        record FAIL dockerd "docker info failed: $(detail)"
        for c in $containers; do record FAIL "container:$c" "skipped (dockerd down)"; done
        return
    fi
    record ok dockerd "server $(detail)"
    local c state
    for c in $containers; do
        if ! deadline docker inspect --type container --format \
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$c"; then
            record FAIL "container:$c" "inspect failed: $(detail)"
            continue
        fi
        state=$(detail)
        case $state in
            "running healthy"|"running no-healthcheck") record ok "container:$c" "$state" ;;
            *) record FAIL "container:$c" "$state" ;;
        esac
    done
}

# --- Grafana /api/health ----------------------------------------------------
check_grafana() {
    if deadline curl -fsS -m "$check_timeout" --resolve "$grafana_resolve" "$grafana_url" \
        && grep -q '"database": *"ok"' "$workdir/out"; then
        record ok grafana "api/health database ok"
    else
        record FAIL grafana "api/health: $(detail)"
    fi
}

# --- Vault unsealed ---------------------------------------------------------
# sys/health answers 200 only when initialized, unsealed and active; the
# body is checked too, so a proxy error page cannot pass.
check_vault() {
    deadline curl -sS -k -m "$check_timeout" "$vault_url"
    if grep -q '"sealed": *false' "$workdir/out"; then
        record ok vault "unsealed"
    elif grep -q '"sealed": *true' "$workdir/out"; then
        record FAIL vault "SEALED"
    else
        record FAIL vault "sys/health: $(detail)"
    fi
}

# --- ZFS pool importable ----------------------------------------------------
# Imported and not FAULTED/UNAVAIL/SUSPENDED. DEGRADED passes: the pool is
# serving data, and disk faults are reported by their own alerts.
check_zpool() {
    if ! deadline zpool list -H -o health "$zpool_name"; then
        record FAIL "zpool:$zpool_name" "zpool list failed: $(detail)"
        return
    fi
    local health
    health=$(detail)
    case $health in
        ONLINE|DEGRADED) record ok "zpool:$zpool_name" "$health" ;;
        *) record FAIL "zpool:$zpool_name" "$health" ;;
    esac
}

check_persistent
check_docker
check_grafana
check_vault
check_zpool
if [ "$force_fail" = 1 ]; then
    record FAIL forced "DEADMAN_FORCE_FAIL=1 / --force-fail"
fi

if [ "$failures" -eq 0 ]; then status=ok; else status=fail; fi
report=$(
    echo "$(hostname -s) deadman-check: $status ($failures failed)"
    printf '%s\n' "${results[@]}"
)

if [ "$dry_run" = 1 ]; then
    echo "$report"
    echo "(dry run: no ping sent)"
    [ "$status" = ok ]
    exit
fi

if [ -z "${DEADMAN_PING_URL:-}" ] && [ -r "$env_file" ]; then
    # KEY=value lines only; the file is never executed.
    DEADMAN_PING_URL=$(sed -n 's/^DEADMAN_PING_URL=//p' "$env_file" | tail -1)
fi
if [ -z "${DEADMAN_PING_URL:-}" ]; then
    echo "$report"
    echo "DEADMAN_PING_URL is not set (env or $env_file); no ping sent" >&2
    exit 3
fi

url=${DEADMAN_PING_URL%/}
[ "$status" = ok ] || url="$url/fail"
echo "$report"
# The URL is a credential: it is never printed. Retries cover a brief WAN
# blip; if every attempt fails, the missing ping alerts after the grace time.
if printf '%s\n' "$report" | curl -fsS -m 10 --retry 3 --retry-delay 5 --retry-all-errors \
    --data-binary @- -o /dev/null "$url"; then
    echo "ping sent ($status)"
else
    echo "ping failed after retries" >&2
    exit 4
fi
[ "$status" = ok ]
