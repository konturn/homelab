#!/bin/bash
# Pull the zwave-js-ui store (Docker volume) from the Z-Wave Pi over SSH and
# keep it as a dated tar.gz on the router. Managed by Ansible
# (ansible/roles/zwave-backup); configuration comes from the systemd unit.
#
# Host down (no ping reply): log, refresh last_ok, exit 0. The host-down
# signals (network_devices, telegraf_hosts, zwave_influx_data) already alert
# on that, so the backup signal stays quiet instead of alerting twice.
# Host up but the backup fails: exit non-zero, last_ok is not refreshed, and
# the zwave_backup anomaly signal flags it once last_ok is too old.
set -euo pipefail

: "${ZWAVE_HOST:?}" "${ZWAVE_USER:?}" "${ZWAVE_VOLUME:?}" "${BACKUP_DIR:?}" "${STATE_DIR:?}" "${KEEP:?}"

log() { echo "zwave-backup: $*"; }

stamp() {
    # $1 = file name under STATE_DIR. ISO 8601 UTC, world-readable (telegraf).
    local f="$STATE_DIR/$1"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$f.tmp"
    chmod 0644 "$f.tmp"
    mv -f "$f.tmp" "$f"
}

mkdir -p "$BACKUP_DIR" "$STATE_DIR"
chmod 0700 "$BACKUP_DIR"

if ! ping -c 3 -W 2 -q "$ZWAVE_HOST" > /dev/null 2>&1; then
    log "host $ZWAVE_HOST does not answer ping; skipped (covered by the host-down alerts)"
    stamp last_ok
    exit 0
fi

ts=$(date +%Y%m%d-%H%M%S)
final="$BACKUP_DIR/zwave-store-$ts.tar.gz"
part="$BACKUP_DIR/.zwave-store-$ts.tar.gz.part"
trap 'rm -f "$part"' EXIT
umask 077

# Remote side: archive the volume's directory as it is on disk. logs/ and the
# downloaded device config DB (.config-db, re-fetched by zwave-js-ui) are left
# out; store/backups/ (zwave-js-ui's own NVM/store backups) is included.
# GNU tar exits 1 when a file changed while it was read; the archive is still
# complete, so that is accepted. Anything above 1 is a failure.
# shellcheck disable=SC2016 # expanded on the remote host
remote='set -u
mp=$(docker volume inspect -f "{{.Mountpoint}}" "$1") || exit 3
[ -f "$mp/settings.json" ] || { echo "no settings.json in $mp" >&2; exit 4; }
rc=0
nice -n 10 tar -C "$mp" --exclude=./logs --exclude=./.config-db --warning=no-file-changed -czf - . || rc=$?
[ "$rc" -le 1 ]'

rc=0
ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
    -o StrictHostKeyChecking=yes \
    "$ZWAVE_USER@$ZWAVE_HOST" "bash -c $(printf '%q' "$remote") _ $(printf '%q' "$ZWAVE_VOLUME")" \
    > "$part" || rc=$?
if [ "$rc" -ne 0 ]; then
    log "pull from $ZWAVE_HOST failed (exit $rc; 255 = ssh, 3 = no $ZWAVE_VOLUME volume, 4 = no settings.json, 1 = tar)"
    exit 1
fi

# Sanity: a readable gzip/tar stream that contains the settings file.
# List to a variable first: piping tar into grep -q fails under pipefail,
# because grep exits at the first match and tar then gets EPIPE (systemd
# ignores SIGPIPE, so tar prints "stdout: write error" and exits 2).
if ! listing=$(tar -tzf "$part"); then
    log "archive from $ZWAVE_HOST is unreadable"
    exit 1
fi
if ! grep -qx './settings.json' <<< "$listing"; then
    log "archive from $ZWAVE_HOST has no settings.json"
    exit 1
fi

mv -f "$part" "$final"
trap - EXIT
log "wrote $final ($(stat -c %s "$final") bytes)"

# Retention: newest $KEEP archives.
find "$BACKUP_DIR" -maxdepth 1 -name 'zwave-store-*.tar.gz' -printf '%T@ %p\n' \
    | sort -rn | tail -n +"$((KEEP + 1))" | cut -d' ' -f2- \
    | while read -r old; do rm -f -- "$old" && log "removed $old"; done

stamp last_success
stamp last_ok
log "ok"
