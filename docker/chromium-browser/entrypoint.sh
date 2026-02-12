#!/bin/bash
set -euo pipefail

cleanup() {
    echo "Shutting down..."
    kill $(jobs -p) 2>/dev/null || true
    wait
    exit 0
}
trap cleanup SIGTERM SIGINT SIGQUIT

# Fix profile directory permissions (volume mount creates as root)
if [ -d /data/chrome-profile ]; then
    chown -R chrome:chrome /data/chrome-profile
fi

# Clean up stale lock files from previous crashes
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start Xvfb
echo "Starting Xvfb on display ${DISPLAY}..."
Xvfb "${DISPLAY}" -screen 0 "${SCREEN_WIDTH}x${SCREEN_HEIGHT}x${SCREEN_DEPTH}" \
    -ac +extension GLX +render -noreset &
XVFB_PID=$!

# Wait for Xvfb to be ready
for i in $(seq 1 30); do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        echo "Xvfb is ready."
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Xvfb failed to start" >&2
        exit 1
    fi
    sleep 0.2
done

# Start dbus session
eval "$(dbus-launch --sh-syntax)" 2>/dev/null || true

# Start x11vnc (with password if set)
echo "Starting x11vnc..."
VNC_ARGS="-display ${DISPLAY} -forever -shared -rfbport 5900 -q"
if [ -n "${VNC_PASSWORD:-}" ]; then
    x11vnc $VNC_ARGS -passwd "${VNC_PASSWORD}" &
else
    echo "WARNING: No VNC_PASSWORD set — VNC is unprotected!"
    x11vnc $VNC_ARGS -nopw &
fi

# Start noVNC via websockify
echo "Starting noVNC on port 6080..."
websockify --web /usr/share/novnc 6080 localhost:5900 &

# Start socat proxy: expose CDP on 0.0.0.0:9222, forwarding to chromium on 127.0.0.1:9223
# Chromium ignores --remote-debugging-address=0.0.0.0 and binds to 127.0.0.1 only
echo "Starting socat CDP proxy (0.0.0.0:9222 -> 127.0.0.1:9223)..."
socat TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9223 &

# Start Chromium
echo "Starting Chromium..."
exec gosu chrome chromium \
    --no-sandbox \
    --disable-blink-features=AutomationControlled \
    --disable-dev-shm-usage \
    --remote-debugging-port=9223 \
    --user-data-dir=/data/chrome-profile \
    --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
    --start-maximized \
    --disable-gpu \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking=false \
    "$@"
