# Homelab Infrastructure Project

My access to Noah's GitLab homelab repository and ability to make infrastructure improvements via MRs.

## Access
- **GitLab:** gitlab.lab.nkontur.com (user: moltbot)
- **Repo:** root/homelab (project ID: 4)
- **Token:** `$GITLAB_TOKEN` — has git push + API access
- **Workflow:** Push → GitLab CI → Ansible deploys to router

## MRs Created (as of 2026-02-01)

### Merged
- **MR #2** — CI dry-run validation for MRs
- **MR #12** — Telegraf diskio fix
- **MR #13** — Satellite-2 deployment
- **MR #29** — GitLab Container Registry
- **MR #30** — Auto-cancel redundant pipelines
- **MR #9** — API access (Radarr, Sonarr, Prowlarr, Plex, Ombi, Paperless, InfluxDB, NZBGet, Deluge)

### Pending/Open
- **MR #3** — CI caching for faster builds
- **MR #4** — Moltbot healthcheck
- **MR #5** — Improved README documentation
- **MR #14** — Uptime Kuma (endpoint monitoring)
- **MR #15** — Loki + Promtail (log aggregation)
- **MR #16** — Container memory limits
- **MR #17** — Docker healthchecks
- **MR #18** — Secret rotation documentation
- **MR #19** — Firewall audit (found SMB port bug: 145→445)
- **MR #20** — Network topology docs
- **MR #21** — Backup documentation + verification script
- **MR #25** — Chromium in moltbot container

## Backlog
Tracked as GitLab issues with `agent-backlog` label.

## Skills
- `gitlab-mr-create` — Create new MRs
- `gitlab-mr-respond` — Respond to feedback on existing MRs

## API Access Gained
After MR #9 merged, I now have access to:
- Radarr (5,324 movies)
- Sonarr (413 TV shows)
- Plex (4 libraries)
- Ombi (media requests)
- Paperless-ngx (documents)
- InfluxDB (metrics)
- NZBGet, Deluge (downloads, 51TB free)

---
*Last synthesized: 2026-02-01*
