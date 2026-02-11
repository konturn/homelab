# Infrastructure Map

## Network (VLANs)
| VLAN | Subnet | Purpose |
|------|--------|---------|
| external | 10.2.x.x | Internet-facing (nginx, Nextcloud, Bitwarden) |
| internal | 10.3.x.x | Lab services (GitLab, Plex, Radarr, moltbot) |
| iot | 10.6.x.x | IoT (Home Assistant, Zigbee, MQTT, Snapcast) |
| mgmt | 10.4.x.x | Management (registry, switches) |

## Key Hosts
| Host | IP | Notes |
|------|-----|-------|
| Router | 10.4.0.1 | SSH: `claude@10.4.0.1` via JIT cert, rbash |
| GitLab | 10.3.32.3 | Internal, also external via nginx |
| Vault | 10.3.32.6:8200 | KV v2 at `homelab/` |
| JIT Service | 10.3.32.8:8080 | Also `https://jit.lab.nkontur.com` |
| Home Assistant | 10.6.32.1:8123 | `https://homeassistant.lab.nkontur.com` |
| MQTT Broker | 10.6.32.2:1883 | Mosquitto |
| Snapcast Server | 10.6.32.2 | Multiroom audio |
| InfluxDB | 10.3.x.x:8086 | `https://influxdb.lab.nkontur.com` |
| Grafana | 10.3.x.x:3000 | `https://grafana.lab.nkontur.com` |
| Loki | localhost:3100 | Internal only (after !214 merges) |

## Cameras
| Name | IP | Notes |
|------|-----|-------|
| Doorbell | 10.6.128.9 | Amcrest, amcrest2mqtt bridge |
| Back camera | 10.6.128.14 | |

## AV Equipment
| Device | Address | Notes |
|--------|---------|-------|
| Denon receiver | 10.6.128.3 | |
| Projector | projector.lab.nkontur.com | PJLink |
| Shield TV | 10.6.128.5 | |
| Apple TV | 10.6.128.19 | |
| MiniDSP | zwave.lab.nkontur.com:5380 | Bass boost presets |
| Mopidy | 10.6.32.7 | Music player |

## Snapcast Speakers
office, global, kitchen, main_bedroom, main_bathroom, guest_bedroom, guest_bathroom, movie

## Air Quality Sensors (Awair)
- Kitchen: AWAIR-ELEM-14B541.lab.nkontur.com
- Bedroom: AWAIR-ELEM-147AA0.lab.nkontur.com

## Other Devices
| Device | Address | Notes |
|--------|---------|-------|
| Vacuum | 10.6.128.18 | Roborock |
| Doc scanner | satellite-2.lab.nkontur.com:8080/scan | |
| Weather station | 10.6.128.16 | ambientweather2mqtt |

## Door Sensors (Z-Wave)
Main bathroom, main bedroom, office, guest bedroom
