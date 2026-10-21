# Home Assistant

Home Assistant runs as the `homeassistant` container on the router. Only
`configuration.yaml` is templated from this repo
(`docker/homeassistant/configuration.yaml`). Everything else under
`/persistent_data/application/homeassistant/` (`.storage`, `automations.yaml`,
`scripts.yaml`, `custom_components/`) lives only on the live volume and is
covered by the normal backups, not by git.

## HTTP settings (not in YAML)

Since 2026.10, Home Assistant imports the `http:` YAML block into
`.storage/http` once and ignores YAML from then on (a Repairs warning says YAML
stops working entirely in 2027.2). The block was removed from
`configuration.yaml` because editing it no longer had any effect. The proxy and
ban settings are now changed in *Settings -> System -> Network* and are
covered by the normal `.storage` backups. Expected values:

- `use_x_forwarded_for: true`
- `trusted_proxies`: the `nginx_ip`, `lab_nginx_ip` and `iot_nginx_ip`
  addresses from `ansible/inventory.yml`, plus `127.0.0.1`
- `ip_ban_enabled: true`, `login_attempts_threshold: 5`

If the nginx IPs change in the inventory, update them in the UI too.

## Custom integrations (HACS)

Custom integrations are installed and upgraded through HACS on the live volume,
not from this repo:

| Integration | Source | Notes |
|---|---|---|
| keymaster | `FutureTense/keymaster` | Manages keypad user codes on the front door lock (Z-Wave JS) |
| osrm_travel_time | `edekeijzer/osrm_travel_time` | |
| mopidy | `bushvin/hass-integrations` | Replaces the `mopidy` integration removed from HA core. Still configured by the `media_player: platform: mopidy` block in `configuration.yaml`; the night-mode automation plays white noise on `media_player.mopidy_3` |
| hacs | `hacs/integration` | |

After a Home Assistant upgrade, check *Settings -> System -> Repairs* and the
log for custom integrations that fail to import. Upgrade them in HACS
(respecting each release's minimum HA version in `hacs.json`) and restart.

### keymaster

- keymaster v0.1.0 and later no longer use generated YAML packages or
  `input_*` helpers. Lock and slot settings live in the config entry and in
  the integration's own `.storage` store. Upgrading from v0.0.x migrates the
  config entry automatically (entry version 2 -> 3 -> 4; the last step may
  need a second restart) and deletes the old `packages/keymaster/` folder.
- On first start, keymaster v0.2.0 and later import codes that already exist
  on the lock into empty slots instead of clearing them.
- The config entry must point at the live lock entity
  (`lock.front_door_lock_2`). Check this after re-including the lock in
  Z-Wave.
- Back up `.storage/` before upgrading keymaster. Never commit `.storage`
  contents or lock codes to this repo.
- The admin-only `lock-codes` dashboard uses the `custom:keymaster` Lovelace
  strategy.

## Frontend cards (HACS)

| Card | Source | Used by |
|---|---|---|
| mini-media-player | `kalkih/mini-media-player` | `home_audio_control`, `tablet_dash` |
| layout-card | `thomasloven/lovelace-layout-card` | `tablet_dash` (`custom:grid-layout` view) |

## HomeKit bridge filter

The *HASS Bridge* HomeKit entry includes whole domains, so it goes over
HomeKit's 150-accessory limit as Plex and Snapcast create new `media_player`
entities. The bridge runs in exclude mode with every `media_player.plex_*` and
`media_player.snapcast_*` entity (and `switch.zigbee2mqtt_bridge_permit_join`)
excluded. The options flow only accepts explicit entity lists, so new Plex
clients have to be added to the exclude list in
*Settings -> Devices & services -> HomeKit Bridge -> Configure*.
The speakers, TV, lock and cameras are separate accessory-mode entries.
