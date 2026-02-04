---
name: home-assistant
description: Control Home Assistant smart home devices, run automations, scenes, and scripts. Supports lights, switches, climate, covers, media players, and sensors.
---

# Home Assistant

Control smart home via Home Assistant's REST API.

## Configuration

Environment variables (already configured):
- `HASS_URL`: Use `https://homeassistant.lab.nkontur.com` (external URL)
- `HASS_TOKEN`: Long-lived access token

**Note:** The env var is `HASS_TOKEN` (not `HA_TOKEN`). Use the external URL, not the internal Docker URL.

## Quick Reference

### List Entities by Domain

```bash
# All entities
curl -s -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/states" | jq '.[].entity_id'

# Filter by domain
curl -s -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/states" | \
  jq -r '.[] | select(.entity_id | startswith("light.")) | .entity_id'

# With state
curl -s -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/states" | \
  jq -r '.[] | select(.entity_id | startswith("switch.")) | "\(.entity_id): \(.state)"'
```

### Get Entity State

```bash
curl -s -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/states/{entity_id}"
```

### Search Entities

```bash
curl -s -H "Authorization: Bearer $HASS_TOKEN" "$HASS_URL/api/states" | \
  jq -r '.[] | select(.entity_id | contains("kitchen")) | .entity_id'
```

## Control Devices

### Lights

```bash
# Turn on
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/light/turn_on" -d '{"entity_id": "light.living_room"}'

# Turn on with brightness (0-255)
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/light/turn_on" -d '{"entity_id": "light.living_room", "brightness": 200}'

# Turn on with brightness percent
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/light/turn_on" -d '{"entity_id": "light.living_room", "brightness_pct": 80}'

# Turn off
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/light/turn_off" -d '{"entity_id": "light.living_room"}'

# Toggle
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/light/toggle" -d '{"entity_id": "light.living_room"}'
```

### Switches

```bash
# Turn on
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/switch/turn_on" -d '{"entity_id": "switch.office_fan"}'

# Turn off
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/switch/turn_off" -d '{"entity_id": "switch.office_fan"}'
```

### Scenes

```bash
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/scene/turn_on" -d '{"entity_id": "scene.movie_time"}'
```

### Scripts

```bash
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/script/turn_on" -d '{"entity_id": "script.goodnight"}'
```

### Automations

```bash
# Trigger automation
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/automation/trigger" -d '{"entity_id": "automation.motion_lights"}'

# Enable/disable automation
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/automation/turn_on" -d '{"entity_id": "automation.motion_lights"}'
```

### Climate (Thermostat)

```bash
# Set temperature
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/climate/set_temperature" \
  -d '{"entity_id": "climate.thermostat", "temperature": 22}'

# Set HVAC mode (heat, cool, auto, off)
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/climate/set_hvac_mode" \
  -d '{"entity_id": "climate.thermostat", "hvac_mode": "heat"}'
```

### Covers (Blinds, Garage)

```bash
# Open/close/stop
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/cover/open_cover" -d '{"entity_id": "cover.garage"}'

curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/cover/close_cover" -d '{"entity_id": "cover.garage"}'
```

### Media Players

```bash
# Play/pause
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/media_player/media_play_pause" -d '{"entity_id": "media_player.living_room_tv"}'

# Volume (0.0-1.0)
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/media_player/volume_set" \
  -d '{"entity_id": "media_player.living_room_tv", "volume_level": 0.5}'
```

## Call Any Service

```bash
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  "$HASS_URL/api/services/{domain}/{service}" -d '{"entity_id": "...", ...}'
```

## Common Services Reference

| Domain | Services | Notes |
|--------|----------|-------|
| `light` | turn_on, turn_off, toggle | brightness, color_temp, rgb_color |
| `switch` | turn_on, turn_off, toggle | |
| `scene` | turn_on | |
| `script` | turn_on, turn_off | |
| `automation` | trigger, turn_on, turn_off | |
| `climate` | set_temperature, set_hvac_mode | temperature, hvac_mode |
| `cover` | open_cover, close_cover, stop_cover | |
| `media_player` | media_play_pause, volume_set, turn_on/off | volume_level |
| `lock` | lock, unlock | |
| `fan` | turn_on, turn_off, set_percentage | percentage |
| `vacuum` | start, stop, return_to_base | |

## Noah's Setup - Quick Reference

**Door Sensors (Z-Wave):**
- `binary_sensor.main_bathroom_door`
- `binary_sensor.main_bedroom_door`
- `binary_sensor.office_door`
- `binary_sensor.guest_bedroom_door`

**Locks:**
- `lock.front_door_lock` (check `sensor.front_door_lock_battery_level_2`)

**AV Equipment:**
- Denon receiver: 10.6.128.3
- Projector: projector.lab.nkontur.com
- Apple TV: 10.6.128.19

**Snapcast Speakers:** See TOOLS.md for room list

## Troubleshooting

- **401 Unauthorized**: Token expired or invalid. Get new one from HA Profile.
- **Connection refused**: Check URL, ensure HA is running.
- **Entity not found**: List entities to verify entity_id.
- **No response**: Some services don't return data (just 200 OK).

## Notes

- Long-lived tokens don't expire — store securely
- API returns JSON
- Service calls return empty `[]` on success
- Always verify entity_id exists before calling services
