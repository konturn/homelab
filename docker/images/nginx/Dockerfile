# nginx (edge, lab, iot, bluebubbles-relay) with current Alpine brotli-libs.
#
# The upstream nginx:*-alpine-perl image has not been rebuilt against the
# current brotli-libs package yet. This derives from it unchanged and upgrades
# only brotli-libs from the Alpine repos; nginx, its modules and every other
# package stay exactly as upstream ships them.
#
# Remove this image (point docker/docker-compose.yml back at upstream nginx)
# once the upstream image ships brotli-libs >= BROTLI_MIN on its own.
#
# Bootstrap note: gitlab-registry.lab.nkontur.com is served by lab_nginx, which
# runs this image. If the image is ever missing from the router's local store
# while lab_nginx is down, build it on the host instead of pulling it:
#   docker build -t gitlab-registry.lab.nkontur.com:443/root/homelab/nginx:latest docker/images/nginx
# (needs only Docker Hub and the Alpine mirror, not the registry).
FROM nginx:1.31.2-alpine3.23-perl@sha256:b3b337e1ef5b1bc8253dec68bc4522d9027920666a428bf51a022ceb7e3396bc

# Lowest acceptable brotli-libs. The build fails if a base bump or a mirror
# lag would leave an older one in the image.
ARG BROTLI_MIN=1.2.1-r0

# Fails the build if brotli-libs is below BROTLI_MIN, if a module the stock
# image ships has gone missing, or if the stock config no longer loads.
RUN set -eu; \
    apk upgrade --no-cache brotli-libs; \
    v="$(awk '/^P:/{p=substr($0,3)} /^V:/{if (p=="brotli-libs") print substr($0,3)}' /lib/apk/db/installed)"; \
    test -n "$v" || { echo "brotli-libs not installed" >&2; exit 1; }; \
    case "$(apk version -t "$v" "$BROTLI_MIN")" in \
      "<") echo "brotli-libs $v is older than $BROTLI_MIN" >&2; exit 1 ;; \
    esac; \
    echo "brotli-libs $v (>= $BROTLI_MIN)"; \
    for m in ngx_http_perl_module ngx_http_js_module ngx_stream_js_module \
             ngx_http_geoip_module ngx_stream_geoip_module ngx_http_image_filter_module \
             ngx_http_xslt_filter_module ngx_http_acme_module; do \
      test -f "/usr/lib/nginx/modules/$m.so" || { echo "missing module $m" >&2; exit 1; }; \
    done; \
    nginx -t
