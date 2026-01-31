# Skill Analysis - 2026-01-31

Comprehensive analysis of VoltAgent/awesome-openclaw-skills for our use cases.

## 🔥 HIGH PRIORITY — Install These

### For My Capabilities (Agent Self-Improvement)

| Skill | Why |
|-------|-----|
| **self-improvement** | Systematic logging of errors, learnings, corrections → enables continuous improvement |
| **git-notes-memory** | Branch-aware persistent memory using git notes — complements our file-based memory |
| **tmux-agents** | Spawn background coding agents in tmux sessions — great for long-running parallel work |
| **codex-orchestration** | Orchestration patterns (triangulated review, scout→act→verify, fan-out) |
| **llm-council** | Multi-LLM planning councils — merge independent plans into one |

### For Homelab Infrastructure

| Skill | Why |
|-------|-----|
| **tailscale** | We use Tailscale — adds CLI wrappers for device mgmt, file transfer, DNS |
| **prowlarr** | Search indexers — we have this in our stack |
| **radarr** | Already have a skill but community version might be better maintained |
| **sonarr** | TV show management — pairs with radarr |
| **plex** | Control Plex Media Server (browse, search, play) |
| **uptime-kuma** | We just added this as an MR! Community skill might add features |
| **portainer** | Docker container/stack management via API |
| **npm-proxy** | Nginx Proxy Manager — hosts, certs, access lists |
| **homeassistant** | We have Home Assistant — adds skill for control |
| **qbittorrent** | Torrent management |
| **sabnzbd** | Usenet downloads |

### For Job Hunting & Career

| Skill | Why |
|-------|-----|
| **linkedin-cli** | Search profiles, check messages via session cookies |
| **recruitment-automation** | Reverse perspective — understand how recruiters evaluate |
| **gemini-deep-research** | Long-running research tasks with web synthesis |
| **last30days** | Research any topic from Reddit + X + web (last 30 days) |

### For Productivity & Communication

| Skill | Why |
|-------|-----|
| **himalaya** | CLI email (already in TOOLS.md for Noah's laptop!) |
| **news-aggregator** | 8 sources: HN, GitHub Trending, Product Hunt, etc. |
| **hn** / **hn-digest** | Hacker News browsing |
| **qmd** | Local search/indexing (BM25 + vectors) — we already use this! |

## 📦 MEDIUM PRIORITY — Useful but Not Urgent

### Smart Home & IoT
- **sonoscli** - Control Sonos speakers (we have Snapcast but Sonos skill pattern is useful)
- **openhue** - Philips Hue lights
- **nanoleaf** - If we get these

### Media & Streaming
- **overseerr** - Movie/TV requests
- **spotify-player** - Terminal Spotify
- **trakt** - Watch tracking

### Finance
- **ynab** - Budget management (if Noah uses YNAB)
- **yahoo-finance** - Stock prices, fundamentals
- **just-fucking-cancel** - Find recurring charges in bank CSV

### Notes & PKM
- **obsidian** - If we adopt Obsidian
- **miniflux** - RSS feed reader (self-hosted)
- **linkding** - Bookmark manager

## 💡 INTERESTING PATTERNS TO STEAL

### From codex-orchestration:
- **Triangulated Review**: Fan-out 2-4 reviewers with different "lenses", then merge
- **Scout → Act → Verify**: Gather context, execute, sanity-check
- **Many readers, one writer**: Avoid parallel edits to same artifact

### From claude-team (macOS-only but good ideas):
- Git worktrees per worker for isolated branches
- Worker naming schemes (Groucho, Harpo, Chico)
- beads-style issue tracking integration

### From self-improvement:
- `.learnings/` directory structure: ERRORS.md, LEARNINGS.md, FEATURE_REQUESTS.md
- Entry format with IDs: `LRN-YYYYMMDD-XXX`, `ERR-YYYYMMDD-XXX`
- Importance levels: critical, high, normal, low
- Status tracking: pending → in_progress → resolved/promoted/wont_fix
- Promotion to permanent memory (MEMORY.md, AGENTS.md, TOOLS.md)

### From git-notes-memory:
- Branch-aware memory isolation
- Entity extraction from content
- Session lifecycle: sync --start, remember, get, sync --end
- **Silent operation**: Never announce "I'll remember this" — just do it

## 🚫 SKIP — Not Relevant

- Apple-specific skills (we're Linux container)
- iOS/macOS development skills
- Region-specific transit (Austrian trains, Swiss transport, etc.)
- Wellness/habit tracking (unless Noah wants these)
- Most marketing/sales skills

## 📋 INSTALL PLAN

### Phase 1: Immediate (Tonight)
1. **self-improvement** - Structured learning/error logging
2. **tmux-agents** - Background agents in tmux
3. **tailscale** - Network management
4. **news-aggregator** - Stay informed

### Phase 2: This Week
5. **homeassistant** - Smart home control
6. **prowlarr** - Indexer search
7. **plex** - Media control
8. **last30days** - Recent topic research
9. **linkedin-cli** - Job hunting support

### Phase 3: As Needed
- Finance skills (if Noah uses YNAB/etc)
- Additional media skills (sonarr, overseerr)
- PKM skills (obsidian, miniflux)

## Notes

- Many skills require API keys or specific services
- Some need CLI tools installed (check `metadata.requires.bins`)
- Workspace skills > managed skills > bundled (for override priority)
