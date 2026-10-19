#!/bin/bash
# Health gate for one compose service after a targeted deploy.
#
# Passes when, before the timeout:
#   - the service container is running the expected image digest,
#   - its Docker healthcheck reports "healthy" (if it defines one), otherwise it
#     has stayed up without restarting for --stable seconds,
#   - and the optional HTTP probe returns 2xx/3xx.
# Fails fast if the container exits or restart-loops.
#
# Usage: wait-healthy.sh --project P --file F --service S [--expect-digest sha256:..]
#                        [--timeout 600] [--probe URL --probe-ip IP] [--stable 60]
set -uo pipefail

PROJECT=docker FILE="" SERVICE="" EXPECT="" TIMEOUT=600 PROBE="" PROBE_IP="" STABLE=60
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    --expect-digest) EXPECT="$2"; shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --probe) PROBE="$2"; shift 2 ;;
    --probe-ip) PROBE_IP="$2"; shift 2 ;;
    --stable) STABLE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$FILE" ] && [ -n "$SERVICE" ] || { echo "--file and --service are required" >&2; exit 2; }

say() { echo "[$(date -u +%H:%M:%S)] [$SERVICE] $*"; }
fail() {
  say "FAILED: $*"
  if [ -n "${CID:-}" ]; then
    say "last log lines:"
    docker logs --tail 20 "$CID" 2>&1 | sed 's/^/    /'
  fi
  exit 1
}

compose() { docker compose -p "$PROJECT" -f "$FILE" "$@"; }

CID=$(compose ps -aq "$SERVICE" 2>/dev/null | head -1)
[ -n "$CID" ] || fail "no container found for service"

IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CID")
say "container $(docker inspect -f '{{.Name}}' "$CID") image $IMAGE"
if [ -n "$EXPECT" ] && [[ "$IMAGE" != *"$EXPECT"* ]]; then
  fail "container is not running the expected digest $EXPECT"
fi

HAS_HC=$(docker inspect -f '{{if .State.Health}}yes{{else}}no{{end}}' "$CID")
R0=$(docker inspect -f '{{.RestartCount}}' "$CID")

probe_host=""; probe_port=443
if [ -n "$PROBE" ]; then
  probe_host=$(echo "$PROBE" | sed -E 's#^[a-z]+://([^/:]+).*#\1#')
  case "$PROBE" in http://*) probe_port=80 ;; esac
fi

probe_ok() {
  [ -z "$PROBE" ] && return 0
  local args=(-sk -o /dev/null -w '%{http_code}' --max-time 10)
  [ -n "$PROBE_IP" ] && args+=(--resolve "${probe_host}:${probe_port}:${PROBE_IP}")
  PROBE_CODE=$(curl "${args[@]}" "$PROBE" 2>/dev/null || true)
  [[ "$PROBE_CODE" =~ ^[23][0-9][0-9]$ ]]
}

say "healthcheck: $HAS_HC; probe: ${PROBE:-none}; timeout ${TIMEOUT}s"
START=$(date +%s)
DELAY=5
while :; do
  ELAPSED=$(( $(date +%s) - START ))
  STATUS=$(docker inspect -f '{{.State.Status}}' "$CID" 2>/dev/null || echo missing)
  RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$CID" 2>/dev/null || echo 0)
  HEALTH=$([ "$HAS_HC" = yes ] && docker inspect -f '{{.State.Health.Status}}' "$CID" || echo n/a)

  case "$STATUS" in
    exited|dead|missing) fail "container is $STATUS" ;;
  esac
  [ $(( RESTARTS - R0 )) -ge 3 ] && fail "container restarted $(( RESTARTS - R0 )) times (restart loop)"

  container_ok=no
  if [ "$STATUS" = running ]; then
    if [ "$HAS_HC" = yes ]; then
      [ "$HEALTH" = healthy ] && container_ok=yes
    else
      STARTED=$(date -d "$(docker inspect -f '{{.State.StartedAt}}' "$CID")" +%s 2>/dev/null || echo "$START")
      UP=$(( $(date +%s) - STARTED ))
      [ "$UP" -ge "$STABLE" ] && [ "$RESTARTS" = "$R0" ] && container_ok=yes
    fi
  fi
  PROBE_CODE="-"
  probe_result=no
  if [ "$container_ok" = yes ] && probe_ok; then probe_result=yes; fi

  say "t=${ELAPSED}s status=$STATUS health=$HEALTH restarts=$((RESTARTS - R0)) probe=${PROBE_CODE}"
  if [ "$container_ok" = yes ] && [ "$probe_result" = yes ]; then
    say "HEALTHY"
    exit 0
  fi
  [ "$ELAPSED" -ge "$TIMEOUT" ] && fail "not healthy after ${TIMEOUT}s (status=$STATUS health=$HEALTH probe=${PROBE_CODE})"
  sleep "$DELAY"
  DELAY=$(( DELAY * 2 )); [ "$DELAY" -gt 60 ] && DELAY=60
done
