#!/bin/bash
# One-time Home Connect login for hcpy (docs/HOMEASSISTANT.md, "Thermador").
#
#   ssh -t root@10.4.0.1 hc-login
#
# Runs hc-login.py from the hcpy image with the router's own network, so the
# login and key download go out the WAN (the bridge itself has no internet).
# It prints a Home Connect URL: open it in a desktop browser with DevTools ->
# Network open, log in there, and paste back the hcauth://auth/prod?... link
# the browser refuses to open. The password is typed only into the Home
# Connect page, never into this host.
#
# The result (per appliance: key, iv and the feature description) is written
# 0400 to $HCPY_DIR/devices.json, owned by the bridge's user, with each
# appliance's address set to its fixed DHCP address. Then the bridge restarts.
# Running it again replaces the file (needed after an appliance is re-paired).
set -euo pipefail
# shellcheck source=/dev/null
. /usr/local/etc/hc-login.conf  # HCPY_IMAGE HCPY_DIR HCPY_UID HOOD_ADDR RANGE_ADDR

[ "$(id -u)" = 0 ] || { echo "hc-login: run as root" >&2; exit 1; }
[ -t 0 ] || { echo "hc-login: needs a terminal (use ssh -t)" >&2; exit 1; }
docker image inspect "$HCPY_IMAGE" >/dev/null 2>&1 \
  || { echo "hc-login: image $HCPY_IMAGE is missing; run the deploy" >&2; exit 1; }

umask 077
work=$(mktemp -d /dev/shm/hc-login.XXXXXX)  # RAM only
trap 'rm -rf "$work"' EXIT
chown "$HCPY_UID:$HCPY_UID" "$work"

docker run --rm -it --network host --user "$HCPY_UID:$HCPY_UID" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -v "$work:/out" -w /app "$HCPY_IMAGE" python3 hc-login.py /out/devices.json

[ -s "$work/devices.json" ] || { echo "hc-login: no devices file was written" >&2; exit 1; }

echo
echo "Appliances found:"
python3 - "$work/devices.json" "$HOOD_ADDR" "$RANGE_ADDR" <<'PY'
import json
import sys

path, hood, rng = sys.argv[1:]
with open(path) as f:
    devices = json.load(f)
for d in devices:
    # The hood has its own address; everything else on the account is the range.
    d["host"] = hood if d["name"].startswith("hood") else rng
    print(f"  {d['name']:<12} -> {d['host']}  ({len(d.get('features', {}))} features)")
with open(path, "w") as f:
    json.dump(devices, f, indent=2)
PY

install -d -m 0700 -o "$HCPY_UID" -g "$HCPY_UID" "$HCPY_DIR"
install -m 0400 -o "$HCPY_UID" -g "$HCPY_UID" "$work/devices.json" "$HCPY_DIR/devices.json"
docker restart hcpy >/dev/null
echo
echo "hc-login: saved. The bridge (hcpy) was restarted; you can close this window."
