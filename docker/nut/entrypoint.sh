#!/bin/sh
set -e

# Ensure proper ownership
chown root:nut /etc/nut/*.conf /etc/nut/*.users 2>/dev/null || true
chmod 640 /etc/nut/ups.conf /etc/nut/upsd.conf /etc/nut/upsd.users /etc/nut/upsmon.conf 2>/dev/null || true
chown nut:nut /var/run/nut
chmod 755 /etc/nut/notify.sh /sbin/nutshutdown 2>/dev/null || true

echo "Starting NUT SNMP UPS driver..."
/usr/sbin/upsdrvctl -u root start

echo "Starting NUT data server (upsd)..."
/usr/sbin/upsd -u nut

echo "Starting NUT monitor (upsmon)..."
exec /usr/sbin/upsmon -D
