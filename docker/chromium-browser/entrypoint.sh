#!/bin/bash
set -euo pipefail

cleanup() {
    echo "Shutting down..."
    kill $(jobs -p) 2>/dev/null || true
    wait
    exit 0
}
trap cleanup SIGTERM SIGINT SIGQUIT

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

# Start Chromium
echo "Starting Chromium..."
exec chromium \
    --no-sandbox \
    --disable-blink-features=AutomationControlled \
    --disable-dev-shm-usage \
    --remote-debugging-port=9222 \
    --remote-debugging-address=0.0.0.0 \
    --user-data-dir=/data/chrome-profile \
    --window-size="${SCREEN_WIDTH},${SCREEN_HEIGHT}" \
    --start-maximized \
    --disable-gpu \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking=false \
    "$@"
