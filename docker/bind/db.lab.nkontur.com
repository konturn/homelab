;
; BIND data file for local loopback interface
;
$TTL	604800
@	IN	SOA	bind.lab.nkontur.com root.localhost. (
			      2		; Serial
			 604800		; Refresh
			  86400		; Retry
			2419200		; Expire
			 604800 )	; Negative Cache TTL
;
lab.nkontur.com.    IN      NS      bind.lab.nkontur.com.
*.iot IN A {{ iot_nginx_ip }}
iot IN A {{ iot_nginx_ip }}
*        IN      A      {{ lab_nginx_ip }}
bind        IN      A      {{ bind_ip }}
@        IN      A      {{ lab_nginx_ip }}
