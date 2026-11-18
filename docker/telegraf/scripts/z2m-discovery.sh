#!/bin/sh
# Emits one z2m_discovery point, in influx line protocol:
#   z2m_discovery devices=19i,configs=23i,devices_missing=0i,collect_ok=1i
# devices: paired Zigbee devices (zigbee2mqtt/bridge/devices, coordinator and
# disabled devices left out). configs: retained Home Assistant discovery
# configs zigbee2mqtt published (node id = a device's IEEE address).
# devices_missing: devices with no retained config at all.
#
# Home Assistant builds its Zigbee entities from those retained configs at
# startup. On 2026-11-18 they were gone from mosquitto (zigbee2mqtt does not
# republish them when Home Assistant comes back), and the next Home Assistant
# restart left 27 lights unavailable until zigbee2mqtt was restarted. Alert:
# z2m-discovery-missing. Fix: docker restart zigbee2mqtt.
#
# Reads as mosquitto user lr4-monitor (read-only; docker/mqtt/acl), password
# in MQTT_MONITOR_PASSWORD. If the broker or zigbee2mqtt's device list can't
# be read, a collect_ok=0i point is written instead of nothing.
sub() {
  docker exec -e PW="$MQTT_MONITOR_PASSWORD" mosquitto sh -c \
    'mosquitto_sub -h 127.0.0.1 -p 1883 -u lr4-monitor -P "$PW" --retained-only -W 3 -v "$@"' sh "$@" 2>/dev/null
}
configs=$(sub -t 'homeassistant/+/+/+/config' | cut -d' ' -f1)
sub -C 1 -t zigbee2mqtt/bridge/devices | CONFIGS="$configs" python3 -I -c '
import json, os, sys
try:
    raw = sys.stdin.read().split(" ", 1)[1]
    devs = {d["ieee_address"] for d in json.loads(raw)
            if d.get("type") != "Coordinator" and not d.get("disabled")}
except Exception:
    print("z2m_discovery collect_ok=0i")
    raise SystemExit(0)
topics = [t for t in os.environ["CONFIGS"].split() if t.count("/") == 4]
nodes = {t.split("/")[2] for t in topics}
configs = sum(1 for t in topics if t.split("/")[2] in devs)
print("z2m_discovery devices=%di,configs=%di,devices_missing=%di,collect_ok=1i"
      % (len(devs), configs, len(devs - nodes)))
'
