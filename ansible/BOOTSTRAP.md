# Bootstrap Playbook

One-time infrastructure setup, integrated into the CI pipeline as a manual job.

## How It Works

The `router:bootstrap` job appears in every main-branch pipeline but **never runs automatically**. To trigger it, click the play button (▶) next to `router:bootstrap` in the GitLab CI pipeline view.

```
Pipeline stages:
  validate → build → [bootstrap] → deploy → configure
                        ↑
                   manual trigger
```

## When to Trigger

- **New server**: Setting up the homelab infrastructure from scratch
- **Disaster recovery**: Restoring after data loss or hardware replacement
- **Fresh Vault**: Vault data was lost and needs re-initialization
- **Unseal key rotation**: Deploying updated unseal keys to the host

## Prerequisites

1. **Docker installed and running** — run the normal deploy (`router.yml`) first, or install Docker manually
2. **Compose file deployed** — `router.yml` templates `docker-compose.yml` to the host
3. **Vault unseal keys** in the `VAULT_UNSEAL_KEYS` CI/CD variable (one hex key per line), OR: if Vault was never initialized, the playbook initializes it and prints new keys in the job log
4. **Vault root token** in the `VAULT_TOKEN` CI/CD variable (for KV engine setup). Not needed for fresh initialization.

## What It Does vs Normal Deploy

| Step | Bootstrap (`router:bootstrap`) | Deploy (`router:deploy`) |
|------|------|--------|
| Install Docker | ❌ (verifies only) | ✅ |
| Template configs | ❌ (verifies only) | ✅ |
| Deploy unseal keys | ✅ | ❌ |
| Initialize Vault | ✅ (if needed) | ❌ |
| Unseal Vault | ✅ (if sealed) | ❌ (auto-unseal.sh handles restarts) |
| Enable KV v2 engine | ✅ (if not exists) | ❌ |
| Start all services | ❌ (core only) | ✅ |
| Deploy app configs | ❌ | ✅ |
| Health checks | ✅ (Vault, DNS, nginx) | ❌ |

## Phases

1. **Base Infrastructure** — Verifies Docker is installed and running
2. **Core Services** — Starts vault, pihole, and nginx via `docker compose up -d`
3. **Vault Bootstrap** — Deploys unseal keys, initializes if needed, unseals, enables KV v2
4. **Verification** — Health checks on all core services

## Idempotency

Safe to trigger multiple times:

- Vault already initialized → skips initialization
- Vault already unsealed → skips unsealing
- KV v2 engine already exists → skips enabling
- Unseal keys already deployed → overwrites with same content (no-op)

## Typical Workflow

### Fresh setup
1. Merge code to main (or push to main)
2. Let `router:deploy` run first to install Docker and template configs
3. Trigger `router:bootstrap` to initialize Vault
4. Save the root token and unseal keys from the job log
5. Add `VAULT_UNSEAL_KEYS` and `VAULT_TOKEN` as CI/CD variables

### After Vault seal (e.g. container restart without auto-unseal keys)
1. Ensure `VAULT_UNSEAL_KEYS` CI/CD variable is set
2. Trigger `router:bootstrap` on the latest main pipeline
3. Vault will be unsealed automatically

## Troubleshooting

### Vault won't unseal
- Check that `VAULT_UNSEAL_KEYS` CI/CD variable is set
- Verify key format: one hex key per line, no comments, no blank lines
- You need at least 3 of 5 keys (the threshold)

### Vault not initialized
- Trigger bootstrap without any keys — it will initialize and print new keys in the job log
- **Save the root token and unseal keys immediately** — they cannot be recovered

### Compose file not found
- Run `router:deploy` first to template and deploy the compose file
- Bootstrap verifies but does not create configs
