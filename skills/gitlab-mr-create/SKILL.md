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

WORK_DIR=$(mktemp -d) && cd "$WORK_DIR"
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab && git config user.email "moltbot@nkontur.com" && git config user.name "Moltbot"

# Branch, change, commit, push, create MR, wait for pipeline, merge if trivial
```

You know git. Check the reference doc for repo-specific gotchas (project ID 4, pipeline stages, when to self-merge vs escalate).
