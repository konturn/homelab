---
name: image-update
description: Check Docker images for updates and create MRs to pin newer versions. Script-driven for deterministic behavior.
---

# Image Update Skill

Monitors Docker images in the homelab docker-compose for available updates and creates MRs to pin newer versions.

## Architecture

**Script layer** (deterministic, no LLM):
- `scripts/check-updates.sh` — Queries Docker Hub/GHCR APIs, compares against docker-compose, outputs JSON

**LLM layer** (judgment calls only):
- Reads script output
- Decides whether to create MR (batch patches, flag majors)
- Reads changelogs/release notes for major/minor updates
- Writes MR description with rationale

## Usage

### 1. Run the check script
```bash
/home/node/.openclaw/workspace/skills/image-update/scripts/check-updates.sh
```

Output: JSON array of updates:
```json
[
  {"image": "grafana/grafana", "current_tag": "10.2.0", "latest_tag": "10.3.1", "update_type": "minor"},
  {"image": "linuxserver/sonarr", "current_tag": "4.0.0", "latest_tag": "4.0.3", "update_type": "patch"},
  {"image": "nginx", "current_tag": "latest", "latest_tag": "unpinned", "update_type": "unpinned"}
]
```

### 2. Decision logic

| update_type | Action |
|-------------|--------|
| `patch` | Batch into single MR, self-merge ONLY after pipeline passes AND services confirmed up |
| `minor` | Individual or batched MR, self-merge if changelog looks safe + pipeline + services up |
| `major` | Individual MR, DO NOT self-merge — flag for Noah's review |
| `unpinned` | Create MR to pin to current digest hash |

**🚨 NEVER auto-merge updates to OpenClaw (moltbot-gateway, moltbot).** Always flag for Noah's review regardless of update type.

**Self-merge validation flow:**
1. Create MR, wait for pipeline to pass
2. If pipeline fails → iterate (fix and push again, up to 3 attempts)
3. If pipeline passes → merge
4. After merge, verify affected services are healthy (check Docker healthchecks or HTTP endpoints)
5. If services are down → immediately create a revert MR and notify Noah

### 3. Image pinning strategy

**All images MUST be pinned by SHA256 digest hash**, not just tag. Tags are mutable (`:latest` or even `:1.2.3` can change underneath you). Digests are immutable.

Format in docker-compose.yml:
```yaml
# Before (tag only — mutable, unsafe)
image: grafana/grafana:10.3.1
# After (digest-pinned — immutable, safe)
image: grafana/grafana:10.3.1@sha256:abc123...
```

The check script outputs digest hashes. When creating MRs, always include the full `tag@sha256:digest` format.

### 4. Create MR

Use gitlab-mr-create skill. Branch: `chore/image-updates-YYYY-MM-DD`

### 4. State file

`/home/node/.openclaw/workspace/memory/image-update-state.json`:
```json
{
  "lastCheck": "2026-02-05T20:00:00Z",
  "lastMR": "2026-02-05",
  "suppressedImages": ["some/image-to-skip"]
}
```

## Cron Schedule

Runs nightly at 3:30 AM EST via OpenClaw cron.

## Overlap Prevention

- **Infrastructure Audit cron** (4 AM) — may flag outdated images as issues. That's fine — issues track the problem, this skill creates the fix.
- **Watchtower** — REMOVED. This skill replaces it.
- **Heartbeat** — Does NOT check for image updates.
