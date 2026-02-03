---
name: gitlab-mr-respond
description: Respond to feedback on existing GitLab merge requests. Reads comments, understands intent, makes fixes, posts threaded replies, and updates MR description. Use when spawned by MR monitoring cron or when explicitly asked to address feedback on an open MR.
---

# GitLab MR Feedback Response

**→ See consolidated reference:** `/home/node/clawd/skills/gitlab-mr/SKILL.md`

This skill has been merged into the main GitLab MR reference doc. The consolidated doc covers both creation and feedback response in a leaner format.

**Quick start:**
```bash
source /home/node/clawd/skills/gitlab/lib.sh

# Clone, checkout the branch, make the requested fix, push
# Reply in the discussion thread:
gitlab_api_call POST "/projects/4/merge_requests/$MR_IID/discussions/$DISCUSSION_ID/notes" \
  '{"body":"Done ✓ — explanation of fix"}'

# Wait for pipeline, merge if appropriate
wait_for_pipeline "$MR_IID"
```

You know git. Check the reference doc for repo-specific gotchas (project ID 4, when to escalate vs iterate, self-merge policy).
