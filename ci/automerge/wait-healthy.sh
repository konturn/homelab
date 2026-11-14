#!/bin/bash
# Health gate for one compose service after a targeted deploy.
#
# Passes when, before the timeout:
#   - the service container is running the expected image digest,
#   - its Docker healthcheck reports "healthy" with no failing checks (if it
#     defines one), otherwise it has not restarted,
#   - the optional HTTP probe returns 2xx/3xx,
#   - and all of that has held continuously for --stable seconds.
# Fails fast if the container exits or restart-loops.
#
# Why the hold applies to healthchecked services too: Docker probes every 5s
# during start_period, so a container is "healthy" after one good answer. On
# 2026-11-14 plex passed this gate 5s after a recreate, failed its next check
# ~45s later and was unhealthy from 07:59Z on, while the deploy reported
# success and no rollback ran. FailingStreak counts failed checks since the
# last success, so it shows a failure one check after it happens, long before
# Docker flips the status to unhealthy (interval x retries).
#
# Usage: wait-healthy.sh --project P --service S [--expect-digest sha256:..]
#                        [--timeout 600] [--probe URL --probe-ip IP] [--stable 60]
# The container is found by its compose labels (project + service), so this
# works for projects built from several compose files. --file is accepted and
# ignored (older callers).
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
[ -n "$SERVICE" ] || { echo "--service is required" >&2; exit 2; }

say() { echo "[$(date -u +%H:%M:%S)] [$SERVICE] $*"; }
fail() {
  say "FAILED: $*"
  if [ -n "${CID:-}" ]; then
    say "last log lines:"
    docker logs --tail 20 "$CID" 2>&1 | sed 's/^/    /'
  fi
  exit 1
}

CID=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
  --filter "label=com.docker.compose.service=$SERVICE" 2>/dev/null | head -1)
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

say "healthcheck: $HAS_HC; probe: ${PROBE:-none}; hold ${STABLE}s; timeout ${TIMEOUT}s"
START=$(date +%s)
OK_SINCE=""
DELAY=5
while :; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START ))
  STATUS=$(docker inspect -f '{{.State.Status}}' "$CID" 2>/dev/null || echo missing)
  RESTARTS=$(docker inspect -f '{{.RestartCount}}' "$CID" 2>/dev/null || echo 0)
  HEALTH=n/a STREAK=0
  if [ "$HAS_HC" = yes ]; then
    read -r HEALTH STREAK < <(docker inspect -f '{{.State.Health.Status}} {{.State.Health.FailingStreak}}' "$CID" 2>/dev/null || echo "unknown 0")
  fi

  case "$STATUS" in
    exited|dead|missing) fail "container is $STATUS" ;;
  esac
  [ $(( RESTARTS - R0 )) -ge 3 ] && fail "container restarted $(( RESTARTS - R0 )) times (restart loop)"

  # A restart since the previous sample breaks the hold. With a healthcheck,
  # one or two restarts during startup are tolerated (as before); without
  # one, staying up without any restart is the only signal, so none are.
  container_ok=no
  if [ "$STATUS" = running ] && [ "$RESTARTS" = "${LAST_RESTARTS:-$R0}" ]; then
    if [ "$HAS_HC" = yes ]; then
      [ "$HEALTH" = healthy ] && [ "${STREAK:-0}" = 0 ] && container_ok=yes
    else
      [ "$RESTARTS" = "$R0" ] && container_ok=yes
    fi
  fi
  LAST_RESTARTS=$RESTARTS
  PROBE_CODE="-"
  ok=no
  if [ "$container_ok" = yes ] && probe_ok; then ok=yes; fi

  if [ "$ok" = yes ]; then
    [ -n "$OK_SINCE" ] || OK_SINCE=$NOW
  else
    [ -n "$OK_SINCE" ] && say "lost health after $(( NOW - OK_SINCE ))s of the ${STABLE}s hold"
    OK_SINCE=""
  fi

  HELD=$([ -n "$OK_SINCE" ] && echo $(( NOW - OK_SINCE )) || echo 0)
  say "t=${ELAPSED}s status=$STATUS health=$HEALTH failing=${STREAK:-0} restarts=$((RESTARTS - R0)) probe=${PROBE_CODE} held=${HELD}s"
  if [ -n "$OK_SINCE" ] && [ "$HELD" -ge "$STABLE" ]; then
    say "HEALTHY (held ${HELD}s)"
    exit 0
  fi
  [ "$ELAPSED" -ge "$TIMEOUT" ] && fail "not healthy for ${STABLE}s within ${TIMEOUT}s (status=$STATUS health=$HEALTH failing=${STREAK:-0} probe=${PROBE_CODE})"
  if [ -n "$OK_SINCE" ]; then
    # Holding: sample often enough to see every healthcheck interval.
    sleep 10
  else
    sleep "$DELAY"
    DELAY=$(( DELAY * 2 )); [ "$DELAY" -gt 60 ] && DELAY=60
  fi
done
