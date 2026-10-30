#!/usr/bin/env bash
# dv-p7-to-p8.sh <input.mkv> <output.mkv> <scratch-dir> [report.json]
#
# Converts one Dolby Vision Profile 7 MKV to Profile 8.1 and validates it.
# Never modifies the input; the caller decides what to do with the output.
#
#   1. extract the HEVC stream and its timestamps (mkvextract, one read)
#   2. dovi_tool -m 2 convert --discard  (P7 -> P8.1; drops the FEL/MEL layer)
#   3. remux with mkvmerge: new video + every other track, chapters, tags,
#      attachments and title from the input; video track name, language,
#      flags and exact frame rate carried over
#   4. validate: duration, frame count (input packets = RPU count = output
#      packets), every frame timestamp within 1 ms, header frame rate, DV profile 8 in the RPUs and in the container's DOVI
#      config record, identical non-video tracks/chapters/attachments, and
#      ffprobe decodes start, middle and end without errors
#
# Intermediates go to <scratch-dir>. Exits non-zero (and removes the output)
# on any failure. All heavy steps run with nice 19 / ionice idle.
set -euo pipefail

in="${1:?usage: dv-p7-to-p8.sh <input.mkv> <output.mkv> <scratch-dir> [report.json]}"
out="${2:?output path required}"
scratch="${3:?scratch dir required}"
report="${4:-}"

LOW=(nice -n 19 ionice -c 3)
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
fail() { log "FAIL: $*"; exit 1; }

[ -f "$in" ] || fail "input not found"
[ ! -e "$out" ] || fail "output already exists"
[ -d "$scratch" ] || fail "scratch dir not found"

work=$(mktemp -d "$scratch/dvconv.XXXXXX")
ok=0
cleanup() {
  rm -rf "$work"
  [ "$ok" = 1 ] || rm -f "$out"
}
trap cleanup EXIT

t0=$(date +%s)
mkvmerge -J "$in" >"$work/in.json"
nvid=$(jq '[.tracks[] | select(.type == "video")] | length' "$work/in.json")
[ "$nvid" = 1 ] || fail "expected exactly 1 video track, found $nvid"
vid=$(jq '.tracks[] | select(.type == "video") | .id' "$work/in.json")
jq -e '.tracks[] | select(.type == "video") | .codec | test("HEVC|H.265")' "$work/in.json" >/dev/null \
  || fail "video track is not HEVC"
jq -c '.tracks[] | select(.type == "video") | .properties' "$work/in.json" >"$work/vprops.json"

# --- 1. extract -------------------------------------------------------------
log "extract: video track $vid + timestamps"
"${LOW[@]}" mkvextract "$in" \
  tracks "$vid:$work/in.hevc" \
  timestamps_v2 "$vid:$work/ts.txt" >"$work/mkvextract.log"
# Authoritative input frame count: video packets in the source MKV (one
# access unit per packet; BL+EL+RPU share it). timestamps_v2 holds one more
# line than there are frames: the end timestamp of the last frame.
"${LOW[@]}" ffprobe -v error -select_streams v:0 -show_entries packet=pts_time \
  -of csv=p=0 "$in" | grep -oE '^-?[0-9.]+' | sort -n >"$work/pts_in.txt"
frames_in=$(wc -l <"$work/pts_in.txt")
ts_lines=$(grep -vc '^#' "$work/ts.txt")
[ "$ts_lines" = "$frames_in" ] || [ "$ts_lines" = $(( frames_in + 1 )) ] \
  || fail "timestamps ($ts_lines) do not match input frames ($frames_in)"
log "extract: $frames_in frames ($ts_lines timestamps)"

# --- 2. convert -------------------------------------------------------------
log "convert: dovi_tool -m 2 convert --discard"
"${LOW[@]}" dovi_tool -m 2 convert --discard -i "$work/in.hevc" -o "$work/p8.hevc" >"$work/convert.log"
rm -f "$work/in.hevc"
"${LOW[@]}" dovi_tool extract-rpu -i "$work/p8.hevc" -o "$work/p8.rpu" >/dev/null
dovi_tool info -i "$work/p8.rpu" --summary >"$work/p8.rpu.txt"
rpu_profile=$(grep -m1 -E '^\s*Profile:' "$work/p8.rpu.txt" | sed -E 's/^\s*Profile:\s*//')
rpu_frames=$(grep -m1 -E '^\s*Frames:' "$work/p8.rpu.txt" | grep -oE '[0-9]+' | head -1)
log "convert: RPU profile '$rpu_profile', $rpu_frames frames"
[[ "$rpu_profile" == 8* ]] || fail "converted RPU profile is '$rpu_profile', not 8"
[ "$rpu_frames" = "$frames_in" ] || fail "RPU frames $rpu_frames != input frames $frames_in"

# --- 3. remux ---------------------------------------------------------------
vp() { jq -r "$1" "$work/vprops.json"; }
yn() { [ "$1" = true ] && echo yes || echo no; }
vargs=()
vargs+=(--language "0:$(vp '.language_ietf // .language // "und"')")
# (not jq's //, which treats an explicit false as missing)
vargs+=(--default-track-flag "0:$(yn "$(vp 'if .default_track == null then true else .default_track end')")")
vargs+=(--forced-display-flag "0:$(yn "$(vp 'if .forced_track == null then false else .forced_track end')")")
vargs+=(--track-enabled-flag "0:$(yn "$(vp 'if .enabled_track == null then true else .enabled_track end')")")
name=$(vp '.track_name // empty')
[ -n "$name" ] && vargs+=(--track-name "0:$name")
# Keep the header frame rate exact (e.g. 24000/1001). Derived from the
# millisecond timestamps alone, mkvmerge would round it (23.976 -> 23.810),
# which breaks frame-rate matching on players.
rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
  -of default=nw=1:nk=1 "$in" | grep -m1 -E '^[0-9]+/[0-9]+$' || true)
# Constant frame rate (every frame interval within 1 ms of 1/rate): mux with
# that exact default duration. Otherwise reapply the source timestamps; the
# header rate is then derived and the default_duration check flags the file
# for a manual look.
cfr=false
if [ -n "$rate" ]; then
  cfr=$(awk -v r="$rate" 'BEGIN { split(r, a, "/"); step = 1000 * a[2] / a[1]; ok = 1 }
    /^#/ { next }
    { if (n++ && ($1 - prev - step > 1 || prev - $1 + step > 1)) ok = 0; prev = $1 }
    END { print (ok && n > 1) ? "true" : "false" }' "$work/ts.txt")
fi
if [ "$cfr" = true ]; then
  vargs+=(--default-duration "0:${rate}p")
else
  log "remux: source is not constant ${rate:-?} fps; reapplying source timestamps"
  vargs+=(--timestamps "0:$work/ts.txt")
fi
pix=$(vp '.pixel_dimensions // empty'); disp=$(vp '.display_dimensions // empty')
[ -n "$disp" ] && [ "$disp" != "$pix" ] && vargs+=(--display-dimensions "0:$disp")
targs=()
title=$(jq -r '.container.properties.title // empty' "$work/in.json")
[ -n "$title" ] && targs=(--title "$title")

log "remux: mkvmerge"
rc=0
"${LOW[@]}" mkvmerge -q -o "$out" "${targs[@]}" \
  "${vargs[@]}" "$work/p8.hevc" \
  --no-video "$in" >"$work/mkvmerge.log" 2>&1 || rc=$?
# mkvmerge: 0 = ok, 1 = warnings (output written), 2 = error
[ "$rc" -le 1 ] || { cat "$work/mkvmerge.log" >&2; fail "mkvmerge exit $rc"; }
[ "$rc" = 0 ] || log "remux: mkvmerge warnings: $(tr '\n' ' ' <"$work/mkvmerge.log" | head -c 500)"
rm -f "$work/p8.hevc"

# --- 4. validate ------------------------------------------------------------
log "validate"
mkvmerge -J "$out" >"$work/out.json"
checks=()
check() { # name pass(true/false) detail
  checks+=("$(jq -cn --arg n "$1" --argjson p "$2" --arg d "$3" '{check:$n, pass:$p, detail:$d}')")
  [ "$2" = true ] && log "  ok   $1: $3" || log "  FAIL $1: $3"
}

# duration: within two frame durations (or 100 ms when unknown)
dur_in=$(jq '.container.properties.duration' "$work/in.json")
dur_out=$(jq '.container.properties.duration' "$work/out.json")
fdur=$(vp '.default_duration // 50000000')
diff=$(( dur_in > dur_out ? dur_in - dur_out : dur_out - dur_in ))
check duration "$([ "$diff" -le $(( 2 * fdur )) ] && echo true || echo false)" \
  "in ${dur_in}ns out ${dur_out}ns diff ${diff}ns"

# frame count and timestamps: full read of the output video packets; every
# output frame must sit within 1 ms of the matching input frame
"${LOW[@]}" ffprobe -v error -select_streams v:0 -show_entries packet=pts_time \
  -of csv=p=0 "$out" | grep -oE '^-?[0-9.]+' | sort -n >"$work/pts_out.txt"
frames_out=$(wc -l <"$work/pts_out.txt")
check frame_count "$([ "$frames_out" = "$frames_in" ] && echo true || echo false)" \
  "in $frames_in out $frames_out rpu $rpu_frames"
maxdiff=$(paste "$work/pts_in.txt" "$work/pts_out.txt" \
  | awk '{ d = $1 - $2; if (d < 0) d = -d; if (d > m) m = d } END { printf "%.6f", m + 0 }')
check timestamps "$(awk -v m="$maxdiff" 'BEGIN { print (m <= 0.0011) ? "true" : "false" }')" \
  "max per-frame difference ${maxdiff}s (cfr=$cfr, rate ${rate:-?})"

# DV profile: RPUs read back from the output + the container config record
"${LOW[@]}" ffmpeg -nostdin -v error -i "$out" -map 0:v:0 -c:v copy -bsf:v hevc_mp4toannexb \
  -frames:v 240 -f hevc - | dovi_tool extract-rpu - -o "$work/out.rpu" >/dev/null
out_rpu_profile=$(dovi_tool info -i "$work/out.rpu" --summary | grep -m1 -E '^\s*Profile:' | sed -E 's/^\s*Profile:\s*//')
check rpu_profile "$([[ "$out_rpu_profile" == 8* ]] && echo true || echo false)" "output RPU profile '$out_rpu_profile'"
cfg=$(ffprobe -v error -select_streams v:0 -show_streams -of json "$out" \
  | jq -c '[.streams[0].side_data_list[]? | select(.side_data_type | test("DOVI"))][0]
           | {dv_profile, dv_level, rpu_present_flag, el_present_flag, bl_present_flag, dv_bl_signal_compatibility_id}')
check dovi_config "$(jq '.dv_profile == 8 and .dv_bl_signal_compatibility_id == 1 and .el_present_flag == 0' <<<"$cfg")" "$cfg"

# tracks: everything except the video track must be identical
proj='{
  other: [ .tracks[] | select(.type != "video") | {type, codec,
    p: (.properties | {codec_id, codec_private_length, language, language_ietf, track_name,
        default_track, forced_track, enabled_track, flag_hearing_impaired,
        flag_visual_impaired, flag_text_descriptions, flag_original, flag_commentary,
        audio_channels, audio_sampling_frequency, audio_bits_per_sample})} ],
  video: [ .tracks[] | select(.type == "video") | {codec,
    p: (.properties | {pixel_dimensions, display_dimensions, language_ietf, track_name,
        default_track, forced_track, enabled_track, default_duration})} ],
  attachments: [ (.attachments // [])[] | {file_name, content_type, size, description} ],
  chapters: (.chapters // []),
  title: .container.properties.title
}'
jq -S "$proj" "$work/in.json" >"$work/in.proj.json"
jq -S "$proj" "$work/out.json" >"$work/out.proj.json"
if diff -u "$work/in.proj.json" "$work/out.proj.json" >"$work/proj.diff"; then
  check tracks true "$(jq -r '"\(.other|length) audio/subtitle tracks, \(.attachments|length) attachments, chapters \(.chapters|tostring), video props identical"' "$work/in.proj.json")"
else
  check tracks false "$(head -c 1500 "$work/proj.diff")"
fi

# decode: 5 s at the start, middle and end
dur_s=$(( dur_out / 1000000000 ))
for at in 0 $(( dur_s / 2 )) $(( dur_s > 15 ? dur_s - 15 : 0 )); do
  n=$("${LOW[@]}" ffprobe -v error -threads 4 -select_streams v:0 -read_intervals "${at}%+5" \
        -show_entries frame=pts_time -of csv=p=0 "$out" 2>"$work/dec.err" | wc -l) || true
  errs=$(head -c 500 "$work/dec.err")
  check "decode@${at}s" "$([ "$n" -gt 0 ] && [ -z "$errs" ] && echo true || echo false)" "$n frames decoded${errs:+, errors: $errs}"
done

all_ok=$(printf '%s\n' "${checks[@]}" | jq -s 'all(.pass)')
secs=$(( $(date +%s) - t0 ))
result=$(jq -n --arg in "$in" --arg out "$out" --argjson ok "$all_ok" --argjson secs "$secs" \
  --argjson in_size "$(stat -c %s "$in")" --argjson out_size "$(stat -c %s "$out")" \
  --arg rpu "$rpu_profile" --argjson mkvmerge_rc "$rc" \
  --argjson checks "$(printf '%s\n' "${checks[@]}" | jq -s .)" \
  '{input:$in, output:$out, ok:$ok, seconds:$secs, input_size:$in_size, output_size:$out_size,
    converted_rpu_profile:$rpu, mkvmerge_rc:$mkvmerge_rc, checks:$checks}')
[ -n "$report" ] && printf '%s\n' "$result" >"$report"
printf '%s\n' "$result"
[ "$all_ok" = true ] || fail "validation failed"
ok=1
log "done in ${secs}s"
