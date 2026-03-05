#!/bin/bash
# Fetch Deluge stats via web API (requires session cookie auth)
# Outputs InfluxDB line protocol

DELUGE_HOST="${DELUGE_HOST:-localhost}"
DELUGE_PORT="${DELUGE_PORT:-8112}"
DELUGE_PASSWORD="${DELUGE_PASSWORD:-deluge}"
COOKIE_JAR=$(mktemp)

trap "rm -f $COOKIE_JAR" EXIT

# Authenticate
AUTH_RESPONSE=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
  "http://${DELUGE_HOST}:${DELUGE_PORT}/json" \
  -H "Content-Type: application/json" \
  -d '{"method":"auth.login","params":["'"${DELUGE_PASSWORD}"'"],"id":1}' \
  --max-time 5 2>/dev/null)

if [ $? -ne 0 ] || [ -z "$AUTH_RESPONSE" ]; then
  exit 0  # Silently fail — service may be down
fi

AUTH_OK=$(echo "$AUTH_RESPONSE" | jq -r '.result // false')
if [ "$AUTH_OK" != "true" ]; then
  exit 0
fi

# Get session status
STATS=$(curl -s -b "$COOKIE_JAR" \
  "http://${DELUGE_HOST}:${DELUGE_PORT}/json" \
  -H "Content-Type: application/json" \
  -d '{"method":"web.update_ui","params":[["download_rate","upload_rate","num_connections","dht_nodes"],{}],"id":2}' \
  --max-time 5 2>/dev/null)

if [ $? -ne 0 ] || [ -z "$STATS" ]; then
  exit 0
fi

DL_RATE=$(echo "$STATS" | jq '.result.stats.download_rate // 0')
UL_RATE=$(echo "$STATS" | jq '.result.stats.upload_rate // 0')
NUM_CONN=$(echo "$STATS" | jq '.result.stats.num_connections // 0')
DHT_NODES=$(echo "$STATS" | jq '.result.stats.dht_nodes // 0')
NUM_TORRENTS=$(echo "$STATS" | jq '.result.torrents | length // 0')

echo "deluge download_rate=${DL_RATE},upload_rate=${UL_RATE},num_connections=${NUM_CONN}i,dht_nodes=${DHT_NODES}i,num_torrents=${NUM_TORRENTS}i"
