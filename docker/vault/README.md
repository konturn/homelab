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

### Unseal Keys File

The unseal keys are stored at:
```
/persistent_data/application/vault/unseal/unseal-keys
```

This file contains hex-encoded unseal keys, one per line. Only 3 keys (the threshold) are needed, though all 5 can be stored. The directory is created by Ansible with mode `0700` (root only).

**Important:** The unseal keys file is NOT stored in git. It must be placed manually on the router:

```bash
# SSH to router
ssh root@router.lab.nkontur.com

# Create the unseal keys file (use your actual keys)
mkdir -p /persistent_data/application/vault/unseal
cat > /persistent_data/application/vault/unseal/unseal-keys << 'EOF'
<hex-encoded-unseal-key-1>
<hex-encoded-unseal-key-2>
<hex-encoded-unseal-key-3>
EOF

# Lock down permissions
chmod 600 /persistent_data/application/vault/unseal/unseal-keys
chmod 700 /persistent_data/application/vault/unseal
```

### Security Considerations

- The unseal keys file is on the same machine as Vault. This is a pragmatic tradeoff for a homelab: it protects against network attackers and accidental container restarts, but not against root compromise of the host.
- The file is readable only by root (mode 0600 in a 0700 directory).
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

After the container starts for the first time, Vault needs to be initialized:

```bash
# SSH to router
ssh root@router.lab.nkontur.com

# Initialize Vault (only run once!)
docker exec vault vault operator init

# Save the unseal keys and root token securely!
# Then place 3+ keys in the unseal-keys file (see above)
```

## Configuration

- **Config file:** `docker/vault/config.hcl` (deployed by Ansible)
- **Auto-unseal script:** `docker/vault/auto-unseal.sh` (deployed by Ansible)
- **Unseal keys:** `/persistent_data/application/vault/unseal/unseal-keys` (manual placement)

## File Storage

Data is stored in the `vault_data` Docker volume, backed by `/vault/file` inside the container.

## Future Enhancements

1. **GitLab JWT Auth** - Allow GitLab CI to authenticate without static tokens
2. **AppRole** - Service-to-service authentication
3. **Secret Migration** - Move secrets from CI/CD variables to Vault
