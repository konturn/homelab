docker run --rm -e "NAMESILO_API_KEY=$1" -e "NAMESILO_PROPAGATION_TIMEOUT=100000" --network docker_internal -v {{ docker_persistent_data_path }}/certs/nkontur.com:/ssl goacme/lego --dns namesilo --domains *.nkontur.com --domains nkontur.com --domains *.lab.nkontur.com --email konoahko@gmail.com --dns.resolvers 1.1.1.1 --path /ssl -a run
scp -i /root/.ssh/id_rsa {{ docker_persistent_data_path }}/certs/nkontur.com/certificates/_.nkontur.com.crt root@vps.nkontur.com:/etc/ssl/certs/nkontur.com/_.nkontur.com.crt
scp -i /root/.ssh/id_rsa {{ docker_persistent_data_path }}/certs/nkontur.com/certificates/_.nkontur.com.key root@vps.nkontur.com:/etc/ssl/private/_.nkontur.com.key
rsync -avz -e 'ssh -i /root/.ssh/id_rsa' {{ docker_persistent_data_path }}/certs/nkontur.com/certificates/ root@zwave.lab.nkontur.com:/certs/
ssh -i /root/.ssh/id_rsa vps.nkontur.com "chown konoahko:konoahko /etc/ssl/certs/nkontur.com/_.nkontur.com.crt && chown root:ssl-cert /etc/ssl/private/_.nkontur.com.key"
ssh -i /root/.ssh/id_rsa vps.nkontur.com "systemctl restart postfix dovecot"
docker restart lab_nginx
docker restart nginx
