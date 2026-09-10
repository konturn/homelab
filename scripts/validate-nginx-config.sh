#!/bin/sh
# Validate every nginx config in docker/nginx/ with `nginx -t`.
#
# The configs are deployed verbatim (no Jinja2), one nginx instance per
# host+network, laid out by ansible/roles/configure-homelab-services as:
#
#   <dest>/conf/nginx.conf          <- docker/nginx/nginx.conf
#   <dest>/conf/conf.d/http.conf    <- docker/nginx/<host>/<network>_http.conf
#   <dest>/conf/conf.d/stream.conf  <- docker/nginx/<host>/<network>_stream.conf
#   <dest>/conf/ssl_config          <- docker/nginx/ssl_config
#
# This script reproduces that layout per instance, so a syntax error fails the
# pipeline instead of taking every public vhost down on deploy.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
NGINX_DIR="$REPO/docker/nginx"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

# ssl_config points at real LetsEncrypt paths. Generate self-signed material at
# exactly those paths so the real ssl_config is validated unmodified.
CERT_DIR=/data/certs/nkontur.com/live/iot.lab.nkontur.com-0003
mkdir -p "$CERT_DIR" /data/certs/nkontur.com/certificates /data/log /data/webroot
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=validate.invalid \
  -keyout "$CERT_DIR/privkey.pem" -out "$CERT_DIR/fullchain.pem" 2>/dev/null
cp "$CERT_DIR/fullchain.pem" "$CERT_DIR/chain.pem"
# 1024-bit is fine here: nginx only parses the file, it never negotiates with it.
openssl dhparam -out /data/certs/nkontur.com/certificates/dhparams.pem 1024 2>/dev/null

# A proxy_pass with a literal hostname is resolved when the config loads, so the
# container names must exist in /etc/hosts or every check dies on "host not found
# in upstream". Extracted rather than hardcoded so new upstreams are picked up.
# Names containing a dot are real DNS and left alone.
grep -rhoE 'proxy_pass +(https?://)?[a-zA-Z0-9_.-]+(:[0-9]+)?|server +[a-zA-Z0-9_.-]+:[0-9]+' "$NGINX_DIR" \
  | sed -E 's|proxy_pass +(https?://)?||; s|server +||; s|:[0-9]+$||' \
  | grep -vE '^\$|\.' | sort -u > "$OUT" || true
while read -r h; do
  [ -n "$h" ] || continue
  if ! grep -qE "[[:space:]]${h}\$" /etc/hosts; then
    echo "127.0.0.1 $h" >> /etc/hosts
  fi
done < "$OUT"

# nginx.conf uses an absolute `include /etc/nginx/conf.d/*`, so the config under
# test has to be staged at that real path — `nginx -t -p <prefix>` would silently
# validate the image's stock default.conf instead.
cp "$NGINX_DIR/nginx.conf" /etc/nginx/nginx.conf
cp "$NGINX_DIR/ssl_config" /etc/nginx/ssl_config

rc=0
found=0
for http_conf in "$NGINX_DIR"/*/*_http.conf "$NGINX_DIR"/*/http.conf; do
  [ -f "$http_conf" ] || continue
  found=$((found + 1))
  host_dir="$(dirname "$http_conf")"
  host="$(basename "$host_dir")"
  base="$(basename "$http_conf" .conf)"
  network="${base%_http}"
  if [ "$network" = "http" ]; then
    stream_conf="$host_dir/stream.conf"
  else
    stream_conf="$host_dir/${network}_stream.conf"
  fi

  rm -rf /etc/nginx/conf.d
  mkdir -p /etc/nginx/conf.d
  cp "$http_conf" /etc/nginx/conf.d/http.conf
  if [ -f "$stream_conf" ]; then
    cp "$stream_conf" /etc/nginx/conf.d/stream.conf
  fi

  printf '==> %s / %s\n' "$host" "$network"
  # Deliberately not `nginx -t | sed`: that reports sed's exit status and every
  # config would pass.
  if nginx -t -c /etc/nginx/nginx.conf > "$OUT" 2>&1; then
    sed 's/^/    /' "$OUT"
  else
    sed 's/^/    /' "$OUT"
    rc=1
  fi
done

if [ "$found" -eq 0 ]; then
  echo "ERROR: no nginx configs found under $NGINX_DIR — check the glob" >&2
  exit 1
fi

if [ "$rc" -eq 0 ]; then
  echo "All $found nginx configs valid."
else
  echo "nginx validation FAILED" >&2
fi
exit "$rc"
