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
| samsungtv_smart | `TheFab21/ha-samsungtv-smart` | Samsung Frame TV (local only). See below |

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

### samsungtv_smart (Frame TV)

- Installed from HACS (`TheFab21/ha-samsungtv-smart`, which adds
  Frame Art Mode services). Configured
  local only: no SmartThings OAuth and no keys for the optional art
  identification (Google Vision / LLM) features.
- Entities: `media_player.frame_tv`, `switch.frame_tv_art_mode`,
  `select.frame_tv_matte_type`, `select.frame_tv_matte_color` and
  `sensor.frame_tv_frame_art` (current artwork, matte and slideshow state as
  attributes).
- Art is managed with the `samsungtv_smart.art_*` services. `art_upload_batch`
  takes a folder Home Assistant can read (for example
  `/config/www/frame/<album>/`) and records what it uploaded in a hidden
  `.samsungtv_upload.json` sidecar, so re-running it only uploads new files.
  Matte ids are `type_color` (e.g. `shadowbox_black`); `art_get_matte_list`
  shows what the TV supports.
- The TV's own slideshow (`art_set_slideshow`) can only target a whole
  category (2 = My Photos, 4 = Favorites, 8 = Store), not a list of images, and
  this TV refuses to favourite uploaded photos. A rotation over a subset of
  uploads therefore has to be driven from Home Assistant with
  `art_select_image` (`wake: false` so it never powers the TV on).
- Scripts (UI script storage, `scripts.yaml` on the live volume):
  - `script.frame_halloween_party_on`: Art Mode on, TV slideshow off, shows the
    first image of the Halloween album, starts `script.frame_halloween_rotate`.
  - `script.frame_halloween_rotate`: helper loop, a random album image every
    3 minutes. It stops on an HA restart; run the "on" script again.
  - `script.frame_halloween_party_off`: stops the loop and the TV slideshow
    and shows the Art Store piece that was on before.
  The album images are public-domain scans kept only on the live volume
  (`www/frame/halloween/`, with a `manifest.csv` of sources and licences),
  not in git.

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

## Denon receiver

`media_player.denon` comes from the `denonavr` integration (telnet enabled).
The *denon* HomeKit entry (accessory mode, port 21077) includes only
`media_player.denon`. It used to point at `media_player.home_theater`, which
no longer exists, and was repointed in the options flow on 2026-10-28. If the
integration is stuck in `setup_retry` with a 403 on `description.xml`, the
receiver needs a mains power-cycle; see docs/denon.md, which also covers the
settings export and drift check.
