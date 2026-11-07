#!/bin/bash
# Pre-upgrade backup and restore for auto-merged version updates of services
# whose new version migrates its data one way (docs/AUTOMERGE.md,
# "Pre-upgrade backups"). Called by ansible/service-update.yml on the host.
#
#   pre-upgrade-backup.sh backup  --type T --project P --service S --dir DIR --new-image IMG [--rate MB]
#   pre-upgrade-backup.sh restore --type T --project P --service S --dir DIR [--rate MB]
#   pre-upgrade-backup.sh prune   --dir ROOT/<service> --keep N
#
# Types:
#   homeassistant  /config without the recorder database, logs and caches,
#                  copied after the container is stopped. The recorder database
#                  (SQLite) is copied only if the new image's recorder schema
#                  differs from the database's: online, from one read
#                  transaction (a consistent snapshot while HA keeps writing),
#                  throttled to --rate MB/s.
#   influxdb       influxd.bolt and influxd.sqlite (the metadata a 2.x minor
#                  migrates), copied after the container is stopped. The TSM
#                  data is not touched by a minor release and is not copied.
#
# backup stops the service's container (the deploy recreates it right after);
# its new image is pulled first, so the service is down only as long as a
# normal recreate takes. restore stops the container and puts the copied
# files back; the caller then recreates the service on its previous image.
#
# Backups live on the ZFS pool, never on /persistent_data: a full-speed 14 GB
# copy there saturated its SSD on 2026-10-20 and made Vault time out. Copies
# to or from /persistent_data are rate-limited (pv / the SQLite backup loop);
# ionice would not help, the disks use mq-deadline, which ignores it.
#
# A backup directory is complete only once it holds COMPLETE; restore refuses
# anything else. Nothing secret is copied out of /config that is not already
# there, and nothing is printed but paths, sizes and versions.
set -euo pipefail

CMD="${1:-}"; shift || true
TYPE="" PROJECT=docker SERVICE="" DIR="" NEW_IMAGE="" RATE=40 KEEP=2
while [ $# -gt 0 ]; do
  case "$1" in
    --type) TYPE="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --new-image) NEW_IMAGE="$2"; shift 2 ;;
    --rate) RATE="$2"; shift 2 ;;
    --keep) KEEP="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { echo "[$(date -u +%H:%M:%S)] [pre-upgrade ${SERVICE:-$TYPE}] $*"; }
die() { say "FAILED: $*"; exit 1; }

# Tests run against scratch directories outside ZFS (test_pre_upgrade.py).
REQUIRE_ZFS="${PRE_UPGRADE_REQUIRE_ZFS:-1}"
STOP_TIMEOUT="${PRE_UPGRADE_STOP_TIMEOUT:-120}"
# Paths excluded from the Home Assistant config copy (rsync patterns, relative
# to /config). The recorder database is handled separately; the rest is
# regenerated or not version-specific.
HA_EXCLUDES=(/home-assistant_v2.db /home-assistant_v2.db-wal /home-assistant_v2.db-shm
             /home-assistant_v2.db.restore '/home-assistant.log*' /.cache/ /deps/ /tts/)
HA_DB=home-assistant_v2.db
INFLUX_FILES=(influxd.bolt influxd.sqlite)

[[ "$RATE" =~ ^[0-9]+$ ]] && [ "$RATE" -gt 0 ] || die "--rate must be a positive integer (MB/s)"
[[ "$KEEP" =~ ^[0-9]+$ ]] && [ "$KEEP" -ge 1 ] || die "--keep must be >= 1"

check_fs() { # dir: backups go to ZFS only
  [ "$REQUIRE_ZFS" = 1 ] || return 0
  local fs
  fs=$(findmnt -n -o FSTYPE -T "$1") || die "cannot find the filesystem of $1"
  [ "$fs" = zfs ] || die "$1 is on $fs, not ZFS; refusing to write backups there"
}

container() {
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$SERVICE" | head -n 1
}

mount_source() { # container destination
  docker inspect -f "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Source}}{{end}}{{end}}" "$1"
}

stop_container() {
  local cid=$1
  if [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = true ] \
      || [ "$(docker inspect -f '{{.State.Restarting}}' "$cid")" = true ]; then
    say "stopping $(docker inspect -f '{{.Name}}' "$cid")"
    docker stop -t "$STOP_TIMEOUT" "$cid" >/dev/null
  fi
}

copy_throttled() { # src dst: at --rate MB/s, keeping owner and mode
  pv -q -L "${RATE}m" "$1" > "$2" || { rm -f "$2"; die "copy of $1 failed"; }
  chown --reference="$1" "$2"
  chmod --reference="$1" "$2"
}

human() { numfmt --to=iec --suffix=B "$1"; }

# --- Home Assistant -----------------------------------------------------------

ha_db_schema() { # db [immutable]: schema version of the recorder database
  # A file nothing writes (a copy, or the live one with HA stopped) is opened
  # immutable, so no -wal/-shm sidecar is created next to it.
  local db=$1
  [ "${2:-}" != immutable ] || db="file:$1?immutable=1"
  sqlite3 -readonly "$db" '.timeout 60000' \
    'SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1;'
}

ha_image_schema() { # image: recorder schema version the image's code writes
  docker run --rm --network none --entrypoint python3 "$1" -c \
    'from homeassistant.components.recorder.db_schema import SCHEMA_VERSION; print(SCHEMA_VERSION)'
}

sqlite_online_copy() { # src dst rate: one read transaction, rate-limited
  python3 - "$1" "$2" "$3" <<'EOF'
import sqlite3, sys, time
src, dst, rate = sys.argv[1], sys.argv[2], int(sys.argv[3]) * 1024 * 1024
# Read-only, with an explicit read transaction held for the whole copy: the
# backup steps all read the same snapshot, so writes by Home Assistant neither
# restart the copy nor make it inconsistent (WAL mode lets them continue).
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, isolation_level=None, timeout=60)
s.execute("BEGIN")
s.execute("SELECT count(*) FROM sqlite_master").fetchone()
page = s.execute("PRAGMA page_size").fetchone()[0]
d = sqlite3.connect(dst)
step = max(1, (4 * 1024 * 1024) // page)   # 4 MiB per step
start, done = time.monotonic(), [0]
def throttle(status, remaining, total):
    done[0] = total - remaining
    ahead = done[0] * page / rate - (time.monotonic() - start)
    if ahead > 0:
        time.sleep(ahead)
s.backup(d, pages=step, progress=throttle)
s.execute("COMMIT")
d.close()
print(f"copied {done[0] * page} bytes in {time.monotonic() - start:.0f}s")
EOF
}

ha_backup() {
  local cid=$1 cfg have want
  cfg=$(mount_source "$cid" /config)
  [ -n "$cfg" ] && [ -d "$cfg/.storage" ] || die "no Home Assistant /config mount with .storage found"
  if grep -Eq '^[[:space:]]*db_url:' "$cfg/configuration.yaml" 2>/dev/null; then
    die "the recorder uses db_url (an external database); only the SQLite recorder is supported"
  fi
  docker pull -q "$NEW_IMAGE" >/dev/null || die "cannot pull $NEW_IMAGE"

  if [ -f "$cfg/$HA_DB" ]; then
    have=$(ha_db_schema "$cfg/$HA_DB") || die "cannot read the recorder schema version"
    want=$(ha_image_schema "$NEW_IMAGE" 2>/dev/null) || want=""
    if [[ "$have" =~ ^[0-9]+$ ]] && [ "$want" = "$have" ]; then
      say "recorder schema $have is unchanged in the new image: database not copied"
      echo "db=skipped schema=$have" >> "$DIR/INFO"
    else
      say "recorder schema ${have:-?} -> ${want:-unknown}: copying the database online at ${RATE} MB/s ($(human "$(stat -c %s "$cfg/$HA_DB")"))"
      sqlite_online_copy "$cfg/$HA_DB" "$DIR/$HA_DB.partial" "$RATE" || die "database copy failed"
      [ "$(ha_db_schema "$DIR/$HA_DB.partial" immutable)" = "$have" ] || die "database copy does not read back"
      chown --reference="$cfg/$HA_DB" "$DIR/$HA_DB.partial"
      chmod --reference="$cfg/$HA_DB" "$DIR/$HA_DB.partial"
      mv "$DIR/$HA_DB.partial" "$DIR/$HA_DB"
      echo "db=copied schema=$have new_schema=${want:-unknown}" >> "$DIR/INFO"
    fi
  else
    say "no $HA_DB in /config: nothing to copy for the recorder"
    echo "db=absent" >> "$DIR/INFO"
  fi

  # Configuration: with Home Assistant stopped, so .storage is flushed and
  # nothing changes while it is copied (the deploy recreates it next anyway).
  stop_container "$cid"
  local ex=() p
  for p in "${HA_EXCLUDES[@]}"; do ex+=(--exclude="$p"); done
  rsync -a --bwlimit="$((RATE * 1024))" "${ex[@]}" "$cfg/" "$DIR/config/" || die "config copy failed"
  [ -f "$DIR/config/.storage/core.config_entries" ] || die "config copy has no .storage/core.config_entries"
  say "config copied ($(du -sh --apparent-size "$DIR/config" | cut -f1))"
}

ha_restore() {
  local cid=$1 cfg
  cfg=$(mount_source "$cid" /config)
  [ -n "$cfg" ] && [ -d "$cfg" ] || die "no Home Assistant /config mount found"
  [ -f "$DIR/config/.storage/core.config_entries" ] || die "backup has no config/.storage/core.config_entries"
  if [ -f "$DIR/$HA_DB" ]; then
    local need free
    need=$(stat -c %s "$DIR/$HA_DB")
    free=$(( $(stat -f -c '%a * %S' "$cfg" ) ))
    [ "$free" -gt $(( need + need / 10 )) ] || die "not enough free space in $cfg to restore the database"
  fi
  stop_container "$cid"
  local ex=() p
  for p in "${HA_EXCLUDES[@]}"; do ex+=(--exclude="$p"); done
  # --delete removes files the new version created; the excluded paths (the
  # recorder database, logs, caches) are neither copied nor deleted.
  rsync -a --delete --bwlimit="$((RATE * 1024))" "${ex[@]}" "$DIR/config/" "$cfg/" || die "config restore failed"
  say "config restored"
  if [ -f "$DIR/$HA_DB" ]; then
    say "restoring the recorder database at ${RATE} MB/s ($(human "$(stat -c %s "$DIR/$HA_DB")"))"
    copy_throttled "$DIR/$HA_DB" "$cfg/$HA_DB.restore"
    # The new version's WAL must not be replayed onto the restored file.
    rm -f "$cfg/$HA_DB-wal" "$cfg/$HA_DB-shm"
    mv -f "$cfg/$HA_DB.restore" "$cfg/$HA_DB"
    say "recorder database restored (schema $(ha_db_schema "$cfg/$HA_DB" immutable))"
  fi
}

# --- InfluxDB 2.x -------------------------------------------------------------

influx_data() { # container: sets DATA to the metadata directory (default paths only)
  local cid=$1 env
  env=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid")
  if grep -Eq '^INFLUXD_(BOLT_PATH|SQLITE_PATH|ENGINE_PATH)=' <<<"$env"; then
    die "INFLUXD_BOLT_PATH/SQLITE_PATH/ENGINE_PATH is set; only the default paths are supported"
  fi
  DATA=$(mount_source "$cid" /var/lib/influxdb2)
  [ -n "$DATA" ] && [ -f "$DATA/influxd.bolt" ] && [ -f "$DATA/influxd.sqlite" ] \
    || die "no /var/lib/influxdb2 mount with influxd.bolt and influxd.sqlite found"
}

influx_backup() {
  local cid=$1 data f
  influx_data "$cid"; data=$DATA
  docker pull -q "$NEW_IMAGE" >/dev/null || die "cannot pull $NEW_IMAGE"
  # Stopped, so bolt and SQLite are closed and consistent with each other.
  stop_container "$cid"
  for f in "${INFLUX_FILES[@]}" influxd.sqlite-wal; do
    if [ -e "$data/$f" ]; then cp -a "$data/$f" "$DIR/$f"; fi
  done
  (cd "$DIR" && sha256sum -- influxd.* > SHA256SUMS)
  echo "files=$(cd "$DIR" && ls influxd.* | tr '\n' ' ')" >> "$DIR/INFO"
  say "metadata copied: $(cd "$DIR" && ls influxd.* | tr '\n' ' ')"
}

influx_restore() {
  local cid=$1 data f
  influx_data "$cid"; data=$DATA
  (cd "$DIR" && sha256sum --quiet -c SHA256SUMS) || die "backup checksums do not match"
  stop_container "$cid"
  # The new version's SQLite WAL must not be replayed onto the restored file.
  rm -f "$data/influxd.sqlite-wal" "$data/influxd.sqlite-shm"
  for f in "${INFLUX_FILES[@]}" influxd.sqlite-wal; do
    [ -e "$DIR/$f" ] || continue
    cp -a "$DIR/$f" "$data/$f.restore"
    mv -f "$data/$f.restore" "$data/$f"
  done
  say "metadata restored"
}

# --- commands -----------------------------------------------------------------

case "$CMD" in
  backup|restore)
    [ -n "$SERVICE" ] && [ -n "$DIR" ] || die "--service and --dir are required"
    case "$TYPE" in homeassistant|influxdb) ;; *) die "unknown --type '$TYPE'" ;; esac
    CID=$(container)
    [ -n "$CID" ] || die "no container for $PROJECT/$SERVICE"
    ;;
esac

case "$CMD" in
  backup)
    [ -n "$NEW_IMAGE" ] || die "--new-image is required"
    [ ! -e "$DIR" ] || die "$DIR already exists"
    mkdir -p -m 0700 "$(dirname "$DIR")"
    check_fs "$(dirname "$DIR")"
    mkdir -m 0700 "$DIR"
    { echo "type=$TYPE"; echo "service=$SERVICE"; echo "date=$(date -u +%FT%TZ)"
      echo "image=$(docker inspect -f '{{.Config.Image}}' "$CID")"; echo "new_image=$NEW_IMAGE"; } > "$DIR/INFO"
    start=$(date +%s)
    case "$TYPE" in
      homeassistant) ha_backup "$CID" ;;
      influxdb) influx_backup "$CID" ;;
    esac
    touch "$DIR/COMPLETE"
    say "backup complete in $(( $(date +%s) - start ))s: $DIR"
    ;;
  restore)
    [ -f "$DIR/COMPLETE" ] || die "$DIR is not a complete backup"
    grep -qx "type=$TYPE" "$DIR/INFO" || die "$DIR is not a $TYPE backup"
    start=$(date +%s)
    case "$TYPE" in
      homeassistant) ha_restore "$CID" ;;
      influxdb) influx_restore "$CID" ;;
    esac
    say "restore complete in $(( $(date +%s) - start ))s from $DIR"
    ;;
  prune)
    # Keep the newest --keep backups of one service (directory names start
    # with a UTC timestamp, so they sort by age); remove the rest.
    [ -n "$DIR" ] && [ -d "$DIR" ] || die "--dir must be an existing directory"
    check_fs "$DIR"
    mapfile -t all < <(find "$DIR" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}T[0-9]{6}Z-[^/]+' | sort)
    n=${#all[@]}
    for ((i = 0; i < n - KEEP; i++)); do
      say "removing old backup ${all[i]}"
      rm -rf -- "${all[i]}"
    done
    say "kept $(( n < KEEP ? n : KEEP )) backup(s) in $DIR"
    ;;
  *)
    echo "usage: $0 backup|restore|prune ..." >&2; exit 2 ;;
esac
