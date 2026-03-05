FROM alpine:3.21

RUN apk add --no-cache \
    nut \
    nut-snmp \
    net-snmp-tools \
    && mkdir -p /var/run/nut /etc/nut \
    && chown nut:nut /var/run/nut

# Copy config files (templates rendered by Ansible)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 3493

ENTRYPOINT ["/entrypoint.sh"]
