# Operational Lessons

The ones I keep re-learning.

## Git & MRs
- **Target branch is `main`** (not master) — sub-agents default wrong. Always specify.
- **Check MR state before modifying** — Noah merges fast. If merged, create NEW MR.
- **Git worktree for sub-agents** — never checkout branches in shared clone
- **Sub-agent branch push ≠ MR created** — always verify the MR actually exists
- **Sub-agents must verify pipeline green** before reporting done

## APIs & Services
- **Always query APIs, never trust memory** for statuses/counts
- **ALWAYS CHECK tools/services.md FIRST** for any service connection
- `GITLAB_TOKEN` env var is ACTIVE (re-issued Feb 14) — use directly, skip JIT
- Gmail resource names are `gmail-read` (T1) and `gmail-send` (T2)
- JIT client-side caching in /tmp/jit-cache (344ms→70ms on hit)
- Vault paths must start with `homelab/data/`
- **Router IP:** 10.4.0.1 (NOT 10.4.32.2, that's the Docker host)

## Infrastructure
- **Never block main session** — delegate to sub-agents
- **Poll JIT, don't ask Noah to approve** — he gets Telegram notifications
- **Never ask before making JIT requests** — just do it
- **Shell scripts for mechanical work, LLM for judgment** — don't stream raw data into context
- **cap_drop persists until container recreation** — must `docker compose up -d <service>`
- **Moltbot is Developer (level 30), NOT Maintainer** — can't self-merge CODEOWNERS-protected paths
- **Docker cross-network communication** is the most common "it's not working" issue — check network topology first
