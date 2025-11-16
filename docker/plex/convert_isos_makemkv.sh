#!/usr/bin/env bash
set -euo pipefail

########################################
# CONFIG
########################################

# Root of your Plex movies directory
ROOT_DIR="/mpool/plex/Movies"

# Output file extension
OUTPUT_EXT="mkv"

# Delete ISO after successful rip? (true/false)
DELETE_ISO="false"

# MakeMKV binary
MKVCON="makemkvcon"

########################################

# Check MakeMKV exists
if ! command -v "$MKVCON" >/dev/null 2>&1; then
    echo "ERROR: makemkvcon not found in PATH."
    echo "Install MakeMKV or add it to PATH."
    exit 1
fi

echo "Scanning for ISO files in: $ROOT_DIR"
echo

# Find all .iso files
find "$ROOT_DIR" -type f -iname "*.iso" -print0 |
while IFS= read -r -d '' iso; do
    dir="${iso%/*}"       # directory path
    base="${iso##*/}"     # filename + extension
    name="${base%.*}"     # filename without extension

    out="$dir/$name.$OUTPUT_EXT"

    echo "========================================"
    echo "Found ISO: $iso"

    # Skip if target MKV already exists
    if [[ -f "$out" ]]; then
        echo "  $out already exists → skipping."
        echo
        continue
    fi

    echo "  Running MakeMKV on title 0…"

    # Rip title #0 from the ISO into the same directory
    # --noscan avoids a second full scan since MakeMKV can use cached info
    if ! "$MKVCON" mkv "iso:$iso" 0 "$dir" --noscan --minlength=120 2>&1; then
        echo "  ERROR: makemkvcon failed on:"
        echo "    $iso"
        echo
        continue
    fi

    # Find newest MKV created in this dir and rename it to match the ISO basename
    new_mkv=$(ls -1t "$dir"/*.mkv 2>/dev/null | head -n 1 || true)

    if [[ -z "$new_mkv" ]]; then
        echo "  ERROR: No MKV file appeared in $dir after ripping."
        echo
        continue
    fi

    # Only rename if it's not already at the desired path
    if [[ "$new_mkv" != "$out" ]]; then
        echo "  Renaming $new_mkv → $out"
        mv -n -- "$new_mkv" "$out"
    else
        echo "  Output file is $out"
    fi

    if [[ "$DELETE_ISO" == "true" ]]; then
        echo "  Deleting ISO…"
        rm -f -- "$iso"
    fi

    echo
done

echo "Done."

