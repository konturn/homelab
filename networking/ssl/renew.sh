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
  --force-renewal \
  -d *.iot.lab.nkontur.com \
  -d *.lab.nkontur.com \
  -d nkontur.com \
  -d homeassistant.nkontur.com \
  -d nextcloud.nkontur.com \
  -d blog.nkontur.com \
  -d bitwarden.nkontur.com \
  -d booksonic.nkontur.com \
  -v
echo "Certbot renewal succeeded."

rsync -avz -e 'ssh -i /root/.ssh/id_rsa' /persistent_data/application/certs/nkontur.com/live/iot.lab.nkontur.com-0003 root@zwave.lab.nkontur.com:/certs/
echo "Certificate sync to zwave succeeded."

docker restart lab_nginx
docker restart nginx
echo "SSL renewal complete."
