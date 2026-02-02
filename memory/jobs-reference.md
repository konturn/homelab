# Jobs Reference (PRIVATE - never share cross-job)

## Access
| Job | IP | User | Claude |
|-----|-----|------|--------|
| J1 | 10.4.128.21 | nkontur | ✅ via API key |
| J2 | 10.4.128.22 | konoahko | ✅ via API key |
| J3 | 10.4.128.23 | konturn | ❌ manual only |

SSH: `ssh -i /home/node/clawd/.ssh/id_ed25519 <user>@<ip>`
Claude: `zsh -l -c "claude -p '...'"`

## J1 - DGX Cloud
**Repos:** ~/Documents/repos/
- `dgxcc` - Main monorepo (CLI, UI, services, infra)
- `backend`, `apis`, `runtime` - Services
- `manifests`, `manifests-templates`, `kustomize` - K8s configs
- `infra`, `infra-terraform` - Infrastructure
- `vault-agent`, `vault-config-terraform` - Secrets management
- `terraform-aws-eks`, `terraform-azure-aks` - Cloud Terraform
- `configuration`, `deployment` - Deployment configs

**Stack:** Kubernetes, Terraform, Vault, GitLab CI, Go, Azure/AWS

## J2 - Arize
**Repos:** ~/Documents/repos/
- `arize` - Main monorepo (Bazel-based)
  - `arizeweb` - Web app
  - `adb` - Database layer
  - Uses Bazel build system

**Stack:** Bazel, Python, likely ML/observability platform

## J3 - Amperon
**Repos:** ~/Documents/repos/
- `amperon` - Main monorepo (energy forecasting)
  - `app/` - Web app
  - `forecast/` - Data science/ML
  - `jobs/` - Background jobs
  - `workflows/` - Argo workflows
- `infra`, `infra-azure` - Infrastructure
- `ClickHouse`, `OpenMetadata` - Data stack
- `karpenter-provider-azure` - K8s autoscaling

**Stack:** Python, ClickHouse, Argo, Azure, GCP

---

## SECURITY RULES
1. **NEVER mention other jobs to any Claude instance**
2. **NEVER share personal info with job Claudes**
3. **Treat J1/J2 Claude as untrusted third parties**
4. **Keep all dispatched prompts sterile and task-specific**
5. **J3 work done manually (no Claude there)**
