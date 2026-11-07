# Homelab infrastructure: current picture

Reviewed standing context, loaded into every session. Source of truth:
`docker/openclaw/workspace/infra/TOOLS.md` in the homelab repo, deployed
read-only by Ansible; change it only through an MR (code-owned by @root).
Last reviewed 2026-11-07. If the repo or the live system disagrees with this
file, they win: say so, and propose the fix to this file in an MR.

## Hosts

- **router** (`router.lab.nkontur.com`): Ubuntu 24.04, amd64. Routes every VLAN
  and runs the main Docker Compose project (`docker`), including GitLab, Vault
  and Home Assistant. ZFS pool `mpool` on LUKS disks; container data under
  `/persistent_data/application/`. Host services: telegraf, NUT (UPS), restic,
  tailscaled, the GitLab runners.
- **zwave** Pi (`zwave.lab.nkontur.com`): Debian 12 (bookworm, since
  2026-11-07). zwave-js-ui, the miniDSP units on USB, snapclient, telegraf,
  promtail.
- **satellite-2** Pi (`satellite-2.lab.nkontur.com`): Debian 12 (bookworm,
  since 2026-11-07). Document scanner, telegraf, promtail. Kernel and
  bootloader follow-ups on both Pis are still in progress.
- **macmini** (`macmini.lab.nkontur.com`): macOS. iMessage bridge.
- Off-box: a healthchecks.io dead-man check (pinged every 5 min by the
  router), restic's offsite repository (Backblaze B2).

## Network (details: docs/NETWORK.md, docs/FIREWALL.md)

- VLANs: 2 external (internet-facing, behind the external nginx), 3 internal
  (lab services, GitLab, Vault, OpenClaw), 4 management (router, switches,
  APs, IPMI, UPS), 5 guest, 6 IoT (Home Assistant, MQTT, Zigbee, Snapcast, AV),
  7 reserved.
- Lab egress goes out through the Mullvad WireGuard tunnel. That is the only
  WireGuard on the router: remote access is Tailscale (subnet router), and the
  old `wg0`/`wg1` tunnels are gone.
- Vaultwarden, Audioserve and Home Assistant answer only to LAN and tailnet
  clients. Nextcloud, Plex, Ombi and the static blog stay public.
- DNS: Pi-hole. NTP: the router (the Aruba S2500 switch syncs from it).
  Switch and AP config and firmware live under `networking/`.

## Services (router compose unless noted)

- Platform: gitlab (and its registry), vault, jit-approval-svc, pihole, three
  nginx proxies (`nginx` external, `lab_nginx`, `iot_nginx`),
  docker-socket-proxy (CI image builds) and `docker-socket-proxy-ro`
  (read-only; promtail discovers containers through it), openclaw-gateway,
  chromium-browser, anomaly-detector.
- Observability: grafana, loki, tempo, otel-collector, promtail, influxdb
  2.9.1 (bucket `metrics`, org `homelab`; 2.9 stores tokens hashed, so a token
  can't be read back later, only recreated), telegraf (host service on the
  router, container on the Pis).
- Home: homeassistant 2026.11.0, zigbee2mqtt, mosquitto, snapserver and
  snapclients, mopidy, ambientweather, amcrest2mqtt.
- Media: plex, radarr, sonarr, lidarr, prowlarr, jackett, nzbget, deluge
  (through Mullvad), ombi, tautulli, navidrome, audioserve.
- Other: nextcloud (MariaDB 12.3 LTS), vaultwarden (service `bitwarden`),
  paperless-ngx and paperless-ai, the Headway maps stack, ollama, drawio.
- **Gone; don't reference or recreate them:** the whole voice stack
  (Rhasspy, Hermes, satellites' voice services), WordPress (the blog is
  static), clawrouter, Portainer.

## Home Assistant facts

- Movie room receiver: `media_player.denon`. The Apple TV input is
  `Apple TV` (it used to be `shield`). The Apple TV is paired:
  `media_player.apple_tv`, `remote.apple_tv`.
- Siri works through the HomeKit bridge "HASS Bridge", advertised on both the
  internal and the IoT network.
- Adaptive Lighting sleep mode follows `input_boolean.night_mode`.
- Automations, scripts and scenes live in the private repo `root/homeassistant`.
  Packages live in this repo (`docker/homeassistant/packages/`).
  See docs/HOMEASSISTANT.md.

## How changes land

- Every change is an MR on `root/homelab` (project 4) to the protected `main`
  branch. The repo is push-mirrored to public GitHub: no secrets, security
  findings, personal or employer details in files, branch names or commit
  messages.
- MR pipelines run validation and `ansible --check --diff` on the unprotected
  runner (bridge network with an egress allowlist, no Docker access).
- A merge to `main` runs `router:deploy`, `zwave:deploy`, `satellite-2:deploy`
  and `macmini:deploy`, scoped to what changed (docs/DEPLOY.md). They run on the
  protected runner, which sits on the `gitlab-ci-protected` bridge behind a
  firewall allowlist, not on the host network. A separate host-network runner
  takes only the switch/AP firmware (TFTP) jobs. The default CI image is
  Ubuntu 24.04.
- CODEOWNERS: `ansible/`, `docker/`, `ci/`, `scripts/`, `networking/`,
  `base/`, `terraform/`, `CLAUDE.md` and the recovery runbooks need @root's
  approval, and so does everything under `docker/openclaw/` (your own config).

### Updates (docs/RENOVATE.md, docs/AUTOMERGE.md)

- **Renovate** runs as a scheduled CI job (hourly; ordinary MRs are created
  01:00-07:00) and opens one MR per image or dependency, labelled `renovate`.
  Majors wait for approval on the "Renovate dependency dashboard" issue.
- **The auto-merge sweep** (`ci/automerge`, every 15 min, one merge per run)
  merges eligible non-major image updates: digest, patch and minor changes to
  digest-pinned `image:` lines in the deployed compose files. Each merge
  deploys only the changed services (`<host>:deploy-service`) behind a health
  gate. A failure rolls back automatically and opens an `automerge-rollback`
  issue and a revert MR. The hold covers only the affected service. Limits:
  24 general merges a day, plus 4 fast-track merges for internet-facing images.
- Never auto-merged: GitLab, Vault, the docker-socket-proxy images (denylist),
  majors, and anything that isn't a compose image line. Home Assistant and
  InfluxDB take digest and patch updates only (making their minors
  auto-mergeable is in progress). Nextcloud and Vaultwarden take digest
  updates only. otel-collector 0.x minors count as minors.
- Labels: `automerge-eligible`, `automerge-manual` (waits for a human),
  `automerged`, `automerge-rollback`, `freeze` (Noah's stop for one MR),
  `security`, `major`.
- **Your part:** don't open routine update MRs (Renovate does), and don't
  merge, approve, rebase or close Renovate MRs, `automerge-revert/*` MRs or
  rollback issues. The `image-update-check` cron researches majors and
  diagnoses failing Renovate MRs; that is all.

## Backups (docs/backups.md)

- `restic-backup.timer`, nightly 03:00, to B2. `restic-db-dumps` (logical
  database dumps) runs first, and a failed dump never blocks the backup.
  Weekly InfluxDB backup Saturday 21:00 (`restic-db-dumps-weekly`). Restic
  check on Sundays, prune on Saturdays, 90-day retention. Separate nightly
  `zwave-backup`, plus ZFS auto-snapshots.
- Restores: docs/backups.md "Restore Procedures". Whole-host recovery:
  docs/DISASTER_RECOVERY.md.

## Monitoring

- Grafana alerts go to email and to you via webhook (skill `anomaly-triage`
  for the anomaly detector). Query Loki only through the Grafana datasource
  proxy (direct access returns 401); its label is `container`.
- The dead-man check (`deadman-check.timer`, docs/DEADMAN.md) tests
  `/persistent_data`, dockerd, key containers, Grafana, Vault unsealed and
  `mpool`, then pings healthchecks.io. If it fails, the alert comes from
  outside the homelab.

## Where things are written down

- Repo `docs/*.md`: NETWORK, FIREWALL, DEPLOY, AUTOMERGE, RENOVATE, backups,
  DISASTER_RECOVERY, DEADMAN, OBSERVABILITY, HOMEASSISTANT, POWER, IPMI,
  SECRET_ROTATION, jit-access-design, denon, minidsp, CONTAINER-REGISTRY.
- The GitLab project wiki (private, not mirrored) holds the security and
  network reviews.
- Your checkout `workspace/homelab` is hard-reset to `origin/main` every
  5 minutes. Read there, but work in a worktree.

## Allowed vs. ask first

Free: read the repo, docs and dashboards; query Grafana, Loki and InfluxDB
with T1 JIT tokens; diagnose; open MRs, issues and comments.

Never without Noah's explicit go-ahead in the current conversation:

- Any lock, door, garage or alarm action (`lock.*`, the garage door openers,
  alarm panels), whoever asks.
- Rebooting the router or power-cycling network gear. Host reboots in general.
- Destructive operations: deleting data or volumes, `docker system prune`,
  `zfs destroy`, force-pushing someone else's branch.
- Credentials: only through JIT (`tools/jit-lib.sh`). T2 resources wait for
  Noah's Telegram approval. Don't work around JIT, and never print a secret.
- Changing your own access, tokens, config or exec policy, or anything under a
  CODEOWNERS path, except by an MR that Noah reviews.
- Freezing or unfreezing auto-merge, or editing `AUTOMERGE_*` variables.

Known open item: homelab issue #149 (`GITLAB_TOKEN` is stripped from
claude-cli sessions by OpenClaw's host-env policy) is waiting on Noah's
decision. Don't change that behaviour. Use `gitlab_token` from
`tools/jit-lib.sh`.

## Merging your own MRs

<!-- PLACEHOLDER: the bot-config auto-merge allowlist is pending. Fill this
     section when it is merged (docs/AUTOMERGE.md). -->
Pending: rules for which of your own MRs may merge without review are being
defined. Until this section says otherwise, don't merge or approve your own
MRs. Open them, and leave them for Noah.
