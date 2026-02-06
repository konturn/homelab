# Disaster Recovery

Complete guide to recovering the homelab from bare metal.

**Estimated Recovery Time:** 2–4 hours (depending on data restore volume)

---

## Recovery Layers

```
Layer 0 — scripts/bootstrap.sh (manual, on router)
  Install Docker, restore data from Backblaze B2, start GitLab + Runner

Layer 1 — router:bootstrap CI job (manual trigger)
  Vault, Pi-hole, nginx, switch config

Layer 2 — Normal CI deploy pipeline
  Full stack (all containers, networking, configs)
```

Each layer depends on the one below it. Work bottom-up.

Data restore happens in Layer 0 because GitLab's own data (repos, users, CI
config) is in the backup. Without it, GitLab starts empty and there's no
pipeline to run.

---

## What You Need (Store Offsite)

These values are everything needed for full recovery:

| Secret | Purpose |
|--------|---------|
| `B2_ACCOUNT_ID` | Backblaze B2 application key ID |
| `B2_ACCOUNT_KEY` | Backblaze B2 application key |
| `RESTIC_REPOSITORY` | Backup repo URL (e.g. `s3:s3.us-east-005.backblazeb2.com/nkontur-homelab`) |
| `RESTIC_PASSWORD` | Restic repository encryption password |
| Vault unseal keys (3 of 5) | Unseal Vault for secrets management |
| LUKS password | Decrypt data drives |

**Where to keep these:** Encrypted file in cloud storage (1Password, Google Drive, etc.) or printed in a safe deposit box. Do NOT rely solely on GitLab CI variables — they live on the router you're recovering.

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
# Install ZFS
apt install -y zfsutils-linux

# Unlock LUKS drives
cryptsetup luksOpen /dev/sdX cryptdata1
# ... repeat for each encrypted drive

# Import ZFS pools
zpool import mpool
zpool import persistent_data
zfs mount -a
```

---

## Phase 2: Layer 0 — Bootstrap Script (~30 min + restore time)

```bash
cd /root
git clone https://github.com/<mirror>/homelab.git  # or restore from backup first
cd homelab
bash scripts/bootstrap.sh
```

The script walks through 7 steps in order:

1. **Install Docker + compose plugin**
2. **Create macvlan networks** (internal, external, iot, mgmt)
3. **Install restic**
4. **Restore data from Backblaze B2** — interactive prompts for each path.
   You'll need `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY`, `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`.
   Say **yes** to at least `/persistent_data/application` — this contains GitLab's data.
5. **Start GitLab** — boots with restored data (repos, users, CI config intact)
6. **Wait for GitLab health**
7. **Install gitlab-runner**

### After bootstrap.sh completes:

1. **Verify GitLab has your projects** — navigate to `http://<router-ip>`
   (If data was restored, the root/homelab project and all config should be there)
2. **Register the runner** (if runner config wasn't restored):
   ```bash
   gitlab-runner register \
     --url https://gitlab.lab.nkontur.com/ \
     --executor docker \
     --docker-image ubuntu:20.04 \
     --docker-network-mode host
   ```
3. **Push the repo** (if needed):
   ```bash
   cd /root/homelab
   git remote set-url origin https://gitlab.lab.nkontur.com/root/homelab.git
   git push -u origin main
   ```
4. **Configure CI/CD variables** in GitLab (Settings → CI/CD → Variables):
   - `ROUTER_PRIVATE_KEY_BASE64`
   - `VAULT_UNSEAL_KEYS`, `VAULT_TOKEN`
   - `PIHOLE_PASSWORD`, `OMAPI_SECRET`
   - `B2_ACCOUNT_ID`, `B2_ACCOUNT_KEY`, `RESTIC_REPOSITORY`, `RESTIC_PASSWORD`
   - `TAILSCALE_AUTH_KEY`, `TAILSCALE_API_TOKEN`
   - `CLOUDFLARE_API_KEY`, `CLOUDFLARE_ZONE_ID`, `NAMESILO_API_KEY`
   - `GRAFANA_TOKEN`
   - `GITLAB_RUNNER_TOKEN`, `IMAGES_GITLAB_RUNNER_TOKEN`

---

## Phase 3: Layer 1 — Bootstrap CI Job (~15 min)

1. Go to CI/CD → Pipelines in GitLab
2. Find the `router:bootstrap` job (manual trigger)
3. Click the play button

This runs `ansible/bootstrap.yml` which:
- Configures Docker daemon
- Starts core services (Vault, Pi-hole, nginx, GitLab via compose)
- Initializes and unseals Vault
- Configures Pi-hole DNS records
- Pushes switch VLAN config (if switch is reachable)
- Runs health checks

---

## Phase 4: Layer 2 — Full Deploy

Once Layer 1 is healthy, run the normal CI pipeline:

1. Push a commit (or re-run the pipeline)
2. The `router:deploy` job runs the full Ansible playbook
3. All remaining services come up

---

## Additional Data Restore

The bootstrap script restores the critical paths interactively. For large data
that you may want to restore later, run manually on the router:

```bash
export AWS_ACCESS_KEY_ID="<B2_ACCOUNT_ID>"
export AWS_SECRET_ACCESS_KEY="<B2_ACCOUNT_KEY>"
export RESTIC_REPOSITORY="s3:s3.us-east-005.backblazeb2.com/nkontur-homelab"
export RESTIC_PASSWORD="<restic-password>"

# Nextcloud data (large)
restic restore latest --target / --include /mpool/nextcloud

# Plex metadata
restic restore latest --target / --include /mpool/plex/config

# Photos and family videos
restic restore latest --target / --include /mpool/plex/Photos
restic restore latest --target / --include /mpool/plex/Family
```

Restart services after restoring: `cd /persistent_data/application/ansible_state && docker compose down && docker compose up -d`

---

## Verification

```bash
# All containers running
docker ps

# DNS resolves
dig @10.3.32.2 gitlab.lab.nkontur.com

# Vault unsealed
curl -sk https://vault.lab.nkontur.com:8200/v1/sys/health | jq .sealed

# External access
curl -I https://nkontur.com

# VLANs up
ping -c 1 10.2.32.1  # external nginx
ping -c 1 10.3.32.2  # pihole
ping -c 1 10.6.32.3  # mosquitto

# Backups scheduled
systemctl status restic-backup.timer
```

---

## Quick Reference Card

```
1. Install Ubuntu 22.04 on boot drive
2. Minimal netplan (WAN DHCP + bond0)
3. Unlock LUKS, import ZFS pools
4. Run scripts/bootstrap.sh
   - When prompted, restore from B2 (need: B2_ACCOUNT_ID, B2_ACCOUNT_KEY,
     RESTIC_REPOSITORY, RESTIC_PASSWORD)
   - Restore /persistent_data/application at minimum (has GitLab data)
5. Verify GitLab has projects, register runner if needed
6. Push repo, set CI variables
7. Trigger router:bootstrap CI job
8. Run normal deploy pipeline
9. Verify: docker ps, DNS, Vault, external access
```
