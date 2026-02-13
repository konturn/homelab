#!/bin/bash
# Check browser availability for job hunting
# Returns: "chromium" (exit 0), "node" (exit 1), or "unavailable" (exit 2)

# Primary: Chromium sidecar on Docker internal network
# Must use IP to avoid Host header rejection from CDP
if curl -s --max-time 5 -H "Host: localhost" "http://chromium-browser:9222/json/version" >/dev/null 2>&1; then
    echo "chromium"
    exit 0
fi

# Also try by IP in case DNS isn't resolving
CHROMIUM_IP=$(getent hosts chromium-browser 2>/dev/null | awk '{print $1}')
if [ -n "$CHROMIUM_IP" ] && curl -s --max-time 5 "http://${CHROMIUM_IP}:9222/json/version" >/dev/null 2>&1; then
    echo "chromium"
    exit 0
fi

# Fallback: check for sandbox localhost browser
if curl -s --max-time 5 "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
    echo "sandbox"
    exit 0
fi

echo "unavailable"
exit 2
