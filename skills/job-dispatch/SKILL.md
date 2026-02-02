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

Kubernetes-style IaaS platform for managing NVIDIA cloud infrastructure.

**Key Repos:**
| Repo | Purpose |
|------|---------|
| `dgxcc` | Main monorepo: CLI, UI, services (Go+React), Temporal workflows |
| `apis` | Protocol Buffer definitions → generates CRDs and Go types |
| `backend` | API machinery: dgxc-apiserver (serves CRDs), dgxc-proxy (auth/routing) |
| `infra` | Layered controllers (L0→L1→L2) for cloud provisioning |
| `manifests` | GitOps cluster configs, Gomplate templating, Vault secrets |

**Architecture:**
```
CLI/UI → dgxc-proxy → dgxc-apiserver (CRDs) → Controllers (L0/L1/L2) → Cloud (AWS/GCP/Azure/OCI)
                                                      ↓
                                              ArgoCD ← manifests repo
```

**Stack:** Go 1.24+, Kubernetes API machinery, Temporal, NATS, PostgreSQL, Terraform, ArgoCD, Vault

### J2 (Arize)

ML Observability platform — monitors, debugs, and improves ML models in production.

**Key Directories in `arize/`:**
| Directory | Purpose |
|-----------|---------|
| `arizeweb/` | React/TypeScript frontend with Relay/GraphQL |
| `copilot/` | AI assistant system (Python) with routing/planning |
| `adb/` | Java-based Arrow database for segments |
| `go/` | Go services (backend APIs, operators) |
| `python/` | Python services (UMAP, metrics, SDK) |
| `proto/` | Protobuf definitions (20+ services) |
| `manifests/` | K8s deployments (90+ components) |
| `phoenix_cloud/` | LLM tracing product (per-tenant pods) |

**Data Flow:**
```
SDK → Receiver → Gazette (streaming) → Model Discovery → Druid Loader → Druid
                                              ↓
                                    Joiner → Conclusion Records
```

**Stack:** Bazel, Go 1.25, Python, Java, gRPC/Protobuf, Druid, Gazette, React/Relay, Kubernetes

### J3 (Amperon)

Energy forecasting and analytics platform.

**Key Repos:**
| Repo | Purpose |
|------|---------|
| `amperon` | Main app: Flask backend, ML forecasting, Argo jobs |
| `infra` | GCP infrastructure (legacy) |
| `infra-azure` | Azure infrastructure (primary) — Terraform + Atlantis |

**Key Directories in `amperon/`:**
| Directory | Purpose |
|-----------|---------|
| `app/` | Flask web app + React frontend |
| `client/` | Next.js modern frontend |
| `forecast/` | ML models (95+ types): LightGBM, PyTorch, TBATS |
| `jobs/` | Background jobs: scrapers (43+), weather (32), integrations |
| `workflows/` | Argo Workflow definitions |

**Architecture:**
```
ingress → Flask/Next.js → MySQL/ClickHouse
                              ↑
                    Argo Workflows (forecast jobs, scrapers, ETL)
```

**Stack:** Python/Flask, Next.js, LightGBM/PyTorch, Argo Workflows, ClickHouse, MySQL, Terraform/Atlantis, ArgoCD

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
