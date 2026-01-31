---
name: gitlab-mr
description: Comprehensive GitLab MR lifecycle management for homelab infrastructure. Creates feature branches, implements changes, opens MRs, monitors for feedback, and iterates until merged. Use when implementing infrastructure changes, fixing CI/CD issues, updating configurations, or any task requiring a complete MR workflow from branch creation to merge.
---

# GitLab MR Lifecycle Management

Automates the complete merge request lifecycle for homelab infrastructure changes:

1. **Branch creation** - Creates feature branches from main
2. **Implementation** - Makes code/config changes to accomplish goals
3. **MR creation** - Opens merge requests via GitLab API
4. **Tracking** - Registers MR for comment monitoring
5. **Iteration** - Responds to feedback (triggered by cron when comments detected)
6. **Completion** - Cleans up tracking when merged/closed

## Environment Setup

**GitLab Instance:** https://gitlab.lab.nkontur.com  
**Project:** root/homelab (Project ID: 4)  
**Authentication:** `$GITLAB_TOKEN` environment variable  
**MR Tracking File:** `/home/node/clawd/memory/open-mrs.json`

## Workspace Setup — IMPORTANT

**Always clone a fresh copy to a temp directory.** Never use the shared workspace at `/home/node/clawd/homelab` — that can cause conflicts with other agents.

```bash
# Create isolated workspace
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"

# Clone fresh copy with auth
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab

# Configure git identity
git config user.email "moltbot@nkontur.com"
git config user.name "Moltbot"
```

## glab CLI Setup

The `glab` CLI is available for MR operations. Configure it before use:

```bash
export GITLAB_HOST=gitlab.lab.nkontur.com
export GITLAB_TOKEN="${GITLAB_TOKEN}"
```

## Core Workflow

### 1. Setup Fresh Workspace
```bash
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab
git config user.email "moltbot@nkontur.com"
git config user.name "Moltbot"
```

### 2. Create Feature Branch
```bash
git checkout -b feature/brief-description
```

### 3. Implement Changes
- Edit configuration files, docker-compose.yml, Ansible playbooks
- Commit with conventional commit messages (`feat:`, `fix:`, `perf:`, `docs:`)

### 4. Push and Create MR
```bash
git push -u origin feature/branch-name
```

Create MR via API:
```bash
MR_RESPONSE=$(curl -s -X POST "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests" \
     -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "source_branch": "feature/branch-name",
       "target_branch": "main", 
       "title": "Descriptive title",
       "description": "Detailed description"
     }')

MR_IID=$(echo "$MR_RESPONSE" | jq -r '.iid')
MR_URL=$(echo "$MR_RESPONSE" | jq -r '.web_url')
```

### 5. Register MR for Tracking — REQUIRED

After creating an MR, **you must register it** so the cron job can monitor for comments:

```bash
# Read existing tracking file (or initialize empty)
TRACKING_FILE="/home/node/clawd/memory/open-mrs.json"
if [ ! -f "$TRACKING_FILE" ]; then
  echo '{}' > "$TRACKING_FILE"
fi

# Add this MR to tracking
jq --arg iid "$MR_IID" \
   --arg title "$MR_TITLE" \
   --arg branch "$BRANCH_NAME" \
   --arg goal "$ORIGINAL_GOAL" \
   '. + {($iid): {"title": $title, "branch": $branch, "goal": $goal, "lastCommentId": 0}}' \
   "$TRACKING_FILE" > tmp.json && mv tmp.json "$TRACKING_FILE"
```

Or using the `write` tool to update the JSON directly.

### 6. Wait for Pipeline to Pass

Before reporting done, **wait for the CI pipeline to pass**. An MR with a red pipeline isn't done.

```bash
# Poll pipeline status until it completes
echo "Waiting for pipeline..."
MAX_ATTEMPTS=60  # 10 minutes max (60 * 10s)
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  PIPELINE_STATUS=$(glab mr view "$MR_IID" --output json 2>/dev/null | jq -r '.pipeline.status // "pending"')
  
  case "$PIPELINE_STATUS" in
    "success")
      echo "✓ Pipeline passed"
      break
      ;;
    "failed"|"canceled")
      echo "✗ Pipeline $PIPELINE_STATUS — investigating..."
      # Check job logs, fix issues, push again, reset counter
      # ... handle failure ...
      ATTEMPT=0
      ;;
    *)
      echo "Pipeline status: $PIPELINE_STATUS (attempt $ATTEMPT)"
      sleep 10
      ATTEMPT=$((ATTEMPT + 1))
      ;;
  esac
done

if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
  echo "⚠ Pipeline timed out — reporting anyway"
fi
```

**If pipeline fails:**
1. Check the job logs: `glab ci view`
2. Fix the issue in your branch
3. Commit and push the fix
4. Reset the polling loop

Only report the MR as complete once the pipeline is green (or you've exhausted reasonable retry attempts).

### 7. Report and Exit

After the pipeline passes and tracking is registered, report the MR link and exit. A cron job will monitor for comments and spawn a new agent if Noah responds.

**Do NOT implement comment polling loops.** The cron handles that. But DO wait for pipeline success before exiting.

---

## Responding to Comments (for cron-spawned agents)

If you were spawned by the MR monitoring cron, you'll receive:
- The MR IID and branch name
- The new comments to address
- The original goal

**Your workflow:**

1. **Clone the repo and checkout the branch:**
```bash
WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
cd homelab
git checkout feature/branch-name
git pull origin feature/branch-name
```

2. **Read and understand the comments**

3. **Make requested changes**

4. **Commit and push:**
```bash
git add -A
git commit -m "Address review feedback: <summary>"
git push origin feature/branch-name
```

5. **Reply to comments via API:**
```bash
curl -s -X POST \
  "https://gitlab.lab.nkontur.com/api/v4/projects/4/merge_requests/$MR_IID/notes" \
  -H "PRIVATE-TOKEN: $GITLAB_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "Done — <explain what you changed>"}'
```

6. **Exit.** The cron will continue monitoring for further comments.

---

## Cleanup (when MR is merged/closed)

The cron job automatically removes merged/closed MRs from tracking. No manual cleanup needed.

---

## Key Guidelines

- **Fresh clone every time** — Avoid workspace conflicts
- **Never push to main** — Always use feature branches
- **Register every MR** — Or comments won't be detected
- **No polling loops** — Let cron handle monitoring
- **Cleanup temp dirs** — `rm -rf "$WORK_DIR"` when done

## Bundled Resources

- **[API Reference](references/api.md)** - GitLab API endpoints
- **[Workflow Details](references/workflow.md)** - Additional patterns
