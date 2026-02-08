# Homelab Infrastructure Project

My access to Noah's GitLab homelab repository and ability to make infrastructure improvements via MRs.

## Access
- **GitLab:** gitlab.lab.nkontur.com (user: moltbot, **Maintainer** role)
- **Repo:** root/homelab (project ID: 4)
- **Token:** `$GITLAB_TOKEN` — has git push + API access
- **Workflow:** Push → GitLab CI → Ansible deploys to router
- **Self-merge policy:** Small config tweaks, iteration, trivial changes = self-merge. Big architectural changes = Noah's review.
- **GitLab Ultimate:** License active, CODEOWNERS enforcement working.

## Major Accomplishments (Feb 2026)

### Vault Integration (Feb 5-7)
- **MR !116** — Vault JWT auth (merged)
- **MR !123** — AppRole rotation job + docs (merged)
- **MR !126** — Full Vault secret migration — all 47 secrets fetched from Vault with env var fallback (merged)
- **MR !128** — Remaining secrets (Cloudflare, Grafana, Spotify) to Vault (merged)
- **MR !129** — JWT fix (bound_audiences + ci-deploy role) (merged)
- **MR !132** — Vault secret trailing newline trim (merged)

### Security Hardening
- **MR !125** — Docker image SHA256 pinning (34 images)
- **MR !127** — Credential scrubbing cron
- **MR !133** — Restic exclude for vault unseal keys
- **MR !109** — CODEOWNERS draft
- **MR !104** — no_log for secrets
- **MR !105** — amcrest2mqtt TLS
- JIT Privileged Access Management design doc (`docs/jit-access-design.md`) — 2500+ lines

### Infrastructure
- **MR !124** — Tailscale firewall fix (DNS, HTTP/S, ICMP)
- **MR !130** — Promtail host log ingestion to Loki
- **MR !107** — Traefik migration design doc (self-merged)
- **MR !111** — GitLab memory tuning (Sidekiq, Puma, PostgreSQL)

### Earlier (Jan 30 - Feb 3)
- MR #2 — CI dry-run validation (merged)
- MR #9 — API access for Radarr, Sonarr, Plex, Ombi, etc. (merged)
- MR #12 — Telegraf diskio fix (merged)
- MR #13 — Satellite-2 deployment (merged)
- MR #29 — GitLab Container Registry (merged)
- MR #30 — Auto-cancel redundant pipelines (merged)

## Current Status
- **186 commits** from me out of 835 total (22.3% of repo)
- **68.1%** of commits since I joined are mine
- Repo grew 48.3% in first 6 days
- Vault JWT auth not fully working yet (bound_audiences issue) — falls back to CI env vars
- CI/CD variable cleanup: 19 dead vars deleted, 44 Vault-backed vars kept (can't deprecate until JWT works)

## Known Issues
- GitLab /jobs API endpoint is slow (700ms warm) — likely ci_builds table bloat
- Sidekiq concurrency=5 causes pipeline creation delays during clustered merges (recommendation: bump to 10)
- Runner concurrent=2 too low for parallel deploys (recommendation: bump to 4)

## Backlog
Tracked as GitLab issues with `agent-backlog` label.

## Skills
- `gitlab-mr-create` — Create new MRs
- `gitlab-mr-respond` — Respond to feedback on existing MRs
- `gitlab/lib.sh` — Shared library for scripted operations

---
*Last synthesized: 2026-02-08*
