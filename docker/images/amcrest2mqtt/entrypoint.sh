#!/bin/sh
# Restart backoff wrapper for amcrest2mqtt.
#
# Upstream exits non-zero as soon as it cannot reach the camera. It spends
# ~13s retrying the HTTP connection first, which is longer than the 10s
# Docker uses to decide a container "started successfully" — so Docker's own
# exponential restart backoff is reset on every attempt and never engages.
# With `restart: unless-stopped` that produces a permanent hot loop of roughly
# 6,600 restarts/day whenever the doorbell is offline.
#
# Sleeping before we exit makes the failure cheap: the container is only
# recreated once per backoff window instead of every 13 seconds.
set -eu

BACKOFF="${AMCREST_RESTART_BACKOFF_SECONDS:-300}"

set +e
"$@"
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
  echo "amcrest2mqtt exited with status ${rc}; backing off ${BACKOFF}s before restart" >&2
  # Sleep in the background and wait on it so a SIGTERM from `docker stop`
  # is handled immediately instead of blocking until the 10s kill timeout.
  trap 'exit 143' INT TERM
  sleep "$BACKOFF" &
  wait "$!"
fi

exit "$rc"
