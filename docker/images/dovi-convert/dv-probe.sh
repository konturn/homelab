#!/usr/bin/env bash
# dv-probe.sh <file.mkv>
#
# Prints one JSON object describing a file's Dolby Vision layout and tracks:
#   - mediainfo's HDR format line (cross-check of what Plex reports);
#   - dovi_tool's profile and FEL/MEL verdict, from RPUs sampled at three
#     points in the film (5%, 35%, 65%), so a dark or black opening does not
#     decide the verdict on its own. Any FEL sample makes the file FEL;
#   - audio and subtitle tracks, chapters and attachments (mkvmerge -J).
# Reads a few hundred MB per file; never writes next to the input.
set -euo pipefail

f="${1:?usage: dv-probe.sh <file.mkv>}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkvmerge -J "$f" >"$tmp/mkv.json"
dur_ns=$(jq -r '.container.properties.duration // 0' "$tmp/mkv.json")
dur_s=$(( dur_ns / 1000000000 ))

samples=()
for pct in 5 35 65; do
  ss=$(( dur_s * pct / 100 ))
  rpu="$tmp/rpu_$pct.bin"
  if ffmpeg -nostdin -v error -ss "$ss" -i "$f" -map 0:v:0 -c:v copy \
       -bsf:v hevc_mp4toannexb -frames:v 48 -f hevc - 2>"$tmp/ff_$pct.err" \
     | dovi_tool extract-rpu - -o "$rpu" >/dev/null 2>"$tmp/dt_$pct.err" \
     && [ -s "$rpu" ]; then
    # e.g. "Profile: 7 (FEL)", "Profile: 7 (MEL)", "Profile: 8"
    line=$(dovi_tool info -i "$rpu" --summary 2>/dev/null | grep -m1 -E '^\s*Profile:' | sed -E 's/^\s*Profile:\s*//')
    samples+=("$(jq -cn --argjson pct "$pct" --arg p "$line" '{pct:$pct, profile:$p}')")
  else
    samples+=("$(jq -cn --argjson pct "$pct" --arg e "$(head -c 300 "$tmp/dt_$pct.err")" '{pct:$pct, error:$e}')")
  fi
done

# mediainfo's JSON output is not always valid JSON (unescaped tag values), so
# ask for just the HDR fields through a template and build the JSON here.
mediainfo --Inform='Video;%HDR_Format%|%HDR_Format_Profile%|%HDR_Format_Settings%|%HDR_Format_Compatibility%\n' "$f" 2>/dev/null \
  | jq -R 'select(length > 0) | split("|") | {HDR_Format: .[0], HDR_Format_Profile: .[1], HDR_Format_Settings: .[2], HDR_Format_Compatibility: .[3]}' \
  | jq -s . >"$tmp/mi.json" || echo '[]' >"$tmp/mi.json"

jq -n \
  --arg file "$f" \
  --slurpfile mkv "$tmp/mkv.json" \
  --slurpfile mi "$tmp/mi.json" \
  --argjson samples "$(printf '%s\n' "${samples[@]}" | jq -s .)" '
  ($mkv[0]) as $m |
  {
    file: $file,
    duration_s: (($m.container.properties.duration // 0) / 1e9),
    mediainfo_hdr: $mi[0],
    rpu_samples: $samples,
    dv_profile: ([ $samples[] | .profile // empty | capture("^(?<p>[0-9]+)").p | tonumber ] | max),
    el_type: (
      [ $samples[] | .profile // empty ] as $p
      | if ($p | any(test("FEL"))) then "FEL"
        elif ($p | any(test("MEL"))) then "MEL"
        else null end),
    video_tracks: [ $m.tracks[] | select(.type == "video")
      | {id, codec, dims: .properties.pixel_dimensions, name: .properties.track_name} ],
    audio_tracks: [ $m.tracks[] | select(.type == "audio")
      | {id, codec, language: (.properties.language_ietf // .properties.language),
         name: .properties.track_name, channels: .properties.audio_channels,
         default: .properties.default_track, forced: .properties.forced_track} ],
    subtitle_tracks: [ $m.tracks[] | select(.type == "subtitles")
      | {id, codec, language: (.properties.language_ietf // .properties.language),
         name: .properties.track_name,
         default: .properties.default_track, forced: .properties.forced_track} ],
    chapters: ([ ($m.chapters // [])[] | .num_entries ] | add // 0),
    attachments: ($m.attachments // [] | length)
  }'
