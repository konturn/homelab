#!/bin/sh
# NUT notification handler
# Sends UPS events to a log (picked up by Promtail → Loki → Grafana alerts)
# and optionally to an external webhook
#
# $NOTIFYTYPE is set by NUT (ONLINE, ONBATT, LOWBATT, FSD, SHUTDOWN, etc.)
# $1 is the notification message

logger -t nut-notify "$NOTIFYTYPE: $1"

# If a webhook URL is configured, POST the event
if [ -n "$NUT_NOTIFY_WEBHOOK" ]; then
    wget -q --timeout=5 --post-data="{\"text\":\"🔋 NUT: $1\",\"type\":\"$NOTIFYTYPE\"}" \
        --header="Content-Type: application/json" \
        "$NUT_NOTIFY_WEBHOOK" -O /dev/null 2>/dev/null || true
fi
