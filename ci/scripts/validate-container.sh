#!/bin/sh
# =============================================================================
# Container Startup Validation
# =============================================================================
# Spins up a container and validates it becomes healthy.
# Used in CI to catch config errors before deploying to production.
#
# Usage: validate-container.sh <container_name> <image> <healthcheck_cmd> [options]
#
# Environment:
#   VALIDATE_TIMEOUT     - Max seconds to wait for healthy (default: 300)
#   VALIDATE_INTERVAL    - Seconds between health checks (default: 10)
#   VALIDATE_VOLUMES     - Space-separated volume mounts (-v args)
#   VALIDATE_ENV         - Space-separated env vars (-e args)
#   VALIDATE_MEMORY      - Memory limit (e.g., 8g)
#   DOCKER_HOST          - Docker daemon URL
# =============================================================================

set -eu

CONTAINER_NAME="${1:?Usage: validate-container.sh <name> <image> <healthcheck_cmd>}"
IMAGE="${2:?Missing image argument}"
HEALTHCHECK_CMD="${3:?Missing healthcheck command}"

TIMEOUT="${VALIDATE_TIMEOUT:-300}"
INTERVAL="${VALIDATE_INTERVAL:-10}"
MEMORY="${VALIDATE_MEMORY:-}"

cleanup() {
  echo "🧹 Cleaning up container: ${CONTAINER_NAME}"
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
}

trap cleanup EXIT

echo "═══════════════════════════════════════════════════════════════"
echo "Container Validation: ${CONTAINER_NAME}"
echo "  Image:       ${IMAGE}"
echo "  Healthcheck: ${HEALTHCHECK_CMD}"
echo "  Timeout:     ${TIMEOUT}s"
echo "═══════════════════════════════════════════════════════════════"

# Build docker run command
RUN_ARGS="--name ${CONTAINER_NAME} -d"

if [ -n "$MEMORY" ]; then
  RUN_ARGS="$RUN_ARGS --memory=$MEMORY"
fi

# Add volume mounts
if [ -n "${VALIDATE_VOLUMES:-}" ]; then
  for vol in $VALIDATE_VOLUMES; do
    RUN_ARGS="$RUN_ARGS -v $vol"
  done
fi

# Add environment variables
if [ -n "${VALIDATE_ENV:-}" ]; then
  for env_var in $VALIDATE_ENV; do
    RUN_ARGS="$RUN_ARGS -e $env_var"
  done
fi

echo "▶ Starting container..."
# shellcheck disable=SC2086
docker run $RUN_ARGS "$IMAGE"

echo "▶ Waiting for healthy (up to ${TIMEOUT}s)..."
elapsed=0
while [ "$elapsed" -lt "$TIMEOUT" ]; do
  # Run healthcheck command inside container
  if docker exec "$CONTAINER_NAME" sh -c "$HEALTHCHECK_CMD" >/dev/null 2>&1; then
    echo "✅ Container ${CONTAINER_NAME} is healthy after ${elapsed}s"
    # Show brief log tail for context
    echo ""
    echo "── Last 10 log lines ──"
    docker logs --tail 10 "$CONTAINER_NAME" 2>&1 || true
    exit 0
  fi

  # Check if container died
  STATE=$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "missing")
  if [ "$STATE" = "exited" ] || [ "$STATE" = "dead" ] || [ "$STATE" = "missing" ]; then
    echo "❌ Container ${CONTAINER_NAME} died (state: ${STATE})"
    echo ""
    echo "── Container logs ──"
    docker logs --tail 50 "$CONTAINER_NAME" 2>&1 || true
    exit 1
  fi

  sleep "$INTERVAL"
  elapsed=$((elapsed + INTERVAL))
  echo "  ${elapsed}s - container ${STATE}, waiting..."
done

echo "❌ Container ${CONTAINER_NAME} failed to become healthy within ${TIMEOUT}s"
echo ""
echo "── Container logs ──"
docker logs --tail 50 "$CONTAINER_NAME" 2>&1 || true
exit 1
