#!/bin/bash
# Pi-hole v6 stats fetcher for Telegraf
# Authenticates via /api/auth with app password, fetches stats, then
# deletes the session to avoid exhausting Pi-hole's session limit.
# Outputs InfluxDB line protocol.
#
# Environment variables:
#   PIHOLE_URL       - Pi-hole base URL (e.g., http://10.3.32.2)
#   PIHOLE_API_TOKEN - Pi-hole app password

set -euo pipefail

PIHOLE_URL="${PIHOLE_URL:?PIHOLE_URL must be set}"
PIHOLE_API_TOKEN="${PIHOLE_API_TOKEN:?PIHOLE_API_TOKEN must be set}"

# Authenticate to get a session ID
AUTH_RESPONSE=$(curl -sf -X POST "${PIHOLE_URL}/api/auth" \
    -H "Content-Type: application/json" \
    -d "{\"password\":\"${PIHOLE_API_TOKEN}\"}" 2>/dev/null) || {
    echo "Failed to authenticate with Pi-hole" >&2
    exit 1
}

SID=$(jq -r '.session.sid // empty' <<< "$AUTH_RESPONSE")
if [[ -z "$SID" ]]; then
    echo "Failed to obtain SID from Pi-hole auth response" >&2
    exit 1
fi

# Ensure session is cleaned up on exit
trap 'curl -sf -X DELETE "${PIHOLE_URL}/api/auth?sid=${SID}" >/dev/null 2>&1' EXIT

# Fetch summary stats
RESPONSE=$(curl -sf "${PIHOLE_URL}/api/stats/summary?sid=${SID}" 2>/dev/null) || {
    echo "Failed to fetch Pi-hole stats" >&2
    exit 1
}

# Parse and output as InfluxDB line protocol
jq -r '
    "pihole queries_total=" + (.queries.total | tostring) + "i" +
    ",queries_blocked=" + (.queries.blocked | tostring) + "i" +
    ",queries_percent_blocked=" + (.queries.percent_blocked | tostring) +
    ",queries_forwarded=" + (.queries.forwarded | tostring) + "i" +
    ",queries_cached=" + (.queries.cached | tostring) + "i" +
    ",queries_unique_domains=" + (.queries.unique_domains | tostring) + "i" +
    ",queries_frequency=" + (.queries.frequency | tostring) +
    ",clients_active=" + (.clients.active | tostring) + "i" +
    ",clients_total=" + (.clients.total | tostring) + "i" +
    ",gravity_domains_being_blocked=" + (.gravity.domains_being_blocked | tostring) + "i"
' <<< "$RESPONSE"
