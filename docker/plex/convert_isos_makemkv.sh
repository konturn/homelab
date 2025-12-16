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

# Number of parallel jobs (set to number of CPU cores or desired parallelism)
# Use $(nproc) to auto-detect CPU cores, or set a specific number
MAX_PARALLEL_JOBS=1

# Timeout per conversion in seconds (1 day = 86400 seconds)
CONVERSION_TIMEOUT=86400

########################################

# Check MakeMKV exists
if ! command -v "$MKVCON" >/dev/null 2>&1; then
    echo "ERROR: makemkvcon not found in PATH."
    echo "Install MakeMKV or add it to PATH."
    exit 1
fi

# Function to process a single ISO file
process_iso() {
    local iso="$1"
    local dir="${iso%/*}"       # directory path
    local base="${iso##*/}"     # filename + extension
    local name="${base%.*}"     # filename without extension
    local out="$dir/$name.$OUTPUT_EXT"

    # Use a lock file per directory to avoid conflicts when processing multiple ISOs in same dir
    local lock_file="$dir/.makemkv_convert.lock"
    
    (
        flock -n 9 || {
            echo "[$(date +%H:%M:%S)] [PID:$$] Skipping $iso (directory locked)"
            return 0
        }
        
        echo "[$(date +%H:%M:%S)] [PID:$$] ========================================"
        echo "[$(date +%H:%M:%S)] [PID:$$] Found ISO: $iso"

        # Skip if target MKV already exists
        if [[ -f "$out" ]]; then
            echo "[$(date +%H:%M:%S)] [PID:$$]   $out already exists → skipping."
            return 0
        fi

        echo "[$(date +%H:%M:%S)] [PID:$$]   Running MakeMKV on title 0…"

        # Rip title #0 from the ISO into the same directory
        # --noscan avoids a second full scan since MakeMKV can use cached info
        # --progress=-same makes it non-interactive and prevents hanging
        # --minlength=120 filters out short titles (trailers, etc.)
        # timeout wraps the command to kill it after CONVERSION_TIMEOUT seconds
        local conversion_success=false
        if timeout "$CONVERSION_TIMEOUT" "$MKVCON" mkv "iso:$iso" 0 "$dir" --noscan --minlength=120 --progress=-same 2>&1; then
            conversion_success=true
        else
            local exit_code=$?
            if [[ $exit_code -eq 124 ]]; then
                echo "[$(date +%H:%M:%S)] [PID:$$]   TIMEOUT: makemkvcon exceeded ${CONVERSION_TIMEOUT}s timeout on:"
                echo "[$(date +%H:%M:%S)] [PID:$$]     $iso"
                echo "[$(date +%H:%M:%S)] [PID:$$]   Continuing with next ISO..."
            else
                echo "[$(date +%H:%M:%S)] [PID:$$]   ERROR: makemkvcon failed (exit code: $exit_code) on:"
                echo "[$(date +%H:%M:%S)] [PID:$$]     $iso"
            fi
        fi

        # Find newest MKV created in this dir and rename it to match the ISO basename
        new_mkv=$(ls -1t "$dir"/*.mkv 2>/dev/null | head -n 1 || true)

        if [[ -z "$new_mkv" ]]; then
            if [[ "$conversion_success" == "false" ]]; then
                echo "[$(date +%H:%M:%S)] [PID:$$]   No MKV file created (conversion failed or timed out)."
            else
                echo "[$(date +%H:%M:%S)] [PID:$$]   ERROR: No MKV file appeared in $dir after ripping."
            fi
            return 0  # Continue processing other ISOs
        fi

        # Only rename if it's not already at the desired path
        if [[ "$new_mkv" != "$out" ]]; then
            echo "[$(date +%H:%M:%S)] [PID:$$]   Renaming $new_mkv → $out"
            mv -n -- "$new_mkv" "$out"
        else
            echo "[$(date +%H:%M:%S)] [PID:$$]   Output file is $out"
        fi

        if [[ "$DELETE_ISO" == "true" ]]; then
            echo "[$(date +%H:%M:%S)] [PID:$$]   Deleting ISO…"
            rm -f -- "$iso"
        fi

        echo "[$(date +%H:%M:%S)] [PID:$$]   Completed: $iso"
    ) 9>"$lock_file"
}

export -f process_iso
export MKVCON OUTPUT_EXT DELETE_ISO CONVERSION_TIMEOUT

echo "Scanning for ISO files in: $ROOT_DIR"
echo "Using $MAX_PARALLEL_JOBS parallel jobs"
echo

# Find all .iso files and process them in parallel
find "$ROOT_DIR" -type f -iname "*.iso" -print0 | \
    xargs -0 -P "$MAX_PARALLEL_JOBS" -I {} bash -c 'process_iso "$@"' _ {}

echo
echo "[$(date +%H:%M:%S)] Done."

