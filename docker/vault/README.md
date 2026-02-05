# HashiCorp Vault Setup

## Overview

HashiCorp Vault for secrets management in the homelab. Initial deployment uses file storage backend for simplicity.

## Access

- **URL:** http://vault.lab.nkontur.com:8200
- **Network:** internal (10.3.32.6)
- **UI:** Enabled at the same URL

## Initial Setup (First Time Only)

After the container starts for the first time, Vault needs to be initialized and unsealed:

```bash
# SSH to router
ssh root@router.lab.nkontur.com

# Initialize Vault (only run once!)
docker exec vault vault operator init

# Save the unseal keys and root token securely!
# You'll need 3 of 5 keys to unseal after any restart

# Unseal with 3 keys
docker exec vault vault operator unseal <key1>
docker exec vault vault operator unseal <key2>
docker exec vault vault operator unseal <key3>

# Check status
docker exec vault vault status
```

**IMPORTANT:** Store the unseal keys and root token in Bitwarden immediately!

## After Container Restart

Vault seals itself when the container restarts. You must unseal it:

```bash
docker exec vault vault operator unseal <key1>
docker exec vault vault operator unseal <key2>
docker exec vault vault operator unseal <key3>
```

## Future Enhancements

These are planned for future MRs (do NOT implement in this MR):

1. **GitLab JWT Auth** - Allow GitLab CI to authenticate without static tokens
2. **AppRole** - Service-to-service authentication
3. **Secret Migration** - Move secrets from CI/CD variables to Vault
4. **Auto-Unseal** - Consider transit auto-unseal or cloud KMS

## File Storage

Data is stored in the `vault_data` Docker volume, mounted at `/data/vault` inside the container.

## Configuration

Config file: `docker/vault/config.hcl`
Deployed to: `/persistent_data/application/vault/config/config.hcl`
