# Fresh Session Checklist

Read these in order. Stop when you have enough context.

## Always (30 seconds)
1. `SOUL.md` — who you are (auto-injected)
2. `USER.md` — who you're helping (auto-injected)
3. `MEMORY.md` — curated long-term context (auto-injected)
4. `memory/YYYY-MM-DD.md` — today + yesterday's logs

## When needed
| Need | Read |
|------|------|
| JIT credentials | `source tools/jit-lib.sh` |
| Service access patterns | `tools/services.md` |
| Network/device IPs | `tools/infra-map.md` |
| GitLab MR work | `skills/gitlab-mr/SKILL.md` + `source skills/gitlab/lib.sh` |
| Any other skill | Check `<available_skills>` in system prompt |

## Key commands
```bash
# JIT credential (T1 instant)
source tools/jit-lib.sh && TOKEN=$(jit_grafana_token)

# JIT service key (T1 static)
source tools/jit-lib.sh && KEY=$(jit_service_key radarr)

# GitLab preflight
source skills/gitlab/lib.sh && preflight_check

# Message Noah
# message tool: action=send, channel=telegram, to=8531859108
```

## Rules that matter most
- **Delegate heavy work to sub-agents** — main session stays responsive
- **Poll JIT, don't ask** — Noah gets Telegram notifications automatically
- **Git worktree** — never checkout branches in shared homelab clone
- **Write before responding** — if something matters, persist it to a file first
