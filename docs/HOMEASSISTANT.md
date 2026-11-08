# Home Assistant

Home Assistant runs as the `homeassistant` container on the router. Two
things are templated from this repo: `configuration.yaml`
(`docker/homeassistant/configuration.yaml`) and the packages in
`docker/homeassistant/packages/` (to `/config/packages/`). Automations,
scripts, scenes and blueprints live in a separate private repo (below).
Everything else under `/persistent_data/application/homeassistant/`
(`.storage`, `custom_components/`, `www/`) lives only on the live volume and
is covered by the normal backups, not by git. The exception is the custom
integrations pinned from private forks, which the deploy syncs into
`custom_components/` (see "Custom integrations").

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
  (`-i automation` etc. prints what was parsed). It should report no errors
  or warnings.

## Secrets

`configuration.yaml` and the packages contain no credentials, and the deploy
renders nothing from Vault into `/config`. Keep it that way: this repo is
public-mirrored, and everything in `/config` ends up in Home Assistant's own
backups and is readable by anything that reads the config.

- **InfluxDB.** Since the `influxdb` integration got a config flow, Home
  Assistant imports the YAML connection settings into a config entry once
  and ignores them afterwards (single config entry). The connection (URL
  `http://influxdb:8086`, token, org `homelab`, bucket `homeassistant`) is
  therefore only in that config entry (`.storage/core.config_entries`). The
  `influxdb:` block in `configuration.yaml` keeps just the `include` filter,
  which the integration still reads from YAML. The token is an InfluxDB
  authorization with read and write on the `homeassistant` bucket only; Vault
  `homelab/influxdb/ha-write` (field `token`) holds the current value as the
  record. Ansible does not deploy it.
- **Rotating the InfluxDB token.** InfluxDB stores tokens hashed, so take the
  value when it is created:
  1. `influx auth create --org homelab --read-bucket <id> --write-bucket <id>
     --description "Home Assistant"` (bucket id from `influx bucket list`),
     with the output piped straight into `vault kv patch
     homelab/influxdb/ha-write token=-`, never printed.
  2. Put the new token in the config entry: *Settings -> Devices & services
     -> InfluxDB -> Reconfigure*, or the same flow over the API
     (`POST /api/config/config_entries/flow` with `{"handler": "influxdb",
     "entry_id": "<id>"}`, then submit `url`, `token`, `organization`,
     `bucket`, `verify_ssl`). The flow tests the connection before saving and
     reloads the integration; no restart is needed.
  3. Check that points still arrive in the `homeassistant` bucket, then
     `influx auth delete --id <old id>`.
- **Rebuilding from scratch** (lost `.storage`): add the InfluxDB integration
  in the UI (API v2) with the values above and the token from Vault.
- **Other integrations** with credentials (Frigate, Spotify, Plex, the
  Marketplace, mobile apps, ...) were set up in the UI and keep them in their
  config entries in `.storage`, which Home Assistant manages.
- **A YAML-only integration that needs a secret:** reference it with
  `!env_var NAME` and add the value to the `homeassistant` container's
  `environment:` in `docker/docker-compose.yml` from a Vault variable
  (`fetch-vault-secrets`), as other services do. Use `!secret` with a
  `secrets.yaml` rendered `0600` only if the integration rejects the
  environment form. Never template a value inline into the YAML.
- **spotcast** was removed on 2026-11-07. The custom integration was not
  installed (it was not in `custom_components/`), so its `spotcast:` block
  and the Vault secret `infrastructure/spotify` were deleted. The
  `shower_wallmote` automation and the `activate_shower_sound` script in the
  private config repo still call `spotcast.start` and fail at that step.

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

## Custom integrations

Custom integrations live in `custom_components/` on the live volume, not in
this repo. All but Frigate, toyota_connected and miele_lan are installed and
upgraded through the Marketplace (the core `marketplace` integration, which replaced
HACS in 2026.11):

| Integration | Source | Notes |
|---|---|---|
| keymaster | `FutureTense/keymaster` | Manages keypad user codes on the front door lock (Z-Wave JS) |
| osrm_travel_time | `edekeijzer/osrm_travel_time` | |
| mopidy | `bushvin/hass-integrations` | Replaces the `mopidy` integration removed from HA core. Still configured by the `media_player: platform: mopidy` block in `configuration.yaml`; the night-mode automation plays white noise on `media_player.mopidy_3` |
| samsungtv_smart | `TheFab21/ha-samsungtv-smart` | Samsung Frame TV (local only). See below |
| adaptive_lighting | `basnijholt/adaptive-lighting` | Entry *Hue Lights* |
| frigate | `blakeblackshear/frigate-hass-integration` | **Not from the Marketplace**: copied by hand into `custom_components/frigate/` (v5.15.6), so the Marketplace does not track or upgrade it. See docs/frigate.md |
| toyota_connected | private fork `root/ha-toyota-connected` (GitLab) | **Not from the Marketplace**: installed by the deploy, pinned to a commit. See below |
| miele_lan | private fork `root/ha-miele-at-lan` (GitLab) of `tiehfood/ha-miele-at-lan` | **Not from the Marketplace**: pinned like toyota_connected. See "Miele" |

So there are three ways a custom integration gets into `custom_components/`:

- **Marketplace** (default): installed and upgraded in the UI; the
  Marketplace tracks the version and shows update notices.
- **Pinned from a private fork** (toyota_connected, miele_lan): listed in
  `homeassistant_pinned_integrations` and synced by every deploy (below).
  Use this for an integration that needs local patches or should only change
  through a reviewed MR.
- **By hand** (frigate): a release's directory copied onto the volume. Nothing
  tracks or upgrades it, and nothing restores it except the volume backups.
  Prefer one of the two above for anything new.

### Pinned integrations from private forks

`homeassistant_pinned_integrations` (`configure-homelab-services` defaults)
lists integrations installed from a private GitLab fork at a full commit id.
Each deploy fetches that commit with a read-only deploy token (Vault
`gitlab/ha-toyota-connected`), checks the id and the manifest domain, and
syncs the integration directory into `custom_components/<domain>/` with
`--delete`. Each entry has its own deploy token (Vault
`gitlab/<project>`). Home Assistant is restarted (once, with any config change) only
when files changed. Bumps are manual: review the fork, tag it, and change
`sha`; Renovate does not track it. Don't edit these directories on the
volume: the next deploy reverts them.

After a Home Assistant upgrade, check *Settings -> System -> Repairs* and the
log for custom integrations that fail to import. Upgrade Marketplace ones in
the Marketplace (respecting each release's minimum HA version in `hacs.json`),
pinned ones by bumping `sha`, and Frigate by hand (docs/frigate.md), then
restart.

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

- Installed from the Marketplace (`TheFab21/ha-samsungtv-smart`, which adds
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

## Miele (Miele@LAN, local)

The Miele appliance is read over the LAN by `miele_lan` (Miele@LAN), pinned
from the private fork `root/ha-miele-at-lan` (see "Custom integrations").
It signs requests with the household GroupKey and gets state changes pushed
by the appliance (SuperVision), with slow polling as a fallback. The cloud
`miele` integration is not used.

- **Setup (once, in the UI):** *Settings -> Devices & services -> Add
  Integration -> Miele@LAN -> Cloud pairing*, country `us`. The flow shows a
  Miele login URL. Open it in desktop Chrome/Edge with DevTools -> Network
  open and *Preserve log* on, log in, then copy the blocked
  `miele://oauth2-code/?code=...` request (right-click -> Copy link address)
  and paste it into the flow. The integration fetches the GroupKey once and
  stores it in its config entry (`.storage`). A Miele `503 Service
  Temporarily Unavailable` during login is an outage on Miele's side: retry
  later.
- **LAN-only afterwards:** the fork stores no refresh token, so nothing talks
  to the Miele cloud after setup. Pairing adds HA as a SuperVision peer and
  push subscriber on the appliance; it does not re-commission the appliance,
  so the Miele app keeps working. Removing the integration deletes those
  entries again.
- **Network:** discovery and push need mDNS (`_mieleathome._tcp`) between HA
  and the appliance, which HA's IoT leg (`iot0`) provides. HA listens for the
  push on port 18082.
- **Controls:** start/stop buttons, power switch, light and fan are disabled
  by default (fork patch). Status, program, phase and times are enabled.
- **Do not** use MieleRESTServer or `tools/miele_lan_provision.py`: they
  re-commission the appliance, which breaks its cloud pairing and the app.

## Thermador (Home Connect, local via hcpy)

The Thermador hood and range are read locally by **hcpy** (`hc2mqtt`), a
bridge from the Home Connect local protocol to MQTT, with Home Assistant MQTT
discovery. It runs as the `hcpy` container, built on the router from the
pinned private fork `root/hcpy` (`hcpy_source` in the
`configure-homelab-services` defaults; `tasks/hcpy.yml`).

- **Network:** internal VLAN at `hcpy_ip`. FORWARD rules allow only the hood
  and range (`thermador_*_address`, fixed DHCP, TCP 80) and mosquitto
  (8883); everything else it sends through the router is refused
  (docs/FIREWALL.md). MQTT user `hcpy` (Vault `docker/hcpy`), TLS verified.
- **Keys:** each appliance has a key/iv fetched once from the Home Connect
  cloud. They are in `/persistent_data/application/hcpy/devices.json`
  (0400, the bridge's uid 10001, mounted read-only), never in git.
- **One-time login:** `ssh -t root@10.4.0.1 hc-login` (installed by the
  deploy). It prints a Home Connect URL: open it in desktop Chrome with
  DevTools -> Network open, log in, find the blocked request starting
  `hcauth://auth/prod?code=...`, copy its URL and paste it at the prompt.
  The login runs from the router's own network (WAN). It writes the devices
  file, sets each appliance's address and restarts the bridge. Run it again
  after an appliance is re-paired in the Home Connect app.
- **Read-only:** `hcpy_control_devices` lists the devices that accept
  commands (the hood). The range is never on it: the bridge does not
  subscribe to its command topics, refuses commands for it, and publishes
  its writable settings as read-only sensors. The hood's control entities
  exist but are disabled by default.
- **Entities:** device names come from the account (`hood`, `oven`, ...);
  entities are `*.<name>_<feature>` (for example
  `sensor.hood_bsh_common_status_operationstate`). Availability follows the
  bridge and per-appliance LWT topics under `homeconnect/`.
- The cloud `home_connect` config entry is kept but disabled; re-enable it
  only if the local path is removed.

## Frontend cards (Marketplace)

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

Siri reaches these entries over HomeKit, so they are advertised on the
container's internal and IoT legs (*Settings -> System -> Network*, network
adapters). The setting stores interface names, and Docker attaches HA's three
networks in no fixed order (ethN changed on recreates and plain restarts), so
`docker/docker-compose.yml` pins the names with `interface_name`: `int0`
(internal), `iot0` (IoT), `ext0` (external). Each deploy sets the selection
to `homeassistant_network_adapters` (`ansible/inventory.yml`, `int0` and
`iot0`) over the websocket API (`network/configure`) and restarts HA when it
changed (`configure-homelab-services`, `homeassistant_post.yml`). Don't
change the selection in the UI; change the inventory list. `facts:drift`
reports a container interface name or the selection that differs.

## Denon receiver

`media_player.denon` comes from the `denonavr` integration (telnet enabled).
The *denon* HomeKit entry (accessory mode, port 21077) includes only
`media_player.denon`. It used to point at `media_player.home_theater`, which
no longer exists, and was repointed in the options flow on 2026-10-28. If the
integration is stuck in `setup_retry` with a 403 on `description.xml`, the
receiver needs a mains power-cycle; see docs/denon.md, which also covers the
settings export and drift check. The movie-room Apple TV is paired through the
`apple_tv` integration (`media_player.apple_tv`, `remote.apple_tv`).

## AirPlay receivers

The AirPlay receivers (one per audio zone, docs/AUDIO.md) publish what they
play over MQTT and announce themselves through MQTT discovery: one device per
receiver, `shairport-sync_<device ID>`, with sensors for artist, album,
title, format, client name and IP. Raw state is under `shairport/<zone>/`
(`playing` and `active` are `1`/`0`). They connect as MQTT user `airplay`
(Vault `homelab/docker/airplay`, created by the deploy like the other MQTT
users).

They can also be added to the Home app as speakers, which makes them Siri
targets by room; the steps are in docs/AUDIO.md ("Home app and Siri").
