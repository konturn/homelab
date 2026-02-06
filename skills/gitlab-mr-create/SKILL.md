---
name: gitlab-mr-create
description: Create new GitLab merge requests for homelab infrastructure. Handles branch creation, implementing changes, opening MRs, waiting for pipeline, and registering for tracking. Use when implementing new features, fixes, or configuration changes that need a fresh MR.
---

# GitLab MR Creation

**→ See consolidated reference:** `/home/node/clawd/skills/gitlab-mr/SKILL.md`

This skill has been merged into the main GitLab MR reference doc. The consolidated doc covers both creation and feedback response in a leaner format.

**Quick start:**
```bash
source /home/node/clawd/skills/gitlab/lib.sh
preflight_check || exit 1

REPO="/home/node/.openclaw/workspace/homelab"
BRANCH="feat/my-thing"
WORK_DIR="/tmp/homelab-${BRANCH}"

cd "$REPO" && git fetch origin main
git worktree add -b "$BRANCH" "$WORK_DIR" origin/main
cd "$WORK_DIR"
git config user.email "moltbot@nkontur.com" && git config user.name "Moltbot"

# Make changes, commit, push, create MR, wait for pipeline, merge if trivial

# Clean up worktree when done
cd /tmp && git -C "$REPO" worktree remove "$WORK_DIR" 2>/dev/null
```
**⚠️ NEVER checkout branches in the shared repo clone.** Use worktrees — multiple agents run concurrently.

You know git. Check the reference doc for repo-specific gotchas (project ID 4, pipeline stages, when to self-merge vs escalate).
