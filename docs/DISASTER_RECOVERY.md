# Disaster Recovery

Complete guide to recovering the homelab from bare metal.

**Estimated Recovery Time:** 1–3 hours (depending on data restore volume)

---

## Overview

Recovery is a **single-command process** after the base OS and disks are ready:

```
1. Install Ubuntu 22.04, unlock disks, import ZFS
2. Run: bash scripts/bootstrap.sh --restore
3. Wait. Done.
```

The bootstrap script handles everything: Docker, networking, data restore,
GitLab, Vault unsealing, CI/CD variable injection, runner registration, and
triggering the first pipeline. Zero manual post-steps.

---

## Recovery Layers (Automated)

```
bootstrap.sh (single shot, on router)
  ├── Steps 1-3:  Docker, networks, restic
  ├── Step 4:     Data restore from Backblaze B2
  ├── Steps 5-6:  Start GitLab, wait for health
  ├── Step 7:     Install gitlab-runner
  ├── Step 8:     Start Vault container
  ├── Step 9:     Unseal Vault (keys from restored data or prompt)
  ├── Step 10:    Set CI/CD variables via GitLab API
  ├── Step 11:    Register runner
  └── Step 12:    Trigger pipeline → wait for green
```

Previous manual steps (register runner, set CI vars, trigger pipeline) are
now fully automated inside the script.

---

## What You Need (Store Offsite)

| Secret | Purpose |
|--------|---------|
| `B2_ACCOUNT_ID` | Backblaze B2 application key ID |
| `B2_ACCOUNT_KEY` | Backblaze B2 application key |
| `RESTIC_REPOSITORY` | Backup repo URL |
| `RESTIC_PASSWORD` | Restic repository encryption password |
| Vault unseal keys (3 of 5) | Unseal Vault after restore |
| LUKS password | Decrypt data drives |
| GitLab PAT (api scope) | Bootstrap API calls (or create one post-restore) |

**Where to keep these:** Encrypted file in cloud storage (1Password, Google
Drive, etc.) or printed in a safe deposit box.

---

## Phase 1: Base OS (~30 min)

1. Install Ubuntu Server 22.04 LTS on the boot NVMe
2. Enable root SSH, update system
3. Install essentials: `apt install -y git curl wget vim htop`

### Minimal Network Config

Create `/etc/netplan/01-bootstrap.yaml`:

```yaml
network:
  version: 2
  renderer: networkd
  ethernets:
    enx6c1ff76b2ec9:
      dhcp4: yes
    enp4s0f0:
      dhcp4: no
    enp4s0f1:
      dhcp4: no
  bonds:
    bond0:
      interfaces: [enp4s0f0, enp4s0f1]
      parameters:
        mode: 802.3ad
      addresses: [10.4.0.1/32]
      routes:
        - to: 10.4.0.0/16
          scope: link
```

```bash
netplan apply
```

### Disk Setup

```bash
apt install -y zfsutils-linux
cryptsetup luksOpen /dev/sdX cryptdata1   # repeat for each drive
zpool import mpool
zpool import persistent_data
zfs mount -a
```

---

## Phase 2: Bootstrap (~30 min + restore time)

### Option A: Restore from Backup (recommended)

```bash
cd /root
git clone https://github.com/<mirror>/homelab.git   # or restore repo first
cd homelab
bash scripts/bootstrap.sh --restore
```

The script will:
1. Install Docker, create macvlan networks, install restic
2. **Prompt for B2 credentials** and restore persistent data
3. Start GitLab with restored data, wait for health
4. Install and start Vault, unseal using restored keys (or prompt)
5. Set all CI/CD variables via GitLab API
6. Register the runner
7. Trigger the first pipeline and wait for green

You'll be prompted for:
- B2 credentials (if not in env)
- Which data paths to restore
- Vault unseal keys (if auto-unseal file not found in restored data)
- GitLab personal access token (for API calls)
- Vault root token (for CI/CD variable injection)

### Option B: Fresh Install (no backup)

```bash
bash scripts/bootstrap.sh --fresh
```

Additional prompts for every Vault secret (or use `--secrets-file`):

```bash
bash scripts/bootstrap.sh --fresh --secrets-file /path/to/secrets.env.gpg
```

The secrets file is GPG-encrypted, containing `KEY=VALUE` pairs. See
[Appendix A](#appendix-a-secrets-file-format) for the format.

---

## Phase 3: There Is No Phase 3

The bootstrap script triggers the CI pipeline automatically. When it
completes, you should see:

```
[bootstrap] 🎉 Pipeline #N passed! First run is GREEN. ✓
[bootstrap] 🎉 BOOTSTRAP COMPLETE 🎉
```

If the pipeline fails, the script will print the URL. Fix the issue and
re-run the pipeline from GitLab UI.

---

## Additional Data Restore (Optional)

Large media files can be restored after the initial bootstrap:

```bash
export AWS_ACCESS_KEY_ID="<B2_ACCOUNT_ID>"
export AWS_SECRET_ACCESS_KEY="<B2_ACCOUNT_KEY>"
export RESTIC_REPOSITORY="s3:s3.us-east-005.backblazeb2.com/nkontur-homelab"
export RESTIC_PASSWORD="<restic-password>"

restic restore latest --target / --include /mpool/nextcloud
restic restore latest --target / --include /mpool/plex/config
restic restore latest --target / --include /mpool/plex/Photos
restic restore latest --target / --include /mpool/plex/Family
```

Restart services after: `docker compose down && docker compose up -d`

---

## Verification

```bash
docker ps                                                    # All containers
dig @10.3.32.2 gitlab.lab.nkontur.com                       # DNS
curl -sk https://vault.lab.nkontur.com:8200/v1/sys/health   # Vault unsealed
curl -I https://nkontur.com                                  # External access
ping -c 1 10.2.32.1  # external nginx
ping -c 1 10.3.32.2  # pihole
ping -c 1 10.6.32.3  # mosquitto
systemctl status restic-backup.timer                         # Backups
```

---

## Appendix A: Secrets File Format

For `--secrets-file`, create a plaintext file with `KEY=VALUE` pairs:

```bash
# Vault seed secrets — env var names match the pattern:
# VAULT_SEED_<PATH>_<FIELD> where path separators become underscores

VAULT_SEED_API_KEYS_ACLAWDEMY_API_KEY=xxx
VAULT_SEED_API_KEYS_ANTHROPIC_API_KEY=xxx
VAULT_SEED_BACKUP_BACKBLAZE_ACCESS_KEY_ID=xxx
VAULT_SEED_BACKUP_BACKBLAZE_SECRET_ACCESS_KEY=xxx
VAULT_SEED_BACKUP_RESTIC_PASSWORD=xxx
# ... (all paths from ansible/roles/fetch-vault-secrets/defaults/main.yml)

# Bootstrap credentials
GITLAB_BOOTSTRAP_TOKEN=xxx
B2_ACCOUNT_ID=xxx
B2_ACCOUNT_KEY=xxx
RESTIC_REPOSITORY=s3:s3.us-east-005.backblazeb2.com/nkontur-homelab
RESTIC_PASSWORD=xxx
```

Encrypt with GPG: `gpg -c secrets.env` → produces `secrets.env.gpg`

---

## Quick Reference Card

```
DISASTER RECOVERY QUICK REFERENCE

1. Install Ubuntu Server 22.04 on boot drive
2. Minimal netplan (WAN DHCP + bond0)
3. Unlock LUKS: cryptsetup luksOpen /dev/sdX name
4. Import ZFS: zpool import mpool && zpool import persistent_data
5. Clone repo: git clone <homelab repo> && cd homelab
6. Run: bash scripts/bootstrap.sh --restore
7. Answer prompts (B2 creds, unseal keys, GitLab token)
8. Wait for "BOOTSTRAP COMPLETE"

Secrets location: ___________________________
Last tested: ___________________________
```
