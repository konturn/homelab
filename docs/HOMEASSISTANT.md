# Home Assistant

Home Assistant runs as the `homeassistant` container on the router. Only
`configuration.yaml` is templated from this repo
(`docker/homeassistant/configuration.yaml`). Everything else under
`/persistent_data/application/homeassistant/` (`.storage`, `automations.yaml`,
`scripts.yaml`, `custom_components/`) lives only on the live volume and is
covered by the normal backups, not by git.

## Custom integrations (HACS)

Custom integrations are installed and upgraded through HACS on the live volume,
not from this repo:

| Integration | Source | Notes |
|---|---|---|
| keymaster | `FutureTense/keymaster` | Manages keypad user codes on the front door lock (Z-Wave JS) |
| osrm_travel_time | `edekeijzer/osrm_travel_time` | |
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
