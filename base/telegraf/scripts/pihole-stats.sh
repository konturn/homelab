#!/bin/bash
# Pi-hole v6 stats fetcher for Telegraf
# Pi-hole v6 uses session-based auth — POST to /api/auth, then use SID.
# Outputs InfluxDB line protocol.
#
# Environment variables:
#   PIHOLE_URL       - Pi-hole base URL (e.g., http://10.3.32.2)
#   PIHOLE_API_TOKEN - Pi-hole API token (app password), used directly as SID

set -euo pipefail

PIHOLE_URL="${PIHOLE_URL:?PIHOLE_URL must be set}"
PIHOLE_API_TOKEN="${PIHOLE_API_TOKEN:?PIHOLE_API_TOKEN must be set}"

SID_PARAM="?sid=${PIHOLE_API_TOKEN}"

# Fetch summary stats
RESPONSE=$(curl -sf "${PIHOLE_URL}/api/stats/summary${SID_PARAM}" 2>/dev/null) || {
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
