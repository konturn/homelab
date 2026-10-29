# Home Assistant

Home Assistant runs as the `homeassistant` container on the router. Two
things are templated from this repo: `configuration.yaml`
(`docker/homeassistant/configuration.yaml`) and the packages in
`docker/homeassistant/packages/` (to `/config/packages/`). Automations,
scripts, scenes and blueprints live in a separate private repo (below).
Everything else under `/persistent_data/application/homeassistant/`
(`.storage`, `custom_components/`, `www/`) lives only on the live volume and
is covered by the normal backups, not by git.

## Automations, scripts and scenes (separate private repo)

The automations, scripts, scenes and the blueprints they use are kept in a
separate private GitLab project, `root/homeassistant`. It has no mirror, so
they never reach the public mirror of this repo. Its README has the file
conventions and the CI details. This repo provides the include lines and
the deploy plumbing:

- `configuration.yaml` loads two sources per domain:

  ```yaml
  automation manual: !include_dir_merge_list automations/
  automation ui: !include automations.yaml
  script manual: !include_dir_merge_named scripts/
  script ui: !include scripts.yaml
  scene manual: !include_dir_merge_list scenes/
  scene ui: !include scenes.yaml
  ```

  The directories (one file per item) are written only by the private
  repo's deploy. The UI files are where the UI editors save. Repo-managed
  items open read-only in the editors; new items made in the UI go to the UI
  files, and a scheduled job in the private repo turns them into an MR there.
  After that MR merges, the deploy moves them out of the UI files, so an id is
  never loaded twice. Turning automations on/off, running them and traces
  work for every item.
- Deploy target: the `ha-config-sync` role (`ansible/roles/ha-config-sync/`)
  creates the three directories, the `ha-config` user and
  `/usr/local/sbin/ha-config-sync`. CI in the private repo logs in to Vault
  with its GitLab `id_token` (JWT role `ha-config-deploy`,
  `terraform/vault/roles.tf`). It gets a 5-minute SSH certificate (SSH role
  `ha-config-deploy`) whose only possible command is `ha-config-sync`, pipes
  the files to it, and then calls `automation.reload`, `script.reload` and
  `scene.reload` over the API. It never restarts Home Assistant. Pre-deploy
  archives are kept in `/var/backups/ha-config-sync/` on the router.
- CI tokens: only the protected `main` jobs (`ha-config-deploy`) read
  `homelab/docker/homeassistant`. MR and branch pipelines get no Vault role
  or Home Assistant token; lint reads a data snapshot that `main` publishes.
- `ha-config-sync` refuses an id that is also defined in `/config/packages/`.
  Packages stay in this repo. To move a package automation to the private
  repo, delete it here and add it there in the same change window.
- Don't edit the managed directories on the volume by hand: the next deploy
  overwrites them, and the drift job reports the difference.
- By hand: `sudo ha-config-sync status` (item counts per source) on the
  router.

## Packages (repo-managed automations)

Automations that must not drift, or that belong with repo-managed REST
commands and sensors, go in a package instead:

- `configuration.yaml` has `homeassistant: packages: !include_dir_named
  packages`. Each file in `/config/packages/` is one package, named after the
  file, and can hold any mix of integrations (`automation:`, `script:`,
  `template:`, `rest_command:` ...), which Home Assistant merges with the main
  config.
- To add one: create `docker/homeassistant/packages/<name>.yaml` and add a
  `docker_config` entry for it (next to `configuration.yaml`, `name:
  "homeassistant"`) in `ansible/inventory.yml`. The file is rendered by
  Ansible like every other config file, so Home Assistant templates go inside
  `{% raw %}` blocks.
- A changed package restarts Home Assistant on deploy, like
  `configuration.yaml`.
- Package automations and scripts show in the UI but cannot be edited there
  ("not stored in automations.yaml"). Change them by MR. Give each automation
  an `id` so its entity, history and traces are kept.
- Moving a UI automation into a package: keep its `id`, back up
  `automations.yaml` on the volume, and delete the old copy from it, or the
  id is defined twice.
- Check a change before merging: copy the live config to a scratch directory
  under `/config`, add the rendered package, and run
  `docker exec homeassistant python3 -m homeassistant --script check_config -c <dir>`
  (`-i automation` etc. prints what was parsed). The only expected warning is
  `spotcast` not found, which the live config has too.

## miniDSP (bass boost)

The two miniDSP 2x4HD units on the Z-Wave Pi are described in
`docs/minidsp.md`, including the rule that nothing may write to them except
the existing bass-boost automation.

- `rest_command.bass_boost` and `rest_command.bass_boost_2` set the preset on
  units 919619 and 921284 (`/devices/<serial>/config`, addressed by serial so
  USB re-enumeration cannot swap them): 0 = normal when
  `input_boolean.bass_boost` is off, 1 = bass boost when it is on.
- Everything that calls them is in the package
  `docker/homeassistant/packages/minidsp_bass_boost.yaml`: the automation
  "Change Minidsp on base boost" (same id as the old UI automation), the sync
  check, the mismatch sensors and the scripts that do the write and read-back.
  See docs/minidsp.md for how they work.
- Read-only status per unit: `sensor.minidsp_<serial>_preset`, `_source`,
  `_volume` and `binary_sensor.minidsp_<serial>_mute`. Levels are in InfluxDB
  (`metrics` bucket, measurement `minidsp`).

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
- Scripts (in the private config repo, see above):
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
