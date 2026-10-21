#!/bin/sh
# Scan to the paperless consume folder (NFS from the router at /mnt).
# Refuse to scan unless /mnt is the NFS mount: a missing mount used to leave
# scans on the Pi's SD card where paperless never saw them.
set -eu

# /mnt is an x-systemd.automount: list it to trigger the mount. While idle it
# is an autofs mount point, so `mountpoint -q` alone would pass; check the
# filesystem type instead.
timeout 30 ls /mnt/ >/dev/null 2>&1 || true
if ! findmnt -n -t nfs,nfs4 -M /mnt >/dev/null; then
  echo "scan.sh: /mnt is not the paperless NFS mount; refusing to scan" >&2
  logger -t scan.sh -p user.err "/mnt is not the paperless NFS mount; scan refused"
  exit 1
fi

now=$(date +"%Y-%m-%d-%H%M%S")
out="/mnt/scan-${now}.pdf"
/root/sane-scan-pdf/scan --device 'fujitsu:ScanSnap iX1600:144412' -r 600 -m Lineart -o "$out"

# sane-scan-pdf exits 0 with "Found no scans." when the scanner is missing or
# the feeder is empty; report that as a failure too.
if [ ! -s "$out" ]; then
  echo "scan.sh: no pages scanned (scanner not connected, or feeder empty)" >&2
  logger -t scan.sh -p user.err "no pages scanned"
  exit 2
fi
echo "scan.sh: wrote $(basename "$out")"
