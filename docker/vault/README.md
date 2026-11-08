# HashiCorp Vault Setup

## Overview

HashiCorp Vault for secrets management in the homelab. Uses file storage backend and automatic unsealing via an entrypoint wrapper script.

## Access

- **URL:** https://vault.lab.nkontur.com:8200
- **Network:** internal (10.3.32.6)
- **UI:** Enabled at the same URL

## Auto-Unseal

Vault automatically unseals on container startup using a wrapper entrypoint script (`auto-unseal.sh`). The script:

1. Starts Vault server in the background
2. Waits for the API to become available (up to 60 seconds)
3. Checks if Vault is sealed
4. If sealed, applies unseal keys from a mounted file
5. Waits on the Vault process (keeps container running)

### Unseal Keys

The unseal keys are managed via the `VAULT_UNSEAL_KEYS` CI/CD variable (protected). `ansible/bootstrap.yml` (the manual `router:bootstrap` CI job) writes them to:
```
/persistent_data/application/vault/unseal/unseal-keys
```

This file contains hex-encoded unseal keys, one per line. Only 3 keys (the threshold) are needed. The directory is created by Ansible with mode `0700` (root only), and the file with mode `0600`.

Only `bootstrap.yml` writes this file; the normal `router:deploy` does not touch it. Once it is in place, every container restart auto-unseals with no manual step.

### Security Considerations

- The unseal keys are stored as a protected CI/CD variable (only available on protected branches).
- On disk, the file is readable only by root (mode 0600 in a 0700 directory).
- The keys are mounted read-only into the container.
- For production environments, use Transit auto-unseal or a Cloud KMS instead.

### Graceful Degradation

If the unseal keys file is missing or invalid, Vault still starts normally but remains sealed. You can manually unseal:

```bash
docker exec vault vault operator unseal <key1>
docker exec vault vault operator unseal <key2>
docker exec vault vault operator unseal <key3>
```

## Initial Setup (First Time Only)

`bootstrap.yml` initializes an uninitialized Vault itself (5 shares, threshold 3) and saves the result to `/persistent_data/application/vault/init-output.json` (0600); back that file up off-box. By hand:

```bash
# SSH to router
ssh root@router.lab.nkontur.com

# Initialize Vault (only run once!)
docker exec vault vault operator init

# Save the unseal keys and root token securely!
# Then add 3+ hex keys to the VAULT_UNSEAL_KEYS CI/CD variable (newline-separated)
```

## Configuration

- **Config file:** `docker/vault/config.hcl` (deployed by Ansible)
- **Auto-unseal script:** `docker/vault/auto-unseal.sh` (deployed by Ansible)
- **Unseal keys:** `VAULT_UNSEAL_KEYS` CI/CD variable → deployed to `/persistent_data/application/vault/unseal/unseal-keys`

## File Storage

Data is stored in the `vault_data` Docker volume (`docker_vault_data`, on the host `/persistent_data/docker/volumes/docker_vault_data/_data`), backed by `/vault/file` inside the container. restic backs it up with the other Docker volumes; the `unseal/` directory is excluded from offsite backups.

## Auth and secrets

GitLab JWT auth, AppRole, policies and the SSH CA are managed by `terraform/vault/` (`vault:configure` CI job). Service secrets live in the `homelab/` KV v2 mount, which neither Terraform nor `bootstrap.yml` creates. Roles, paths and rotation: `docs/SECRET_ROTATION.md`.
