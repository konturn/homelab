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

## ACL

`docker/mqtt/acl`, rendered by the deploy to
`/persistent_data/application/mqtt/conf/acl` (uid 1883, 0600) and loaded
through `acl_file` in `mosquitto.conf`. Deny by default: a user can read or
write only what is listed under it, and a user missing from the file can do
nothing. A change to the file restarts mosquitto (every client reconnects
within seconds); a `SIGHUP` also reloads it.

| User | Reads | Writes |
|---|---|---|
| `mosquitto` (Home Assistant) | `#` | `homeassistant/status`; command topics: `zigbee2mqtt/+/set`, `zigbee2mqtt/+/set/+`, `zigbee2mqtt/+/get`, `zigbee2mqtt/+/get/+`, `zigbee2mqtt/bridge/request/#`, `frigate/+/set`, `frigate/+/+/set`, `frigate/+/ptz`, `homeconnect/<control device>/{set,activeProgram,selectedProgram}`, `prod/LR4/+/command`; for Valetudo (same user) `valetudo/#` and `homeassistant/+/Main/#` |
| `zigbee2mqtt` | `zigbee2mqtt/#`, `homeassistant/#` | `zigbee2mqtt/#`, `homeassistant/#` (one discovery node per device) |
| `ambientweather` | nothing | `homeassistant/<station MAC>`, `homeassistant/+/<station MAC>/#` (`weather_station_mac`) |
| `amcrest2mqtt` | nothing | `amcrest2mqtt/#`, `homeassistant/#` (discovery node is the doorbell's serial) |
| `frigate` | `frigate/#` | `frigate/#` |
| `hcpy` | `homeconnect/<control device>/{set,activeProgram,selectedProgram}` | `homeconnect/LWT`, `homeconnect/+/LWT`, `homeconnect/+/state/#`, `homeconnect/+/event/#`, `homeassistant/+/hcpy/#` |
| `airplay` | nothing | `shairport/#`, `homeassistant/+/shairport-sync_<receiver MAC>/#` (from `airplay_receivers`) |
| `anomaly-detector` | nothing | `homelab/anomaly/#`, `homeassistant/+/anomaly_detector/#` |
| `lr4-bridge` | `prod/LR4/+/command` | `prod/LR4/+/state`, `prod/LR4/+/activity`, `$SYS/broker/connection/lr4-bridge/state` |
| `lr4-monitor` | `prod/LR4/+/state`, `prod/LR4/+/activity`, `$SYS/broker/connection/lr4-bridge/state` | nothing |

`<control device>` is each entry of `hcpy_control_devices` (role defaults;
today only `hood`).

Rules the file keeps:

- Home Assistant is the integrator: it reads everything and is the only
  user that publishes command topics of other services' devices. It does not
  publish their state.
- hcpy command topics (`set`, `activeProgram`, `selectedProgram`) exist only
  for `hcpy_control_devices`; only Home Assistant may publish them and only
  hcpy reads them. The oven/range is never in that list (the deploy asserts
  it), so no user can publish a command to it.
- A service writes only its own tree and its own Home Assistant discovery
  node. zigbee2mqtt and amcrest2mqtt have the whole discovery prefix because
  their node ids (Zigbee IEEE addresses, the doorbell serial) cannot be
  listed in the repo.
- `#` and `+` never match `$SYS/...`; the bridge-state topic is granted by
  name.

Mosquitto checks a subscription filter and then every message delivered
through it, so a wildcard subscription is allowed but only delivers what the
user may read.

### Adding or changing a client

1. Give it its own user (above).
2. Add a `user` block to `docker/mqtt/acl` with exactly the topics it
   publishes and subscribes to; check them against a capture
   (`mosquitto_sub -t '#' -v` as Home Assistant's user) or the client's
   source.
3. If Home Assistant must command it, add the command topic pattern under
   `user mosquitto`.
4. Deploy, then look for denials (below).

### Finding denials

At the default log level mosquitto does not log denials. With `log_type
debug` it logs each denied publish (`Denied PUBLISH from <client> ...
'<topic>'`); a subscription outside a user's grants is not logged, it just
delivers nothing, so check the subscriber's entities too. To look, add
`log_type` lines for `error`, `warning`, `notice`, `information` and `debug`
to the rendered `mosquitto.conf` (any `log_type` replaces the default set),
`docker kill -s HUP mosquitto`, and
`docker logs -f mosquitto 2>&1 | grep -i denied`; the next deploy (or removing
the line and another HUP) turns it off again. The client is not told about a
denied publish.

### Rollback

Remove the `acl_file` line from the rendered
`/persistent_data/application/mqtt/conf/mosquitto.conf` and
`docker restart mosquitto` (everything is then allowed to every
authenticated user again), then revert the change in the repo before the
next deploy.
