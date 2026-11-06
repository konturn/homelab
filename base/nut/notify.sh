#!/bin/sh
# NUT notification handler (NOTIFYCMD in upsmon.conf; runs as the nut user)
# Sends UPS events to a log (picked up by Promtail → Loki → Grafana alerts),
# optionally to an external webhook, and then to upssched, which runs the
# early-shutdown timers in upssched.conf.
#
# $NOTIFYTYPE is set by NUT (ONLINE, ONBATT, LOWBATT, FSD, SHUTDOWN, etc.)
# $UPSNAME is set by NUT
# $1 is the notification message

logger -t nut-notify "$NOTIFYTYPE: $1"

# If a webhook URL is configured, POST the event
if [ -n "$NUT_NOTIFY_WEBHOOK" ]; then
    wget -q --timeout=5 --post-data="{\"text\":\"🔋 NUT: $1\",\"type\":\"$NOTIFYTYPE\"}" \
        --header="Content-Type: application/json" \
        "$NUT_NOTIFY_WEBHOOK" -O /dev/null 2>/dev/null || true
fi

# upssched reads NOTIFYTYPE and UPSNAME from the environment. It only acts
# on the events upssched.conf lists (ONBATT, ONLINE).
if [ -x /usr/sbin/upssched ]; then
    /usr/sbin/upssched "$1" || logger -t nut-notify "upssched failed for $NOTIFYTYPE"
fi
