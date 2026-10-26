#!/usr/bin/env bash
set -euo pipefail

echo "Starting SSL certificate renewal..."

docker run --rm \
  -v /persistent_data/application/certs/nkontur.com:/etc/letsencrypt \
  -v /root/cron/credentials:/root/cloudflare.ini \
  --network host \
  certbot/dns-cloudflare:v5.1.0 certonly \
  --dns-cloudflare \
  --dns-cloudflare-credentials /root/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 60 \
  --email konoahko@gmail.com \
  --agree-tos --no-eff-email \
  --keep-until-expiring \
  -d *.iot.lab.nkontur.com \
  -d *.lab.nkontur.com \
  -d nkontur.com \
  -d homeassistant.nkontur.com \
  -d nextcloud.nkontur.com \
  -d blog.nkontur.com \
  -d bitwarden.nkontur.com \
  -d booksonic.nkontur.com \
  -d jit-webhook.nkontur.com \
  -v
echo "Certbot renewal succeeded."

CERT_ROOT=/persistent_data/application/certs/nkontur.com

# Certbot lineage directory. Deliberately a literal, NOT auto-detected from the
# live/ symlink: the same lineage name is hardcoded in six other files that read
# these certs, and renew.sh silently following a new lineage would leave every
# consumer pointing at the old one. If certbot ever rolls to -0004, all of these
# change together:
#   docker/nginx/ssl_config
#   docker/vault/config.hcl
#   docker/mqtt/mosquitto.conf
#   docker/docker-compose.yml        (VAULT_CACERT)
#   base/gitlab-runner/config.toml   (tls-ca-file)
#   networking/ssl/renew.sh          (this file)
LINEAGE=iot.lab.nkontur.com-0003

# Fix privkey permissions so mosquitto (gid 1883) can read it
chmod 640 "$CERT_ROOT/archive/$LINEAGE"/privkey*.pem
chgrp 1883 "$CERT_ROOT/archive/$LINEAGE"/privkey*.pem

# Push the cert to the zwave satellite. BEST EFFORT ONLY.
#
# This step used to be unguarded, and under `set -e` its failure aborted the
# script before any of the docker restarts below ever ran. zwave has been hard
# down since 2026-09-16 (issue #137), so the rsync has failed with "No route to
# host" on every run since, and the restarts have not executed once:
# measured in Loki over 28d, `Certbot renewal succeeded.` appears 4 times and
# `SSL renewal complete.` appears 0 times.
#
# That is latent rather than visible only because certbot keeps answering
# "Certificate not yet due for renewal". The shared wildcard cert (11 endpoints:
# nginx, gitlab, vault, grafana, sonarr, radarr, nzbget, tautulli, bitwarden,
# paperless-ngx, homeassistant) expires 2026-12-13, so certbot will issue a
# replacement around 2026-11-12 -- and on that run the new cert would be written
# to disk and no service would ever load it, because nothing else in this repo
# reloads them. The certs are bind-mounted read-only and read at startup; there
# is no inotify watcher and no `nginx -s reload` anywhere.
#
# A satellite that cannot be reached must not stop the local fleet from picking
# up a new certificate.
rsync_rc=0
rsync -avz -e 'ssh -i /root/.ssh/id_rsa -o ConnectTimeout=10' \
  "$CERT_ROOT/live/$LINEAGE" root@zwave.lab.nkontur.com:/certs/ || rsync_rc=$?
if [ "$rsync_rc" -eq 0 ]; then
  echo "Certificate sync to zwave succeeded."
else
  echo "WARNING: certificate sync to zwave FAILED (rsync exit ${rsync_rc}) -- continuing to the service restarts anyway."
fi

# Reload every service that reads the cert from disk at startup, but only the
# ones that started before the cert on disk was written. This runs weekly and
# certbot is a no-op on all but ~one run in eight, so restarting unconditionally
# bounced all three nginx fronts, mosquitto and vault every Monday 08:00Z for
# nothing (nginx bounces can surface as Loki/Influx DatasourceError pages).
#
# Comparing against each container's own start time, rather than asking "did
# certbot renew on THIS run", keeps the self-healing property: a restart that
# failed on the renewal run, or a cert issued by hand, is picked up next week.
# Each restart is independent: one container failing to come back must not leave
# the rest still serving the superseded certificate.
# If the cert files cannot be stat'ed, fall back to "now" so every service restarts.
cert_mtime=$(stat -L -c %Y "$CERT_ROOT/live/$LINEAGE/fullchain.pem" "$CERT_ROOT/live/$LINEAGE/privkey.pem" | sort -n | tail -1) || cert_mtime=$(date +%s)
restart_failed=0
for c in lab_nginx nginx iot_nginx mosquitto vault; do
  # GNU date reads an empty string as today's midnight, so guard it: a container
  # we cannot inspect falls through to the restart, which is the old behavior.
  started=$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null) || started=""
  started_epoch=0
  [ -n "$started" ] && { started_epoch=$(date -d "$started" +%s 2>/dev/null) || started_epoch=0; }
  if [ "$started_epoch" -gt "$cert_mtime" ]; then
    echo "${c}: started ${started}, after the current cert; no restart needed."
    continue
  fi
  echo "${c}: restarting to load the current cert."
  docker restart "$c" || { echo "WARNING: docker restart ${c} FAILED"; restart_failed=1; }
done

if [ "$restart_failed" -ne 0 ] || [ "$rsync_rc" -ne 0 ]; then
  echo "SSL renewal complete WITH WARNINGS (zwave sync rc=${rsync_rc}, restart_failed=${restart_failed})."
else
  echo "SSL renewal complete."
fi
