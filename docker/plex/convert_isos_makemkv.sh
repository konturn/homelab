#!/usr/bin/env bash
# Convert Blu-ray ISOs under the Plex movie library to MKV with MakeMKV.
#
# Deployed by the configure-router-network role to /root/cron/convert_isos.sh
# (rendered with ansible.builtin.template, so this file must stay free of
# Jinja delimiters) and run nightly from root's crontab.
#
# For each ISO it scans the disc, picks the main feature (not title 0, which
# is often a trailer or extra), rips it to a temp dir on the same dataset,
# checks the result with ffprobe against the runtime Radarr reports, then
# renames it into place. A failed ISO gets a "<iso>.failed" marker holding the
# reason, and no partial MKV is left behind.
#
# Modes:
#   nightly (default)  every ISO under ROOT_DIR without a finished MKV
#   list               ISO_LIST=/path/to/file (one ISO path per line), or
#                      ISO paths as arguments. Retries failed ISOs and redoes
#                      ISOs that already have an MKV; on success the old MKV
#                      is moved to QUARANTINE_DIR, never deleted.
#
# Every run ends with one "iso-convert summary:" line.
#
# MakeMKV itself is managed by the same role (tasks/makemkv.yml): the version
# is pinned in its defaults (makemkv_version, built from the vendor tarballs
# when the installed one differs), and the registration key comes from Vault
# (homelab/services/makemkv, app_key) into /root/.MakeMKV/settings.conf
# (0600). To upgrade: bump makemkv_version and both SHA-256s, then dry-run
# `makemkvcon -r --cache=1 info <some.iso>` on the router after the deploy.
# The makemkv snap (/snap/bin) is not used: cron's PATH has /usr/bin only.
set -uo pipefail

########################################
# CONFIG (env overrides allowed)
########################################
ROOT_DIR="${ROOT_DIR:-/mpool/plex/Movies}"
QUARANTINE_DIR="${QUARANTINE_DIR:-/mpool/plex/.iso-convert-quarantine}"
# Rip scratch space. Must be on the same dataset as ROOT_DIR so the final
# rename is atomic, and outside the library so Plex/Radarr never see partials.
WORK_ROOT="${WORK_ROOT:-/mpool/plex/.iso-convert-work}"
DELETE_ISO="false"
MKVCON="${MKVCON:-makemkvcon}"
MAX_PARALLEL_JOBS="${MAX_PARALLEL_JOBS:-1}"
CONVERSION_TIMEOUT="${CONVERSION_TIMEOUT:-86400}"
# Titles shorter than this are ignored when scanning. Title numbers depend on
# it, so the scan and the rip must use the same value.
MIN_TITLE_SECONDS="${MIN_TITLE_SECONDS:-900}"
# Verification: ripped duration must be >= MIN_RUNTIME_RATIO x Radarr runtime,
# or >= FALLBACK_MIN_SECONDS when Radarr has no runtime for the movie.
MIN_RUNTIME_RATIO="${MIN_RUNTIME_RATIO:-0.85}"
FALLBACK_MIN_SECONDS="${FALLBACK_MIN_SECONDS:-3600}"
# Radarr (runtime lookup) and Plex (partial scan after a rip). Both optional.
RADARR_CONTAINER="${RADARR_CONTAINER:-radarr}"
RADARR_CONFIG="${RADARR_CONFIG:-/persistent_data/application/radarr/config.xml}"
RADARR_MOVIES_ROOT="${RADARR_MOVIES_ROOT:-/movies}"   # ROOT_DIR inside radarr
PLEX_CONTAINER="${PLEX_CONTAINER:-plex}"
PLEX_PREFS="${PLEX_PREFS:-/mpool/plex/config/Library/Application Support/Plex Media Server/Preferences.xml}"
PLEX_HOST_ROOT="${PLEX_HOST_ROOT:-/mpool/plex}"       # mounted at /data in plex
PLEX_CONTAINER_ROOT="${PLEX_CONTAINER_ROOT:-/data}"
PLEX_SCAN="${PLEX_SCAN:-true}"
# Load gate: before each disc, wait while the 1-minute load average is above
# MAX_LOAD (the router also runs DNS and everything else). After
# LOAD_WAIT_MAX seconds the disc is skipped for this run (no failure marker).
MAX_LOAD="${MAX_LOAD:-32}"
LOAD_WAIT_MAX="${LOAD_WAIT_MAX:-21600}"
# Locks and scratch live on tmpfs, not next to the media.
RUN_DIR="${RUN_DIR:-/run/iso-convert}"
ISO_LIST="${ISO_LIST:-}"
########################################

ts() { date +%H:%M:%S; }
log() { echo "[$(ts)] [PID:$$] $*"; }

if ! command -v "$MKVCON" >/dev/null 2>&1; then
    echo "ERROR: makemkvcon not found in PATH."
    exit 1
fi
for bin in ffprobe python3 flock curl; do
    command -v "$bin" >/dev/null 2>&1 || { echo "ERROR: $bin not found in PATH."; exit 1; }
done

# makemkvcon reads its registration key from $HOME/.MakeMKV/settings.conf.
# Without it (e.g. under systemd-run, which sets no HOME) an expired build
# refuses every disc, which would mark every ISO as failed.
export HOME="${HOME:-/root}"
# Capture first: grep -q in a pipeline would SIGPIPE makemkvcon and, with
# pipefail, hide the match.
MKV_PREFLIGHT=$("$MKVCON" -r info disc:9999 2>&1)
if grep -q '^MSG:5021,' <<<"$MKV_PREFLIGHT"; then
    echo "iso-convert summary: aborted, makemkvcon refuses to run (MSG 5021: version too old / no valid registration key in $HOME/.MakeMKV/settings.conf)"
    exit 1
fi

mkdir -p "$RUN_DIR/locks" "$WORK_ROOT"
chmod 700 "$RUN_DIR"

# One run at a time (nightly cron vs a manual list run).
exec 8>"$RUN_DIR/run.lock"
if ! flock -n 8; then
    echo "iso-convert summary: skipped, another run holds $RUN_DIR/run.lock"
    exit 0
fi

RUN_TMP=$(mktemp -d "$RUN_DIR/run.XXXXXX")
cleanup_run() { rm -rf -- "$RUN_TMP"; }
trap cleanup_run EXIT

RESULTS="$RUN_TMP/results"
RUNTIMES="$RUN_TMP/runtimes.tsv"
: >"$RESULTS"
: >"$RUNTIMES"
START_TS=$(date +%s)

container_ip() {
    docker inspect "$1" 2>/dev/null | python3 -c '
import json, sys
try:
    nets = json.load(sys.stdin)[0]["NetworkSettings"]["Networks"]
except Exception:
    sys.exit(0)
for n in nets.values():
    if n.get("IPAddress"):
        print(n["IPAddress"]); break
'
}

# Radarr runtimes as "<top-level folder>\t<minutes>". The API key is read from
# Radarr's config and handed to curl on stdin, so it never hits argv or logs.
load_radarr_runtimes() {
    local ip key
    ip=$(container_ip "$RADARR_CONTAINER")
    key=$(sed -n 's:.*<ApiKey>\(.*\)</ApiKey>.*:\1:p' "$RADARR_CONFIG" 2>/dev/null | head -n 1)
    if [[ -z "$ip" || -z "$key" ]]; then
        log "WARN: Radarr not reachable (container IP or API key missing); runtime checks fall back to ${FALLBACK_MIN_SECONDS}s"
        return 0
    fi
    printf 'X-Api-Key: %s\n' "$key" \
        | curl -fsS -m 60 -H @- "http://$ip:7878/api/v3/movie" 2>/dev/null \
        | RADARR_MOVIES_ROOT="$RADARR_MOVIES_ROOT" python3 -c '
import json, os, sys
root = os.environ["RADARR_MOVIES_ROOT"].rstrip("/") + "/"
try:
    movies = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for m in movies:
    p = m.get("path") or ""
    if p.startswith(root) and m.get("runtime"):
        print(p[len(root):].split("/")[0] + "\t" + str(m["runtime"]))
' >"$RUNTIMES"
    if [[ ! -s "$RUNTIMES" ]]; then
        log "WARN: Radarr API returned no runtimes; runtime checks fall back to ${FALLBACK_MIN_SECONDS}s"
    fi
}

# Plex section id + token for partial scans, written to a 0600 file.
load_plex() {
    [[ "$PLEX_SCAN" == "true" ]] || return 0
    local ip token
    ip=$(container_ip "$PLEX_CONTAINER")
    token=$(grep -o 'PlexOnlineToken="[^"]*"' "$PLEX_PREFS" 2>/dev/null | cut -d'"' -f2)
    if [[ -z "$ip" || -z "$token" ]]; then
        log "WARN: Plex not reachable; skipping partial scans"
        return 0
    fi
    (umask 077; printf 'X-Plex-Token: %s\n' "$token" >"$RUN_TMP/plex.hdr")
    echo "$ip" >"$RUN_TMP/plex.ip"
}

# Ask Plex to rescan one movie directory (host path).
plex_scan_dir() {
    local dir="$1" ip section cpath
    [[ -f "$RUN_TMP/plex.hdr" ]] || return 0
    ip=$(cat "$RUN_TMP/plex.ip")
    cpath="$PLEX_CONTAINER_ROOT${dir#"$PLEX_HOST_ROOT"}"
    section=$(curl -fsS -m 20 -H @"$RUN_TMP/plex.hdr" "http://$ip:32400/library/sections" 2>/dev/null \
        | CPATH="$cpath" python3 -c '
import os, sys, xml.etree.ElementTree as ET
cpath = os.environ["CPATH"]
try:
    root = ET.fromstring(sys.stdin.read())
except Exception:
    sys.exit(0)
for d in root.iter("Directory"):
    for loc in d.iter("Location"):
        p = loc.get("path", "").rstrip("/") + "/"
        if cpath.startswith(p):
            print(d.get("key")); sys.exit(0)
')
    [[ -n "$section" ]] || { log "  WARN: no Plex section covers $cpath"; return 0; }
    if curl -fsS -m 20 -G -H @"$RUN_TMP/plex.hdr" --data-urlencode "path=$cpath" \
        "http://$ip:32400/library/sections/$section/refresh" >/dev/null 2>&1; then
        log "  Plex partial scan requested (section $section)"
    else
        log "  WARN: Plex partial scan request failed"
    fi
}

# Pick the main feature from `makemkvcon -r info` output on stdin.
# argv: runtime_minutes (0 = unknown). Prints "<title id>\t<explanation>" or
# "FAIL\t<reason>".
PY_SELECT='
import re, sys
runtime = int(sys.argv[1]) * 60
titles = {}
def title(tid):
    return titles.setdefault(tid, dict(id=tid, dur=0, chapters=0, size=0, src="", audio=0))
for line in sys.stdin:
    m = re.match(r"SINFO:(\d+),\d+,1,6202,", line)
    if m:
        title(int(m.group(1)))["audio"] += 1
        continue
    m = re.match(r"TINFO:(\d+),(\d+),\d+,\"(.*)\"\s*$", line)
    if not m:
        continue
    t, attr, val = title(int(m.group(1))), int(m.group(2)), m.group(3)
    if attr == 9:
        h, mi, s = (val.split(":") + ["0", "0"])[:3]
        t["dur"] = int(h) * 3600 + int(mi) * 60 + int(s)
    elif attr == 8:
        t["chapters"] = int(val or 0)
    elif attr == 11:
        t["size"] = int(val or 0)
    elif attr == 16:
        t["src"] = val
ts = [t for t in titles.values() if t["dur"] > 0]
if not ts:
    print("FAIL\tno titles found on disc"); sys.exit(0)
longest = max(t["dur"] for t in ts)
# Many near-identical playlists from different files is the usual
# playlist-obfuscation pattern; without a known-good playlist we cannot pick.
same = [t for t in ts if t["dur"] >= longest * 0.99]
if len(set(t["src"] for t in same)) >= 8:
    print("FAIL\tplaylist obfuscation suspected (%d near-identical playlists)" % len(same))
    sys.exit(0)
if runtime:
    # Long titles (within 10% of the longest) compete on closeness to the
    # Radarr runtime: alternate presentations (commentary/story-reel cuts,
    # localized versions) are often a few minutes longer than the film.
    near = [t for t in ts if t["dur"] >= longest * 0.90]
    # A "play all" or extras playlist can dwarf the film; if the longest is
    # far past the runtime and some title is close to it, use those.
    if longest > runtime * 1.35:
        close = [t for t in ts if runtime * 0.85 <= t["dur"] <= runtime * 1.25]
        if close:
            near = close
    best_gap = min(abs(t["dur"] - runtime) for t in near)
    near = [t for t in near if abs(t["dur"] - runtime) <= best_gap + 60]
    how = "closest to Radarr runtime" if len(near) < len(ts) else "only candidate"
else:
    near = [t for t in ts if t["dur"] >= longest - 60]
    how = "longest"
# Seamless-branching / angle duplicates: more audio tracks (the feature
# carries the full language set), then more chapters, then larger.
near.sort(key=lambda t: (t["audio"], t["chapters"], t["size"], t["dur"]), reverse=True)
t = near[0]
print("%d\t%s: %s %ds %d chapters %d audio %.1fGB (%d titles scanned)" % (
    t["id"], how, t["src"], t["dur"], t["chapters"], t["audio"], t["size"] / 1e9, len(ts)))
'

# Last useful makemkvcon message from a robot-mode log.
last_mkv_error() {
    grep -E '^MSG:(2|5)[0-9]{3},' "$1" 2>/dev/null \
        | grep -vE '^MSG:(5042|5075|5011|5036|5005|5014),' \
        | tail -n 1 | cut -d'"' -f2
}

is_unfinalized() {
    [[ "$(head -c 36 -- "$1" 2>/dev/null)" == "This file was not properly finalized" ]]
}

# Open the disc. MakeMKV reads Blu-ray ISOs directly; some ISOs are plain
# ISO9660 images with BDMV nested in a subfolder, or wrap a second image.
# Those get loop-mounted read-only. Prints the makemkvcon source spec.
open_source() {
    local iso="$1" work="$2" info="$3"
    if nice -n 19 ionice -c3 "$MKVCON" -r --noscan --minlength="$MIN_TITLE_SECONDS" info "iso:$iso" >"$info" 2>&1 \
        && grep -qE '^TCOUNT:[1-9]' "$info"; then
        echo "iso:$iso"; return 0
    fi
    mkdir -p "$work/mnt"
    mount -o loop,ro -- "$iso" "$work/mnt" 2>/dev/null || return 1
    local bdmv inner
    bdmv=$(find "$work/mnt" -maxdepth 4 -type d -name BDMV 2>/dev/null | head -n 1)
    if [[ -n "$bdmv" ]]; then
        echo "file:${bdmv%/BDMV}"; return 0
    fi
    inner=$(find "$work/mnt" -maxdepth 2 -type f -size +1G 2>/dev/null | head -n 1)
    if [[ -n "$inner" ]]; then
        echo "iso:$inner"; return 0
    fi
    return 1
}

# Wait until the 1-minute load average is at or below MAX_LOAD.
wait_for_load() {
    local waited=0 load
    while :; do
        load=$(cut -d' ' -f1 /proc/loadavg)
        awk -v l="$load" -v m="$MAX_LOAD" 'BEGIN { exit !(l <= m) }' && return 0
        [[ $waited -ge $LOAD_WAIT_MAX ]] && return 1
        [[ $waited -eq 0 ]] && log "  Load $load > $MAX_LOAD, waiting"
        sleep 60; waited=$((waited + 60))
    done
}

process_iso() {
    local iso="$1" mode="$2"
    local dir="${iso%/*}" base="${iso##*/}"
    local name="${base%.*}"
    local out="$dir/$name.mkv"
    local failed_marker="${iso}.failed"
    local lock_file
    lock_file="$RUN_DIR/locks/$(printf '%s' "$dir" | md5sum | cut -d' ' -f1).lock"

    (
        flock -n 9 || { log "Skipping $iso (directory locked)"; echo "SKIP_LOCKED" >>"$RESULTS"; exit 0; }

        if [[ "$mode" == "nightly" ]]; then
            if [[ -f "$out" ]] && ! is_unfinalized "$out"; then
                echo "SKIP_EXISTS" >>"$RESULTS"; exit 0
            fi
            if [[ -f "$failed_marker" && "$failed_marker" -nt "$iso" ]]; then
                log "Skipping $iso (prior failure; rm ${failed_marker} to retry)"
                echo "SKIP_FAILED" >>"$RESULTS"; exit 0
            fi
        fi

        log "========================================"
        log "ISO: $iso"
        if [[ ! -f "$iso" ]]; then
            log "  Not found, skipping"; echo "SKIP_MISSING" >>"$RESULTS"; exit 0
        fi
        local work="$WORK_ROOT/job.$$"
        local info="$RUN_TMP/info.$$" riplog="$RUN_TMP/rip.$$"
        local mounted=""
        cleanup_job() {
            if mountpoint -q "$work/mnt" 2>/dev/null; then
                umount "$work/mnt" || return 1   # never rm -rf across a mount
            fi
            rm -rf -- "$work" "$info" "$riplog"
        }
        trap cleanup_job EXIT
        trap 'exit 1' INT TERM

        fail() {
            log "  FAILED: $1"
            # A MakeMKV licensing/version refusal says nothing about the ISO;
            # do not mark it, so it is retried once MakeMKV is fixed.
            if ! grep -qs '^MSG:5021,' "$info" "$riplog"; then
                printf '%s %s\n' "$(date -Is)" "$1" >"$failed_marker"
            fi
            echo "FAIL" >>"$RESULTS"
            exit 0
        }

        if ! wait_for_load; then
            log "  Load above ${MAX_LOAD} for ${LOAD_WAIT_MAX}s, skipping this run"
            echo "SKIP_LOAD" >>"$RESULTS"; exit 0
        fi

        mkdir -p "$work" || fail "cannot create $work"

        local top runtime
        top="${dir#"$ROOT_DIR"/}"; top="${top%%/*}"
        runtime=$(awk -F'\t' -v k="$top" '$1 == k { print $2; exit }' "$RUNTIMES")
        runtime="${runtime:-0}"

        local src
        src=$(open_source "$iso" "$work" "$info") \
            || fail "makemkvcon could not open the image: $(last_mkv_error "$info")"
        [[ "$src" == iso:"$iso" ]] || mounted=1
        if [[ -n "$mounted" ]]; then
            log "  Opened via read-only loop mount: ${src#*:}"
            nice -n 19 ionice -c3 "$MKVCON" -r --noscan --minlength="$MIN_TITLE_SECONDS" info "$src" >"$info" 2>&1
        fi

        local pick tid
        pick=$(python3 -c "$PY_SELECT" "$runtime" <"$info")
        tid="${pick%%$'\t'*}"
        if [[ "$tid" == "FAIL" || -z "$tid" ]]; then
            fail "${pick#*$'\t'}; makemkvcon: $(last_mkv_error "$info")"
        fi
        log "  Title $tid (${pick#*$'\t'}); Radarr runtime ${runtime}m"

        # makemkvcon sometimes spins at 100% CPU after "Copy complete" and never
        # exits; stop it once the copy has been reported done for a while.
        local rc=0 mpid done_at=0
        timeout "$CONVERSION_TIMEOUT" nice -n 19 ionice -c3 \
            "$MKVCON" -r --noscan --minlength="$MIN_TITLE_SECONDS" mkv "$src" "$tid" "$work" \
            >"$riplog" 2>&1 &
        mpid=$!
        while kill -0 "$mpid" 2>/dev/null; do
            if grep -q '^MSG:5036,' "$riplog"; then
                [[ $done_at -eq 0 ]] && done_at=$SECONDS
                if (( SECONDS - done_at > 120 )); then
                    log "  makemkvcon still running 120s after copy complete, stopping it"
                    kill "$mpid" 2>/dev/null
                fi
            fi
            sleep 10
        done
        wait "$mpid" || rc=$?
        [[ $rc -eq 124 ]] && fail "makemkvcon timed out after ${CONVERSION_TIMEOUT}s"

        local ripped
        ripped=$(find "$work" -maxdepth 1 -type f -name '*.mkv' | head -n 1)
        [[ -n "$ripped" ]] || fail "no MKV produced (exit $rc): $(last_mkv_error "$riplog")"
        is_unfinalized "$ripped" && fail "MKV not finalized (exit $rc): $(last_mkv_error "$riplog")"

        # Verify: duration vs runtime, at least one video and one audio stream.
        local probe dur vcount acount need
        probe=$(ffprobe -v error -show_entries format=duration:stream=codec_type \
            -of default=nw=1 -- "$ripped" 2>/dev/null)
        dur=$(sed -n 's/^duration=\([0-9]*\).*/\1/p' <<<"$probe" | head -n 1)
        vcount=$(grep -c '^codec_type=video' <<<"$probe")
        acount=$(grep -c '^codec_type=audio' <<<"$probe")
        if [[ "$runtime" -gt 0 ]]; then
            need=$(python3 -c "import sys; print(int(int(sys.argv[1]) * 60 * float(sys.argv[2])))" "$runtime" "$MIN_RUNTIME_RATIO")
        else
            need="$FALLBACK_MIN_SECONDS"
        fi
        [[ -n "$dur" ]] || fail "ffprobe could not read the ripped MKV"
        [[ "$vcount" -ge 1 && "$acount" -ge 1 ]] || fail "ripped MKV has $vcount video / $acount audio streams"
        [[ "$dur" -ge "$need" ]] || fail "ripped duration ${dur}s < required ${need}s (Radarr runtime ${runtime}m)"

        # Replace an existing MKV (list mode, or an unfinalized leftover):
        # the old file goes to quarantine, never deleted.
        if [[ -e "$out" ]]; then
            local qdir="$QUARANTINE_DIR/${dir#"$ROOT_DIR"/}"
            local qdest="$qdir/${out##*/}"
            [[ -e "$qdest" ]] && qdest="$qdest.$(date +%Y%m%d%H%M%S)"
            mkdir -p -- "$qdir" && mv -n -- "$out" "$qdest" && [[ ! -e "$out" ]] \
                || fail "could not quarantine existing $out"
            log "  Quarantined previous MKV to $qdir/"
        fi
        # Match the ISO's owner (the library is konoahko-owned) before it goes live.
        chown --reference="$iso" -- "$ripped" 2>/dev/null
        chmod 644 -- "$ripped"
        mv -T -n -- "$ripped" "$out" && [[ ! -e "$ripped" ]] || fail "rename into $out failed"
        rm -f -- "$failed_marker"
        log "  OK: $out (${dur}s, ${vcount}v/${acount}a, need ${need}s)"
        echo "OK" >>"$RESULTS"
        plex_scan_dir "$dir"

        if [[ "$DELETE_ISO" == "true" ]]; then
            rm -f -- "$iso"
        fi
    ) 9>"$lock_file"
}

export -f process_iso wait_for_load open_source last_mkv_error is_unfinalized log ts plex_scan_dir
export MKVCON CONVERSION_TIMEOUT MIN_TITLE_SECONDS MIN_RUNTIME_RATIO FALLBACK_MIN_SECONDS \
    ROOT_DIR QUARANTINE_DIR WORK_ROOT DELETE_ISO RUN_DIR RUN_TMP RESULTS RUNTIMES PY_SELECT \
    PLEX_HOST_ROOT PLEX_CONTAINER_ROOT MAX_LOAD LOAD_WAIT_MAX

# Locks used to sit next to the media as .makemkv_convert.lock; they now live
# in $RUN_DIR. Clear the empty leftovers.
find "$ROOT_DIR" -maxdepth 3 -type f -name .makemkv_convert.lock -size 0 -delete 2>/dev/null

load_radarr_runtimes
load_plex

MODE="nightly"
LIST_FILE="$RUN_TMP/isos"
if [[ -n "$ISO_LIST" || $# -gt 0 ]]; then
    MODE="list"
    { [[ -n "$ISO_LIST" ]] && grep -v '^[[:space:]]*$' "$ISO_LIST"; printf '%s\n' "$@"; } \
        | grep -v '^$' | tr '\n' '\0' >"$LIST_FILE"
else
    find "$ROOT_DIR" -type f -iname "*.iso" -print0 >"$LIST_FILE"
fi

echo "Mode: $MODE; root: $ROOT_DIR; parallel jobs: $MAX_PARALLEL_JOBS"
xargs -0 -P "$MAX_PARALLEL_JOBS" -I {} bash -c 'process_iso "$1" "$2"' _ {} "$MODE" <"$LIST_FILE"

count() { grep -cx "$1" "$RESULTS"; }
echo "iso-convert summary: mode=$MODE isos=$(tr -cd '\0' <"$LIST_FILE" | wc -c)" \
    "converted=$(count OK) failed=$(count FAIL) skipped_existing=$(count SKIP_EXISTS)" \
    "skipped_failed=$(count SKIP_FAILED) skipped_locked=$(count SKIP_LOCKED) missing=$(count SKIP_MISSING) skipped_load=$(count SKIP_LOAD)" \
    "elapsed=$(( $(date +%s) - START_TS ))s"
