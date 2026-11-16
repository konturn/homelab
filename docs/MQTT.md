# MQTT (mosquitto)

The main broker is the `mosquitto` container on the router (IoT VLAN,
`mqtt.lab.nkontur.com`). TLS on 8883 for every client, plain 1883 on the
container's loopback only. Anonymous access is off. Config:
`docker/mqtt/mosquitto.conf`, rendered to
`/persistent_data/application/mqtt/conf/`.

The Litter-Robots have their own broker, `lr4-broker`, bridged into this one
(docs/litter-robot.md).

## Users

Every client has its own user, so the broker can tell them apart.
`mosquitto.passwd` is host state: the deploy adds a missing user with the
password from Vault and never changes or removes other lines. Rotation:
docs/SECRET_ROTATION.md, "MQTT passwords".

| User | Client | Password in Vault | Added by |
|---|---|---|---|
| `mosquitto` | Home Assistant (MQTT integration, set in HA's UI); Valetudo on the vacuum (configured on the robot) | `homelab/mqtt/mosquitto` | by hand (the original user) |
| `zigbee2mqtt` | zigbee2mqtt | `homelab/mqtt/zigbee2mqtt` | configure-homelab-services |
| `ambientweather` | ambientweather (weather station) | `homelab/mqtt/ambientweather` | configure-homelab-services |
| `amcrest2mqtt` | amcrest2mqtt (doorbell) | `homelab/mqtt/amcrest2mqtt` | configure-homelab-services |
| `frigate` | Frigate | `homelab/docker/frigate` | frigate_post.yml |
| `hcpy` | hcpy (Thermador bridge) | `homelab/docker/hcpy` | hcpy_post.yml |
| `airplay` | the shairport-sync AirPlay receivers | `homelab/docker/airplay` | configure-homelab-services |
| `anomaly-detector` | anomaly-detector | `homelab/docker/anomaly-detector` | configure-homelab-services |
| `lr4-bridge` | lr4-broker's bridge | `homelab/docker/lr4-broker` | configure-homelab-services |
| `lr4-monitor` | telegraf's Litter-Robot heartbeat, `lr4ctl status` | `homelab/docker/lr4-broker` | configure-homelab-services |

Telegraf (apart from `lr4-monitor`), event-router, zwave-js and the cameras do
not use MQTT.

A new client gets a new user: password in Vault, read by fetch-vault-secrets,
added by a deploy task like the ones above, and a row here.
