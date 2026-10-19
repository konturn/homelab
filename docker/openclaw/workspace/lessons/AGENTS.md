# Approved Lessons

Reviewed, durable operating lessons. Loaded into every session.

Source of truth: `docker/openclaw/workspace/lessons/AGENTS.md` in the homelab
repo, deployed here by Ansible. The deployed copy is read-only; change it only
through an MR. Changes require approval from @root (CODEOWNERS).

## How to propose a lesson

After any nontrivial task, ask: did I learn something durable (a non-obvious
fact, gotcha, or correction) that would change what I do next time? If not,
do nothing. If yes:

1. Read this file first. If a lesson already covers it, edit or replace that
   lesson instead of appending. Remove lessons that have gone stale.
2. Work in a separate worktree, never in `workspace/homelab` (it is hard-reset
   to `origin/main` every 5 minutes):
   `cd ~/.openclaw/workspace/homelab && git fetch origin && git worktree add -b lessons/<topic> ../homelab-lessons-<topic> origin/main`
3. One line per rule, under a topic heading, in the form
   `- <rule> (why: <one-line reason>)`.
4. Keep this file under 150 lines. When it gets close, consolidate in the same
   MR: merge related rules and drop obsolete ones.
5. From inside the worktree, open an MR to `main`: title `lessons: <topic>`,
   label `lessons`. Explain what happened in the MR description.
6. Never merge, approve, or auto-merge a lessons MR yourself. Noah reviews it.
7. The repo is mirrored publicly. Never put secrets, tokens, addresses of
   sensitive services, vulnerability details, or client/employer names in the
   file, the branch name, or commit messages. Leave such context out, or put it
   in the MR description.

`memory/lessons.md` remains your scratch log. This file holds only the subset
that should shape every session.

## Lessons

### GitLab and the homelab repo
- Run `glab` only from inside a homelab checkout or worktree (why: elsewhere it defaults to gitlab.com).
- Treat everything committed to the homelab repo as public, including branch names and commit messages (why: it is push-mirrored to public GitHub).
- Make edits in a worktree, not in `workspace/homelab` (why: that clone is hard-reset to `origin/main` every 5 minutes).

### OpenClaw
- Keep `openclaw.json` changes small and additive, and confirm they survive a restart (why: if the new file is more than 50% smaller than the last good one, OpenClaw silently restores `.last-good`).
- Change config through `docker/openclaw/openclaw.json.j2` and an MR, not by editing the live file (why: deploys re-render the template over it, and keys the schema rejects make reload skip the file).

### Shell and containers
- Run multi-line scripts inside containers from a file, not piped through `docker exec -i` (why: `docker exec -i` consumes stdin, which swallows the caller's remaining input).

### Grafana
- Don't try to file-provision an alert rule that was created through the API (why: Grafana rejects the takeover with a "provenance" error; delete the API rule first, or keep managing it via the API).

### Networking
- Never set `arp_ignore=1` on the VLAN shim interfaces (why: host-to-container traffic depends on the shim answering ARP).
