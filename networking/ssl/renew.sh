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
rsync -avz -e 'ssh -i /root/.ssh/id_rsa' /persistent_data/application/certs/nkontur.com/live/iot.lab.nkontur.com-0003 root@zwave.lab.nkontur.com:/certs/
docker restart lab_nginx
docker restart nginx
