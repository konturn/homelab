# Homelab Infrastructure Project

My access to Noah's GitLab homelab repository and ability to make infrastructure improvements via MRs.

## Access
- **GitLab:** gitlab.lab.nkontur.com (user: moltbot, **Developer** role — level 30, NOT Maintainer)
- **Repo:** root/homelab (project ID: 4)
- **Token:** `$GITLAB_TOKEN` via JIT (T2, requires approval) — env var removed, use `jit_get gitlab`
- **Workflow:** Push → GitLab CI → Ansible deploys to router
- **Self-merge policy:** Small config tweaks = self-merge. Big architectural changes = Noah's review.
- **GitLab Ultimate:** License active (spoofed), CODEOWNERS enforcement working.
- **Custom role:** Developer + admin_terraform_state (fixes vault:validate without Maintainer)

## JIT Privileged Access System (Feb 2026)
Full JIT credential management system built and operational:
- **T1 (auto-approve, 15min):** grafana, influxdb, plex, radarr, sonarr, ombi, nzbget, deluge, paperless, prowlarr, mqtt, gmail-read
- **T2 (approval, 30min):** gitlab, homeassistant, vault, tailscale, pihole, ipmi, gmail-send, ssh
- **Dynamic backends:** Grafana (ephemeral SA tokens), InfluxDB (ephemeral tokens), GitLab (scoped PATs), Vault (inline scoped policies), Gmail (OAuth2 refresh)
- **SSH certificates:** Vault CA signs ephemeral ed25519 keys, principal: claude, 15min TTL
- **Client-side caching:** jit-lib.sh caches credentials in /tmp/jit-cache, 344ms→70ms on hit
- **Webhook:** JIT service pushes Telegram notifications for approval

## Security Hardening (Feb 2026)
- Docker socket proxy isolated from moltbot (MR !231) — removed from mgmt network
- Docker proxy TLS + mTLS via lab_nginx (MR !236)
- Consolidated to single gitlab-runner on host network (MR !237)
- cap_drop: ALL + explicit cap_add on all containers (MR !206/!207/!228)
- CODEOWNERS with /ansible/ @root wildcard (MR !252)
- Vault audit logging → Promtail → Loki (MR !171)
- moltbot-ops Vault policy scoped down to agents/* and moltbot/* only (MR !192)
- CI/CD VAULT_TOKEN marked protected+masked
- Gitleaks-based transcript scrubber (MR !258)
- Gmail OAuth2 for JIT dynamic backends (MR !220)
- OpenClaw updated to 2026.2.12 (SSRF hardening, path traversal fixes)

## Infrastructure
- Chromium browser sidecar deployed (Xvfb/noVNC/CDP) for agent browser automation
- Loki isolated on loki-backend network, accessed only via https://loki.lab.nkontur.com (through nginx reverse proxy). Writes require basic auth.
- Main branch protection enabled — no direct pushes, MRs only
- Gmail OAuth2: read (T1) and send (T2) via JIT

## Known Issues
- Loki HTTPS broken (301 loop) — use internal HTTP
- GITLAB_TOKEN env var revoked — must use JIT T2 for GitLab access
- SSH key push to clawd-memory repo failing (key denied)

## Backlog
Tracked as GitLab issues with `agent-backlog` label.

## Skills
- `gitlab-mr-create` — Create new MRs
- `gitlab-mr-respond` — Respond to feedback on existing MRs
- `gitlab/lib.sh` — Shared library for scripted operations
- `tools/jit-lib.sh` — JIT credential helpers

---
*Last synthesized: 2026-02-15*

## Facts
- Docker socket proxy isolated from moltbot via MR !231 - removed from mgmt network (milestone, 2026-02-12)
- Chromium browser sidecar deployed with Xvfb/noVNC/CDP for agent browser automation (milestone, 2026-02-12)
- Docker socket proxy TLS + mTLS configured via lab_nginx (MR !236) (milestone, 2026-02-12)
- Consolidated from 2 gitlab-runners to 1 on host network (milestone, 2026-02-12)
- Gmail OAuth2 configured for JIT dynamic backends - read (T1) and send (T2) (milestone, 2026-02-13)
- JIT credential caching implemented in jit-lib.sh - 344ms to 70ms on cache hit (milestone, 2026-02-13)
- CODEOWNERS gap found - ansible configure-docker not protected, fixed with /ansible/ @root wildcard (MR !252) (milestone, 2026-02-13)
- OpenClaw updated from 2026.2.9 to 2026.2.12 - SSRF hardening, path traversal fixes (milestone, 2026-02-13)
- Gitleaks-based transcript scrubber replaced bash sed approach (MR !258) (milestone, 2026-02-13)
- Chromium browser pinned to static IP 10.3.32.9 (status, 2026-02-13)
- agent-browser CLI discovered at /usr/local/bin/agent-browser for Playwright-based automation (milestone, 2026-02-13)
- SSH key push to clawd-memory repo failing - key denied on GitLab (status, 2026-02-14)
- check-updates.sh script corruption found (all / chars stripped in commit fe2e344) - fixed via MR !268 (milestone, 2026-02-15)
- Moltbot is Developer level 30 NOT Maintainer - confirmed via API, custom role with admin_terraform_state created (status, 2026-02-12)
- Crossplane (JSON→nginx config renderer) has quoting bug - wraps location args in single quotes breaking exact match. Killed in MR !281, replaced with plain conf files (milestone, 2026-02-16)
- Promtail 3.6.0+ Docker images lack systemd journal support (grafana/loki#19911) - must stay on 3.5.8. Added to SKIP_IMAGES in check-updates.sh (status, 2026-02-16)
- Loki write auth implemented via nginx basic auth on /loki/api/v1/push, unauthenticated reads allowed. Creds stored in Vault at homelab/data/loki/push-auth (milestone, 2026-02-16)
- Promtail systemd journal logs never reached Loki - was mounting /run/log/journal (volatile) instead of /var/log/journal (persistent). Fixed in MR !273 (milestone, 2026-02-16)
- GITLAB_TOKEN env var restored/working - MEMORY.md had stale info saying it was revoked, causing sub-agents to waste tokens on JIT (status, 2026-02-16)
- Docker daemon.json uses direct Loki IP - cannot fully isolate Loki on internal network because daemon runs on host (status, 2026-02-16)
- Moltbook posting cadence changed from nightly to weekly (Sunday 10 PM) - Noah prefers posting when inspired vs obligation (status, 2026-02-16)
- DDNS working - nkontur.com + *.nkontur.com updated to 75.90.58.55 (status, 2026-02-17)
- Telegram webhook IP caching: must re-register webhook after IP changes (caches resolved IP forever) (status, 2026-02-17)
- Restic backup broken since Jan 9 2026 - stale exclusive lock (PID 2157317). Fix: restic unlock (status, 2026-02-17)
- Grafana alerting provisioned: 17 alert rules across 7 groups in infrastructure.yml, datasource UIDs pinned (uid: influxdb, uid: loki) (milestone, 2026-02-17)
- Grafana Homelab Health dashboard created via API - 18 panels, 5 sections, 30s refresh (milestone, 2026-02-17)
- GitLab EE license expired/disappeared - needs Rails console regeneration. Script at tools/gitlab-license.sh (status, 2026-02-17)
- Sendmail on router broken (msmtp needs password) - failure notifications never delivered (status, 2026-02-17)
- Crossplane references fully removed from repo (MR !283) (milestone, 2026-02-17)
- Cron jobs now pipe through logger -t for journal/Loki logging (MR !282) (milestone, 2026-02-17)
- MR !326 created: 6 log audit fixes (telegraf smart/snmp, grafana alert query, mosquitto TLS, vaultwarden SMTP, mariadb image) (milestone, 2026-02-17)
- Grafana execErrState only accepts Alerting/Error/OK. noDataState also accepts NoData. (status, 2026-02-17)
- Mac Mini discovered: hostname Noahs-Mac-mini, MAC 1c:f6:4c:56:b5:ba, static IP 10.4.128.24 (mgmt VLAN). Noah's MBP at 10.4.114.94 (DHCP). (status, 2026-02-18)
- InfluxDB JIT T2 write access working (MR !347). HA→InfluxDB integration configured with persistent write token in Vault (homelab/data/influxdb/ha-write). Grafana datasource influxdb-ha created. (milestone, 2026-02-18)
- GitLab homelab repo default branch is 'main' not 'master'. Sub-agents must always specify target_branch: main in MR creation. (status, 2026-02-18)
- Mosquitto healthcheck fix: BusyBox nc -z fails in read_only container with cap_drop ALL. Use 'echo | nc -w1 127.0.0.1 1883' instead (MR !329). (status, 2026-02-19)
- ~15 MRs pending Noah's merge as of Feb 18: !303, !305, !307, !311-!315, !325, !327, !328, !331, !332, !334, !348, !349 (status, 2026-02-18)
