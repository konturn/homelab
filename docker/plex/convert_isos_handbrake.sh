#!/usr/bin/env bash
set -euo pipefail

########################################
# CONFIG
########################################

# Root of your Plex movies directory
ROOT_DIR="/mpool/plex/Movies"

# Output container
OUTPUT_EXT="mkv"

# Delete ISO after successful encode? (true/false)
DELETE_ISO="false"

# Video quality (lower = better; 16–18 is very good, 14–16 is overkill territory)
HB_QUALITY="16"

# x264 encoder preset: slower = better quality & more CPU
HB_ENCODER_PRESET="veryslow"

########################################

if ! command -v HandBrakeCLI >/dev/null 2>&1; then
  echo "ERROR: HandBrakeCLI not found in PATH. Install handbrake-cli first."
  exit 1
fi

echo "Scanning for ISO files under: $ROOT_DIR"
echo "Using HandBrakeCLI with:"
echo "  RF (quality):   $HB_QUALITY"
echo "  Preset speed:   $HB_ENCODER_PRESET"
echo "  Output format:  .$OUTPUT_EXT"
echo

# Find all ISO files
find "$ROOT_DIR" -type f -iname '*.iso' -print0 | while IFS= read -r -d '' iso; do
  # No $(...) to avoid any paren weirdness; use pure parameter expansion
  dir="${iso%/*}"         # everything before last /
  base="${iso##*/}"       # everything after last /
  name="${base%.*}"       # strip extension
  out="$dir/$name.$OUTPUT_EXT"

  echo "========================================"
  echo "Found ISO:      $iso"
  echo "Output target:  $out"

  if [ -f "$out" ]; then
    echo "  Skipping: output already exists."
    echo
    continue
  fi

  echo "  Starting high-quality encode ..."

  # One long line to avoid line-continuation issues
  HandBrakeCLI -i "$iso" -o "$out" --main-feature -e x264 -q "$HB_QUALITY" --encoder-preset "$HB_ENCODER_PRESET" --encoder-profile high --encoder-level 4.1 --all-audio --audio-copy-mask ac3,dts,dtshd,truehd,eac3 --audio-fallback ac3 --all-subtitles --subtitle-default=1 --subtitle-burned=none --optimize --verbose 1

  echo "  Encode complete."

  if [ "$DELETE_ISO" = "true" ]; then
    echo "  Deleting original ISO..."
    rm -f -- "$iso"
  fi

  echo
done

echo "All ISOs processed."

