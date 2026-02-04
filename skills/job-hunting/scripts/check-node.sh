#!/bin/bash
# Check if Noah's XPS node is connected
# Exit 0 if connected, exit 1 if not
# Usage: ./check-node.sh

NODE_NAME="noah-XPS-13-7390-2-in-1"

# Use the nodes tool via moltbot CLI
STATUS=$(moltbot nodes status 2>/dev/null | jq -r ".nodes[] | select(.name == \"$NODE_NAME\") | .connected" 2>/dev/null)

if [ "$STATUS" = "true" ]; then
    echo "connected"
    exit 0
else
    echo "disconnected"
    exit 1
fi
