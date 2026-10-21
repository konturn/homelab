"""Retained MQTT summary for Home Assistant (MQTT discovery).

- sensor.anomaly_<signal>: ok | moderate | severe, attributes list the
  anomalous series.
- sensor.house_anomalies: number of signals with an active (unsuppressed)
  anomaly.
"""
import json
import logging
import ssl

log = logging.getLogger(__name__)

BASE = "homelab/anomaly"
AVAIL = f"{BASE}/availability"
DEVICE = {"identifiers": ["homelab_anomaly_detector"], "name": "Anomaly Detector",
          "manufacturer": "homelab", "model": "anomaly-detector"}


def discovery_messages(signal_names, prefix="homeassistant"):
    msgs = []
    for name in signal_names:
        cfg = {"name": f"Anomaly {name.replace('_', ' ')}", "unique_id": f"anomaly_{name}",
               "default_entity_id": f"sensor.anomaly_{name}", "state_topic": f"{BASE}/{name}/state",
               "json_attributes_topic": f"{BASE}/{name}/attributes",
               "availability_topic": AVAIL, "icon": "mdi:chart-bell-curve", "device": DEVICE}
        msgs.append((f"{prefix}/sensor/anomaly_detector/{name}/config", json.dumps(cfg)))
    house = {"name": "House anomalies", "unique_id": "anomaly_house_count",
             "default_entity_id": "sensor.house_anomalies", "state_topic": f"{BASE}/house/count",
             "json_attributes_topic": f"{BASE}/house/attributes", "availability_topic": AVAIL,
             "state_class": "measurement", "unit_of_measurement": "signals",
             "icon": "mdi:home-alert", "device": DEVICE}
    msgs.append((f"{prefix}/sensor/anomaly_detector/house/config", json.dumps(house)))
    return msgs


def state_messages(summary):
    """summary: Engine.signal_summary()."""
    msgs, active = [], []
    for name, s in summary.items():
        msgs.append((f"{BASE}/{name}/state", s["severity"]))
        attrs = {"description": s["signal"].description, "category": s["signal"].category,
                 "series": s["series"], "suppressed": s["suppressed"],
                 "active": [{k: a[k] for k in ("check", "label", "severity", "detail", "value", "baseline")}
                            for a in s["active"][:10]]}
        msgs.append((f"{BASE}/{name}/attributes", json.dumps(attrs)))
        if s["severity"] != "ok":
            active.append({"signal": name, "severity": s["severity"]})
    msgs.append((f"{BASE}/house/count", str(len(active))))
    msgs.append((f"{BASE}/house/attributes", json.dumps({"signals": active})))
    return msgs


class Publisher:
    def __init__(self, host, port, username, password, tls=True, client_id="anomaly-detector"):
        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.client.will_set(AVAIL, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.connected = False
        self.discovery = []
        self.last_error = None
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = not reason_code.is_failure
        if self.connected:
            client.publish(AVAIL, "online", qos=1, retain=True)
            for topic, payload in self.discovery:
                client.publish(topic, payload, qos=1, retain=True)
            self.last_error = None
        else:
            self.last_error = f"connect failed: {reason_code}"
            log.error("mqtt %s", self.last_error)

    def set_discovery(self, msgs):
        self.discovery = msgs
        if self.connected:
            for topic, payload in msgs:
                self.client.publish(topic, payload, qos=1, retain=True)

    def publish(self, msgs):
        if not self.client.is_connected():
            self.connected = False
            return False
        for topic, payload in msgs:
            self.client.publish(topic, payload, qos=1, retain=True)
        return True

    def close(self):
        try:
            self.client.publish(AVAIL, "offline", qos=1, retain=True).wait_for_publish(2)
        except Exception:
            pass
        self.client.loop_stop()
        self.client.disconnect()
