docker run --rm \
  -v /persistent_data/application/certs/nkontur.com:/etc/letsencrypt \
  -v /root/cron/credentials:/root/cloudflare.ini \
  certbot/dns-cloudflare:v2.10.0 certonly \
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
  -d blog.nkontur.com \
  -d bitwarden.nkontur.com \
  -d booksonic.nkontur.com \
  -v
scp -i /root/.ssh/id_rsa /persistent_data/application/certs/nkontur.com/certificates/_.nkontur.com.crt root@vps.nkontur.com:/etc/ssl/certs/nkontur.com/_.nkontur.com.crt
scp -i /root/.ssh/id_rsa /persistent_data/application/certs/nkontur.com/certificates/_.nkontur.com.key root@vps.nkontur.com:/etc/ssl/private/_.nkontur.com.key
rsync -avz -e 'ssh -i /root/.ssh/id_rsa' /persistent_data/application/certs/nkontur.com/live/iot.lab.nkontur.com-0001 root@zwave.lab.nkontur.com:/certs/
ssh -i /root/.ssh/id_rsa vps.nkontur.com "chown konoahko:konoahko /etc/ssl/certs/nkontur.com/_.nkontur.com.crt && chown root:ssl-cert /etc/ssl/private/_.nkontur.com.key"
ssh -i /root/.ssh/id_rsa vps.nkontur.com "systemctl restart postfix dovecot"
docker restart lab_nginx
docker restart nginx
