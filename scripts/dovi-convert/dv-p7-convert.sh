#!/usr/bin/env bash
# dv-p7-convert.sh: convert Dolby Vision Profile 7 files to Profile 8.1, one
# at a time, from the inventory written by dv-p7-scan.py.
#
# Usage:
#   dv-p7-convert.sh --key <ratingKey> [--key ...]   # specific items
#   dv-p7-convert.sh --all [--limit N]               # every pending item
#   options: --keep-going (default: stop at the first failure), --dry-run
#
# Per file:
#   1. waits until it may run (see "Guards"), then runs dv-p7-to-p8.sh in the
#      dovi-convert image: input mounted read-only, intermediates on the
#      scratch dataset, output in a hidden work dir on the media dataset;
#   2. if every validation check passed and the original is unchanged:
#      hard-links the original into the hidden holding dir as
#      <name>.p7-original.mkv.bak, then atomically renames the new file over
#      the original path (same filename, so Plex keeps metadata/watch state);
#   3. partial-scans that one folder in Plex, analyzes that one item, and
#      waits for Plex to report DOVIProfile 8.
#   Originals are never deleted by this script.
#
# Guards (checked before each file and every 30 s while one runs; the
# container is paused with `docker pause` and resumed when clear):
#   - quiet window 02:15-04:00 local time;
#   - any active Plex session (/status/sessions);
#   - a scrub or resilver in progress on the pool.
#
# State, logs and reports: $DV_STATE_DIR (default /root/dv-p7, root-only).
set -euo pipefail

STATE_DIR="${DV_STATE_DIR:-/root/dv-p7}"
INVENTORY="${DV_INVENTORY:-$STATE_DIR/inventory.json}"
IMAGE="${DV_IMAGE:-gitlab-registry.lab.nkontur.com:443/root/homelab/dovi-convert:latest}"
POOL="${DV_POOL:-mpool}"
MEDIA="/mpool/plex"                 # /data in the Plex container
SCRATCH="${DV_SCRATCH:-/mpool/dv-scratch}"
HOLD="$MEDIA/.dv-p7-originals"      # same dataset: hard link, no copy
WORK="$MEDIA/.dv-p7-work"           # same dataset: atomic rename
QUIET_FROM=0215 QUIET_TO=0400
PREFS="$MEDIA/config/Library/Application Support/Plex Media Server/Preferences.xml"

log() { printf '%s %s\n' "$(date +%FT%T%z)" "$*" | tee -a "$STATE_DIR/convert.log" >&2; }
die() { log "ERROR: $*"; exit 1; }

keys=() all=false limit=0 keep_going=false dry=false
while [ $# -gt 0 ]; do
  case "$1" in
    --key) keys+=("$2"); shift 2 ;;
    --all) all=true; shift ;;
    --limit) limit="$2"; shift 2 ;;
    --keep-going) keep_going=true; shift ;;
    --dry-run) dry=true; shift ;;
    *) die "unknown argument $1" ;;
  esac
done
[ "$(id -u)" = 0 ] || die "run as root"
umask 077
mkdir -p "$STATE_DIR"/{state,reports,logs}
[ -f "$INVENTORY" ] || die "no inventory at $INVENTORY (run dv-p7-scan.py)"
mountpoint -q "$SCRATCH" || die "scratch dataset not mounted at $SCRATCH"
exec 9>"$STATE_DIR/.lock"; flock -n 9 || die "another dv-p7-convert.sh is running"
# leftovers from an interrupted run (we hold the lock, so nothing else uses them)
rm -rf "$SCRATCH"/dvconv.* "$WORK"/*.mkv 2>/dev/null || true
cname=""
on_exit() { # never leave a paused or orphaned conversion behind
  if [ -n "$cname" ] && docker inspect "$cname" >/dev/null 2>&1; then
    log "interrupted: removing $cname (original untouched)"
    docker rm -f "$cname" >/dev/null 2>&1 || true
  fi
}
trap on_exit EXIT
trap 'exit 130' INT TERM

# --- Plex -------------------------------------------------------------------
PLEX="http://$(docker inspect plex --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $NF}'):32400"
plex() { # plex <method> <path> [curl args]; token never on the command line
  local method="$1" path="$2"; shift 2
  local tok; tok=$(grep -o 'PlexOnlineToken="[^"]*"' "$PREFS" | cut -d'"' -f2)
  curl -sf -X "$method" -H 'Accept: application/json' \
    --header @<(printf 'X-Plex-Token: %s\n' "$tok") "$@" "$PLEX$path"
}

# --- guards -----------------------------------------------------------------
blocker() { # prints a reason when work must not run now
  local now; now=$(date +%H%M)
  if [ "$((10#$now))" -ge "$((10#$QUIET_FROM))" ] && [ "$((10#$now))" -lt "$((10#$QUIET_TO))" ]; then
    echo "quiet window ${QUIET_FROM}-${QUIET_TO}"; return
  fi
  if zpool status "$POOL" | grep -qE '(scrub|resilver) in progress'; then
    echo "scrub/resilver in progress on $POOL"; return
  fi
  local n; n=$(plex GET /status/sessions | jq -r '.MediaContainer.size // 0') || { echo "Plex unreachable"; return; }
  [ "$n" = 0 ] || echo "$n active Plex session(s)"
}

wait_clear() { # exponential backoff, capped at 5 minutes
  local delay=30 why
  while why=$(blocker) && [ -n "$why" ]; do
    log "waiting: $why (next check in ${delay}s)"
    sleep "$delay"; delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
  done
}

# --- one file ---------------------------------------------------------------
convert_one() {
  local key="$1" row src rel size_inv sec out report clog rc paused=false why
  row=$(jq -c --arg k "$key" '.files[] | select(.ratingKey == $k)' "$INVENTORY" | head -1)
  [ -n "$row" ] || { log "[$key] not in inventory"; return 1; }
  src=$(jq -r .host_path <<<"$row"); sec=$(jq -r .sectionID <<<"$row")
  size_inv=$(jq -r .size <<<"$row")
  rel="${src#"$MEDIA"/}"
  [ -f "$STATE_DIR/state/$key.done" ] && { log "[$key] already done"; return 0; }
  [ -f "$src" ] || { log "[$key] source missing"; return 1; }
  [ "$(stat -c %s "$src")" = "$size_inv" ] || { log "[$key] size differs from inventory; rescan first"; return 1; }
  [ "$(jq -r '.probe.dv_profile // empty' <<<"$row")" = 7 ] || { log "[$key] probe does not confirm profile 7"; return 1; }

  out="$WORK/$key.mkv"; report="$STATE_DIR/reports/$key.json"; clog="$STATE_DIR/logs/$key.log"
  cname="dv-p7-$key"
  if $dry; then log "[$key] dry run: would convert $(jq -r .title <<<"$row")"; return 0; fi
  mkdir -p "$WORK" "$HOLD"; chmod 700 "$WORK" "$HOLD"
  rm -f "$out"
  local st_before; st_before=$(stat -c '%s %Y %i' "$src")

  wait_clear
  log "[$key] start: $(jq -r .title <<<"$row") ($(numfmt --to=iec "$size_inv"))"
  local t0; t0=$(date +%s)
  docker rm -f "$cname" >/dev/null 2>&1 || true
  docker run -d --name "$cname" --cpus 4 --memory 4g \
    -v "$MEDIA:/data:ro" -v "$WORK:/work" -v "$SCRATCH:/scratch" \
    -v "$STATE_DIR/reports:/reports" \
    "$IMAGE" "/data/$rel" "/work/$key.mkv" /scratch "/reports/$key.json" >/dev/null

  while [ "$(docker inspect -f '{{.State.Running}}' "$cname")" = true ]; do
    why=$(blocker)
    if [ -n "$why" ] && ! $paused; then
      docker pause "$cname" >/dev/null && paused=true && log "[$key] paused: $why"
    elif [ -z "$why" ] && $paused; then
      docker unpause "$cname" >/dev/null && paused=false && log "[$key] resumed"
    fi
    sleep 30
  done
  rc=$(docker wait "$cname")
  docker logs "$cname" >"$clog" 2>&1 || true
  docker rm "$cname" >/dev/null; cname=""
  local secs=$(( $(date +%s) - t0 ))

  if [ "$rc" != 0 ] || [ "$(jq -r .ok "$report" 2>/dev/null)" != true ]; then
    log "[$key] FAILED (exit $rc, ${secs}s); original untouched; see $clog"
    rm -f "$out"; touch "$STATE_DIR/state/$key.failed"; return 1
  fi
  [ "$(stat -c '%s %Y %i' "$src")" = "$st_before" ] || {
    log "[$key] original changed during conversion; discarding output"; rm -f "$out"; return 1; }

  # swap: keep the original as a hard link in the holding dir, then rename
  # the new file over the original path in one step
  local bak="$HOLD/${rel%.*}.p7-original.mkv.bak"
  mkdir -p "$(dirname "$bak")"
  [ ! -e "$bak" ] || { log "[$key] $bak exists; refusing to overwrite"; rm -f "$out"; return 1; }
  ln "$src" "$bak"
  chown --reference="$src" "$out"; chmod --reference="$src" "$out"
  mv -T "$out" "$src"
  [ "$(stat -c '%s %i' "$bak")" = "$(cut -d' ' -f1,3 <<<"$st_before")" ] || die "[$key] holding copy check failed"
  log "[$key] swapped in ($(numfmt --to=iec "$(stat -c %s "$src")"), ${secs}s); original kept at $bak"
  jq -n --arg key "$key" --arg src "$src" --arg bak "$bak" --argjson secs "$secs" \
    '{ratingKey:$key, path:$src, original:$bak, seconds:$secs, at:(now|todate)}' >"$STATE_DIR/state/$key.done"

  # Plex: rescan only this folder, analyze only this item
  local cdir; cdir="/data/$(dirname "$rel")"
  plex GET "/library/sections/$sec/refresh" -G --data-urlencode "path=$cdir" >/dev/null || log "[$key] Plex folder refresh request failed"
  sleep 20
  plex PUT "/library/metadata/$key/analyze" >/dev/null || log "[$key] Plex analyze request failed"
  local delay=15 waited=0 prof=""
  while [ "$waited" -lt 900 ]; do
    prof=$(plex GET "/library/metadata/$key" | jq -r --arg f "/data/$rel" \
      '[.MediaContainer.Metadata[0].Media[].Part[] | select(.file == $f) | .Stream[]? | select(.streamType == 1) | .DOVIProfile][0] // empty')
    [ "$prof" = 8 ] && break
    sleep "$delay"; waited=$(( waited + delay )); delay=$(( delay * 2 > 300 ? 300 : delay * 2 ))
  done
  if [ "$prof" = 8 ]; then log "[$key] Plex reports DOVIProfile 8"
  else log "[$key] WARNING: Plex still reports DOVIProfile '${prof:-?}' after ${waited}s"; fi
  jq --arg p "$prof" '. + {plex_dovi_profile:$p}' "$STATE_DIR/state/$key.done" >"$STATE_DIR/state/$key.done.tmp" \
    && mv "$STATE_DIR/state/$key.done.tmp" "$STATE_DIR/state/$key.done"
}

# --- main -------------------------------------------------------------------
if $all; then
  mapfile -t keys < <(jq -r '.files[] | select(.probe.dv_profile == 7) | .ratingKey' "$INVENTORY" | awk '!seen[$0]++')
fi
[ "${#keys[@]}" -gt 0 ] || die "nothing to do: pass --key or --all"
log "image $IMAGE; ${#keys[@]} candidate item(s)"
done_n=0 fail_n=0
for key in "${keys[@]}"; do
  [ -f "$STATE_DIR/state/$key.done" ] && continue
  if [ "$limit" -gt 0 ] && [ "$(( done_n + fail_n ))" -ge "$limit" ]; then break; fi
  if convert_one "$key"; then done_n=$(( done_n + 1 ))
  else
    fail_n=$(( fail_n + 1 ))
    $keep_going || { log "stopping after failure (use --keep-going to continue)"; break; }
  fi
done
log "finished: $done_n converted, $fail_n failed"
[ "$fail_n" = 0 ]
