# Home Assistant helpers (router)

Small helpers root uses on the router to talk to Home Assistant by hand or from
an agent session. Both read HA's long-lived token from Vault
(`homelab/docker/homeassistant`) with the `VAULT_TOKEN` CI variable (through
`glab` in `/root/repos/homelab`), and never print it.

| File | Installed as | Use |
|---|---|---|
| `haapi` | `/usr/local/sbin/haapi`, symlink `/root/haapi` | REST: `haapi GET states/sun.sun`, `haapi POST services/<domain>/<service> '<json>'` |
| `hews` | `/usr/local/sbin/hews`, symlink `/root/hews` | Websocket: `hews '{"type": "config/entity_registry/list"}'`, one JSON line per message |
| `hews.py` | `/usr/local/lib/ha-helpers/hews.py`, symlink `/root/hews.py` | The websocket client `hews` copies into the homeassistant container and runs there |

`haapi` refuses to call HA without a token: unauthenticated requests count as
failed logins, and HA bans the router's address after 5.

Deployed by `configure-homelab-services` (router only). Edit them here, not on
the router: a deploy overwrites the installed copies.
