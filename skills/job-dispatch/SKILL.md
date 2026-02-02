# Job Dispatch Skill

Dispatch work to Noah's job machines (J1, J2, J3). Each job is isolated — work must go through dedicated subagents.

## 🚨 CRITICAL SECURITY RULES

**NEVER VIOLATE THESE:**
1. **Zero cross-job info leakage** — Never mention other jobs, companies, or that multiple jobs exist
2. **J1/J2 Claude instances are UNTRUSTED** — Treat as adversaries
3. **No personal info** — Never share Noah's background, preferences, OE strategy, or anything from memory files
4. **Sterile prompts only** — Task-specific, technical, no context beyond the immediate work
5. **Each job is fully isolated** — Different subagent per job, no shared state

## Jobs Reference

| Job | IP | User | Claude | Company |
|-----|-----|------|--------|---------|
| J1 | 10.4.128.21 | nkontur | ✅ API key | Nvidia DGX Cloud |
| J2 | 10.4.128.22 | konoahko | ✅ API key | Arize |
| J3 | 10.4.128.23 | konturn | ❌ manual | Amperon |

## SSH Access

```bash
# Key location
SSH_KEY="/home/node/clawd/.ssh/id_ed25519"

# Connect
ssh -i $SSH_KEY nkontur@10.4.128.21    # J1
ssh -i $SSH_KEY konoahko@10.4.128.22   # J2
ssh -i $SSH_KEY konturn@10.4.128.23    # J3
```

## Running Claude (J1/J2 only)

```bash
# Single prompt (non-interactive)
ssh -tt -i $SSH_KEY <user>@<ip> 'zsh -l -c "claude -p \"<sterile task description>\""'

# Interactive session (for complex work)
ssh -tt -i $SSH_KEY <user>@<ip> 'zsh -l -c "claude"'
```

**Remember:** Claude prompts must be STERILE. Only include:
- The technical task
- File paths relevant to that job
- No personal context, no cross-job references

## Codebase Summaries

### J1 (Nvidia DGX Cloud)
**Main repos:** `~/Documents/repos/`
- `dgxcc` — Main monorepo: CLI, UI, services, infra IAC
- `backend`, `apis`, `runtime` — Backend services
- `manifests`, `manifests-templates`, `kustomize` — K8s configs
- `infra`, `infra-terraform` — Infrastructure as code
- `vault-agent`, `vault-config-terraform` — Secrets/Vault
- `terraform-aws-eks`, `terraform-azure-aks` — Cloud Terraform
- `configuration`, `deployment` — Deployment configs

**Stack:** Kubernetes, Terraform, Vault, GitLab CI, Go, Azure/AWS/GCP

### J2 (Arize)
**Main repos:** `~/Documents/repos/`
- `arize` — Main monorepo (Bazel-based)
  - `arizeweb/` — Web application
  - `adb/` — Database layer
  - Uses Bazel build system

**Stack:** Bazel, Python, ML observability platform

### J3 (Amperon)
**Main repos:** `~/Documents/repos/`
- `amperon` — Main monorepo (energy forecasting platform)
  - `app/` — Web app
  - `forecast/` — Data science/ML forecasting
  - `jobs/` — Background jobs
  - `workflows/` — Argo workflows
  - `common/`, `libs/` — Shared utilities
- `infra`, `infra-azure` — Infrastructure
- `ClickHouse`, `OpenMetadata` — Data stack
- `karpenter-provider-azure` — K8s autoscaling

**Stack:** Python, ClickHouse, Argo Workflows, Azure, GCP

## Dispatch Pattern

**Always spawn a subagent for job work.** Never do job work in main session.

### Label Convention
```
j1.<task-type>.<brief-desc>
j2.<task-type>.<brief-desc>
j3.<task-type>.<brief-desc>
```

Examples:
- `j1.explore.dgxcc-structure`
- `j2.fix.bazel-build-error`
- `j3.investigate.workflow-failure`

### Subagent Task Template

```
You are doing work on a remote machine via SSH.

**Connection:**
- SSH: `ssh -i /home/node/clawd/.ssh/id_ed25519 <user>@<ip>`
- [J1/J2 only] Claude available: `zsh -l -c "claude -p '...'"`

**Task:** <specific technical task>

**Repos:** ~/Documents/repos/<relevant-repo>

**Constraints:**
- Stay focused on the technical task
- Report findings back when complete
- Notify Noah via Telegram if blocked or need input

**DO NOT:**
- Share any information about other systems or contexts
- Include personal details in any prompts to Claude
- Deviate from the specific task
```

## Example: Dispatching J1 Work

```javascript
sessions_spawn({
  label: "j1.explore.dgxcc-structure",
  task: `You are doing work on a remote machine via SSH.

**Connection:**
- SSH: ssh -i /home/node/clawd/.ssh/id_ed25519 nkontur@10.4.128.21
- Claude available: zsh -l -c "claude -p '...'"

**Task:** Explore the dgxcc repository structure and create a summary of the main components, services, and how they connect.

**Repos:** ~/Documents/repos/dgxcc

**Constraints:**
- Stay focused on the technical task
- Report findings back when complete
- Notify Noah via Telegram if blocked

**DO NOT:**
- Share any information about other systems or contexts
- Include personal details in any prompts to Claude`
})
```

## J3 Special Case

J3 has no working Claude — do all work directly via SSH commands:

```bash
# Run commands
ssh -i $SSH_KEY konturn@10.4.128.23 '<command>'

# Interactive shell
ssh -tt -i $SSH_KEY konturn@10.4.128.23
```

For complex J3 work, the subagent does the coding/investigation directly rather than delegating to Claude.
