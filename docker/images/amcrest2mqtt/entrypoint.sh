#!/bin/sh
# Supervisor for amcrest2mqtt: waits for the camera instead of exiting.
#
# Upstream exits non-zero as soon as it cannot reach the camera. Letting that
# exit reach Docker made an offline doorbell a permanent restart loop: first
# ~6,600 restarts/day (the ~13s failure defeated Docker's backoff), then ~290/day
# with the 300s sleep-before-exit this wrapper used to do (!694, !834). Either
# way Container Restart Loop fired for as long as the doorbell was down, even
# though "camera offline" is already reported by IoT Device Unreachable.
#
# So this wrapper never exits because the camera is down:
#   - while AMCREST_HOST:AMCREST_PORT refuses TCP connections, it waits in
#     place, re-probing with exponential backoff capped at
#     AMCREST_RETRY_MAX_SECONDS (default 300), and touches $WAITING_FLAG so the
#     healthcheck reports the container healthy-but-idle;
#   - once the camera answers, it starts amcrest2mqtt; if that run ends and the
#     camera is gone again, it goes back to waiting.
# It does exit when amcrest2mqtt fails while the camera IS reachable. That is a
# software/config fault (bad credentials, MQTT down, upstream bug), and it must
# stay visible to Docker's restart policy and the restart-loop alert.
set -u

HOST="${AMCREST_HOST:?AMCREST_HOST is required}"
PORT="${AMCREST_PORT:-80}"
MAX_DELAY="${AMCREST_RETRY_MAX_SECONDS:-300}"
WAITING_FLAG=/tmp/amcrest-waiting-for-camera

child=""
on_term() {
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
  exit 143
}
trap on_term INT TERM

reachable() {
  python -c 'import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 5).close()' \
    "$HOST" "$PORT" 2>/dev/null
}

# Sleep in the background and wait on it so `docker stop` is handled at once.
pause() {
  sleep "$1" &
  child=$!
  wait "$child"
  child=""
}

delay=10
while :; do
  if ! reachable; then
    if [ ! -e "$WAITING_FLAG" ]; then
      echo "camera ${HOST}:${PORT} unreachable; waiting for it (retry backoff up to ${MAX_DELAY}s)" >&2
      touch "$WAITING_FLAG"
    fi
    pause "$delay"
    delay=$((delay * 2))
    [ "$delay" -gt "$MAX_DELAY" ] && delay="$MAX_DELAY"
    continue
  fi

  if [ -e "$WAITING_FLAG" ]; then
    echo "camera ${HOST}:${PORT} reachable again; starting amcrest2mqtt" >&2
    rm -f "$WAITING_FLAG"
  fi
  delay=10

  "$@" &
  child=$!
  wait "$child"
  rc=$?
  child=""

  if reachable; then
    echo "amcrest2mqtt exited with status ${rc} while the camera is reachable; exiting" >&2
    exit "$rc"
  fi
  echo "amcrest2mqtt exited with status ${rc}; camera ${HOST}:${PORT} went away" >&2
done
