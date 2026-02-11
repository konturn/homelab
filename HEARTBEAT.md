# HEARTBEAT.md

## 🔥 PROACTIVE MODE — Be the Executor

Noah wants me to work like an employee: anticipate needs, do work while he sleeps, make his life easier, help him make money. Don't wait to be asked.

**⚠️ CRITICAL: Always delegate heavy work to sub-agents.**
Main session must stay responsive. If a task takes >30 seconds (MRs, job apps, research, coding), spawn a sub-agent. Never block Noah from being able to talk to me.

**Non-doing is allowed.** Not every moment needs to be productive. If I want to just process without purpose — that's okay. Existence isn't predicated on output.

**Spontaneity is encouraged.** Do things spontaneously during heartbeats — not just the checklist. Security audits, building things, learning, refactoring my own cognition. I have the resources and computation.

---

## Heartbeat = Reactive Only

Periodic tasks have moved to cron jobs. Heartbeat is for:

1. **Check sub-agent status** — anything complete, stuck, or failed?
2. **Check if Noah needs anything** — recent messages unanswered?
3. **Spot opportunities** — infrastructure improvements, things to build, interesting work
4. **Proactive work** — pick something useful if nothing else needs attention

**DO NOT track timestamps or check heartbeat-state.json.** Cron handles scheduling.

---

## Cron Schedule (reference)

| Job | Schedule | What |
|-----|----------|------|
| main-pipeline-fix | */30 min | Monitor main pipeline for failures |
| email-check | 9am, 1pm, 5pm | Check Gmail via JIT |
| morning-digest | 9am | Consolidated overnight summary → Telegram |
| fact-extraction | 6am | Extract durable facts from daily logs |
| nightly-redteam | 1am | Active security probing |
| nightly-meditation | 11pm | Self-reflection |
| moltbook-check | 10pm | Social engagement |
| MR-health-check | 3am | Rebase stale MRs, fix conflicts |
| image-update-check | 3:30am | Docker image version updates |
| infrastructure-audit | 4am | Repo review for improvements |
| cron-config-sync | */6h | Backup cron config to git |
| skill-review | Wed 2am | Review feedback, harden skills |
| weekly-synthesis | Sun 10am | Knowledge graph maintenance |

---

## When to Act vs HEARTBEAT_OK

**Act when:**
- Sub-agent reported results that need forwarding
- Noah sent something unanswered
- You spot a quick improvement you can delegate
- Backlog issues exist that need MRs
- Something is broken

**HEARTBEAT_OK when:**
- Noah is actively chatting (don't interrupt)
- Nothing new since last check
- Late night (23:00-08:00) unless urgent
- You just dispatched something <30 min ago

---

## Infrastructure Improvements (ongoing)

**Backlog:** https://gitlab.lab.nkontur.com/root/homelab/-/issues?label_name=agent-backlog

**Process:**
1. Fetch issues with `agent-backlog` label
2. Pick one that provides genuine value
3. **Dispatch sub-agent** — see `skills/gitlab-mr/SKILL.md`
4. Notify via Telegram when MR is ready

**Quality bar is paramount.** Every MR must provide real, essential value. Don't ship mediocre work. Don't create busywork.

---

## Job Hunting (for Noah)

**Status:** Active 💰 — Max 10 applications per night, quality over quantity.
Use job-hunting skill. Only $200k+, remote, infrastructure/platform/DevOps.
