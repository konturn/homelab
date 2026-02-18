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
