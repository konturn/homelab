#!/bin/sh
# Bring the retired WordPress blog up on the LAN to edit it, then re-export.
# See docker/wordpress/README.md. Run on the router as root.
#
#   edit-session.sh start    start blog + wordpress_db and a LAN-only TLS proxy
#   edit-session.sh export   write a fresh static export to a new directory
#   edit-session.sh stop     stop the proxy and both WordPress containers
#
# The proxy is a throwaway nginx container on the `internal` (lab LAN) network
# with the existing *.nkontur.com certificate; the public nginx never proxies
# to WordPress. Point blog.nkontur.com at the printed address in the editing
# machine's /etc/hosts for the session, then remove the entry.
set -eu

COMPOSE="docker compose -p docker -f /persistent_data/application/ansible_state/docker-compose.yml --profile wordpress"
HERE="$(cd "$(dirname "$0")" && pwd)"
PROXY=blog-edit-proxy
# Same image as the nginx service in docker-compose.yml.
PROXY_IMAGE="$(docker inspect -f '{{.Config.Image}}' nginx)"

case "${1:-}" in
start)
  $COMPOSE up -d blog wordpress_db
  docker rm -f "$PROXY" >/dev/null 2>&1 || true
  docker run -d --name "$PROXY" --network docker_wordpress_internal \
    --read-only --tmpfs /tmp --tmpfs /run --tmpfs /var/cache/nginx \
    -v "$HERE/edit-proxy.conf:/etc/nginx/conf.d/default.conf:ro" \
    -v /persistent_data/application/nginx/conf/ssl_config:/etc/nginx/ssl_config:ro \
    -v /persistent_data/application/certs:/data/certs:ro \
    "$PROXY_IMAGE" >/dev/null
  docker network connect docker_internal "$PROXY"
  ip=$(docker inspect -f '{{(index .NetworkSettings.Networks "docker_internal").IPAddress}}' "$PROXY")
  echo "WordPress is up. On the editing machine add to /etc/hosts:"
  echo "  $ip blog.nkontur.com"
  echo "then open https://blog.nkontur.com/wp-admin/ (LAN only)."
  ;;
export)
  out="${2:-/tmp/blog-static-$(date +%Y%m%d%H%M%S)}"
  python3 -I "$HERE/export_static.py" --out "$out"
  echo "Export in $out. Replace docker/nginx/sites/blog.nkontur.com/ with it and open an MR."
  ;;
stop)
  docker rm -f "$PROXY" >/dev/null 2>&1 || true
  $COMPOSE stop blog wordpress_db
  ;;
*)
  sed -n '2,12p' "$0"
  exit 2
  ;;
esac
