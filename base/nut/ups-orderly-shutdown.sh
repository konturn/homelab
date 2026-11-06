#!/bin/bash
# Orderly power-fail shutdown (deployed as /usr/local/sbin/ups-orderly-shutdown).
#
# upsmon runs this as SHUTDOWNCMD ("... --execute") after it has written
# POWERDOWNFLAG. Steps, each with a timeout so a hung step cannot eat the
# battery:
#   1. running libvirt VMs (excel-vm): virsh shutdown, wait, then virsh destroy
#   2. docker compose stop for the main compose project
#   3. sync
#   4. /sbin/shutdown -h +0 — the normal shutdown then stops the remaining
#      services, exports the ZFS pool, closes LUKS, and at the very end
#      nutshutdown tells the UPS to cut and later restore its outlets.
# When battery.runtime is already below FAST_BELOW_SECONDS (the LOWBATT
# backstop path), the VM and container timeouts are shortened.
#
# Without --execute, or with DRY_RUN=1, it only logs what it would do.
# Step 4 runs even if earlier steps fail.
# Log: syslog tag ups-shutdown, and $LOG_FILE.
set -u

EXECUTE=0
for arg in "$@"; do
    case "$arg" in
        --execute) EXECUTE=1 ;;
        --dry-run) EXECUTE=0 ;;
        *) echo "usage: $0 [--execute|--dry-run]" >&2; exit 2 ;;
    esac
done
if [ -n "${DRY_RUN:-}" ] && [ "${DRY_RUN}" != "0" ]; then
    EXECUTE=0
fi

UPS="${UPS:-apc@localhost}"
VM_TIMEOUT="${VM_TIMEOUT:-90}"
DOCKER_STOP_TIMEOUT="${DOCKER_STOP_TIMEOUT:-30}"
DOCKER_TOTAL_TIMEOUT="${DOCKER_TOTAL_TIMEOUT:-150}"
FAST_BELOW_SECONDS="${FAST_BELOW_SECONDS:-300}"
FAST_VM_TIMEOUT="${FAST_VM_TIMEOUT:-30}"
FAST_DOCKER_STOP_TIMEOUT="${FAST_DOCKER_STOP_TIMEOUT:-10}"
FAST_DOCKER_TOTAL_TIMEOUT="${FAST_DOCKER_TOTAL_TIMEOUT:-45}"
COMPOSE_FILE="${COMPOSE_FILE:-/persistent_data/application/ansible_state/docker-compose.yml}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-docker}"
LOG_FILE="${LOG_FILE:-/var/log/ups-shutdown.log}"
POLL_SECONDS="${POLL_SECONDS:-3}"

UPSC="${UPSC_BIN:-upsc}"
VIRSH="${VIRSH_BIN:-virsh}"
DOCKER="${DOCKER_BIN:-docker}"
SHUTDOWN="${SHUTDOWN_BIN:-/sbin/shutdown}"
SYNC="${SYNC_BIN:-sync}"
LOGGER="${LOGGER_BIN:-logger}"
SLEEP="${SLEEP_BIN:-sleep}"

log() {
    local msg
    msg="$(date '+%F %T') $*"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
    echo "$msg" >&2
    "$LOGGER" -t ups-shutdown -- "$*" 2>/dev/null || true
}

# run CMD...: execute, or only log under dry run
run() {
    if [ "$EXECUTE" -eq 1 ]; then
        log "run: $*"
        "$@"
    else
        log "DRY_RUN: would run: $*"
        return 0
    fi
}

stop_vms() {
    local timeout="$1" doms dom waited remaining
    if ! command -v "$VIRSH" >/dev/null 2>&1; then
        log "virsh not found; skipping VMs"
        return 0
    fi
    doms="$(timeout 20 "$VIRSH" list --name --state-running 2>/dev/null | sed '/^$/d')"
    if [ -z "$doms" ]; then
        log "no running VMs"
        return 0
    fi
    for dom in $doms; do
        run timeout 20 "$VIRSH" shutdown "$dom" || log "virsh shutdown $dom failed"
    done
    if [ "$EXECUTE" -ne 1 ]; then
        log "DRY_RUN: would wait up to ${timeout}s for: $(echo "$doms" | tr '\n' ' '), then virsh destroy what is left"
        return 0
    fi
    waited=0
    while [ "$waited" -lt "$timeout" ]; do
        remaining="$(timeout 20 "$VIRSH" list --name --state-running 2>/dev/null | sed '/^$/d')"
        [ -z "$remaining" ] && break
        "$SLEEP" "$POLL_SECONDS"
        waited=$((waited + POLL_SECONDS))
    done
    remaining="$(timeout 20 "$VIRSH" list --name --state-running 2>/dev/null | sed '/^$/d')"
    for dom in $remaining; do
        log "VM $dom still running after ${timeout}s; destroying"
        run timeout 20 "$VIRSH" destroy "$dom" || log "virsh destroy $dom failed"
    done
    [ -z "$remaining" ] && log "all VMs stopped after ${waited}s"
    return 0
}

stop_containers() {
    local per="$1" total="$2"
    if ! command -v "$DOCKER" >/dev/null 2>&1; then
        log "docker not found; skipping containers"
        return 0
    fi
    if [ -f "$COMPOSE_FILE" ]; then
        run timeout "$total" "$DOCKER" compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" stop -t "$per" \
            || log "docker compose stop failed or timed out (the system shutdown stops docker anyway)"
    else
        log "compose file $COMPOSE_FILE missing; stopping all containers instead"
        # shellcheck disable=SC2046
        run timeout "$total" "$DOCKER" stop -t "$per" $("$DOCKER" ps -q) \
            || log "docker stop failed or timed out"
    fi
}

main() {
    local runtime vm_t per total mode
    [ "$EXECUTE" -eq 1 ] && mode="EXECUTE" || mode="DRY_RUN"
    log "=== UPS orderly shutdown starting ($mode)"
    log "ups.status=$(timeout 10 "$UPSC" "$UPS" ups.status 2>/dev/null) battery.charge=$(timeout 10 "$UPSC" "$UPS" battery.charge 2>/dev/null)"
    runtime="$(timeout 10 "$UPSC" "$UPS" battery.runtime 2>/dev/null | cut -d. -f1)"
    vm_t="$VM_TIMEOUT"; per="$DOCKER_STOP_TIMEOUT"; total="$DOCKER_TOTAL_TIMEOUT"
    if [ -n "$runtime" ] && [ "$runtime" -eq "$runtime" ] 2>/dev/null && [ "$runtime" -lt "$FAST_BELOW_SECONDS" ]; then
        vm_t="$FAST_VM_TIMEOUT"; per="$FAST_DOCKER_STOP_TIMEOUT"; total="$FAST_DOCKER_TOTAL_TIMEOUT"
        log "battery.runtime ${runtime}s < ${FAST_BELOW_SECONDS}s: fast mode"
    else
        log "battery.runtime ${runtime:-unknown}s: normal timeouts"
    fi
    log "step 1/4: VMs (timeout ${vm_t}s)"
    stop_vms "$vm_t"
    log "step 2/4: containers (docker stop -t ${per}, overall ${total}s)"
    stop_containers "$per" "$total"
    log "step 3/4: sync"
    run "$SYNC"
    log "step 4/4: system shutdown"
    run "$SHUTDOWN" -h +0 "UPS power failure: orderly shutdown"
    log "=== UPS orderly shutdown handed over to the system shutdown ($mode)"
}

main
