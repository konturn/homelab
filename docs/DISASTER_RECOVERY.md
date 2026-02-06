# Disaster Recovery

Complete guide to recovering the homelab from bare metal.

**Estimated Recovery Time:** 2–4 hours (depending on data restore volume)

---

## Recovery Layers

```
Layer 0 — scripts/bootstrap.sh (manual, on router)
  Ubuntu + Docker + GitLab + Runner
  Optional: restore data from Backblaze B2 (interactive prompt)

Layer 1 — router:bootstrap CI job (manual trigger)
  Vault, Pi-hole, nginx, switch config

Layer 1.5 — router:restore CI job (manual trigger)
  Restore persistent data from Backblaze B2 restic backups

Layer 2 — Normal CI deploy pipeline
  Full stack (all containers, networking, configs)
```

Layers 0 → 1 → 1.5 → 2. Data restore (1.5) can also happen during Layer 0 if
GitLab data itself needs recovery.

---

## What You Need (Store Offsite)

These 6 values are everything needed for full recovery:

| Secret | CI Variable | Purpose |
|--------|-------------|---------|
| Backblaze B2 key ID | `B2_ACCOUNT_ID` | Access backup storage |
| Backblaze B2 access key | `B2_ACCOUNT_KEY` | Access backup storage |
| Restic repository URL | `RESTIC_REPOSITORY` | Locate backup repo |
| Restic repository password | `RESTIC_PASSWORD` | Decrypt backups |
| Vault unseal keys (3 of 5) | `VAULT_UNSEAL_KEYS` | Unseal Vault |
| LUKS password | — | Decrypt data drives |

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

## Phase 2: Layer 0 — Bootstrap Script (~20 min)

```bash
cd /root
git clone https://github.com/<mirror>/homelab.git  # or restore from backup
cd homelab
bash scripts/bootstrap.sh
```

This installs Docker, creates networks, starts GitLab, and installs gitlab-runner.
The script will also prompt to restore data from Backblaze B2 — say **yes** if this is a
fresh install and you need GitLab data/configs back. You'll need `B2_ACCOUNT_ID`,
`B2_ACCOUNT_KEY`, `RESTIC_REPOSITORY`, and `RESTIC_PASSWORD`.

### After bootstrap.sh completes:

1. **Set GitLab root password** — navigate to `http://<router-ip>` in a browser
2. **Create the `root/homelab` project** in GitLab
3. **Register the runner:**
   ```bash
   gitlab-runner register \
     --url https://gitlab.lab.nkontur.com/ \
     --executor docker \
     --docker-image ubuntu:20.04 \
     --docker-network-mode host
   ```
4. **Push the repo:**
   ```bash
   cd /root/homelab
   git remote set-url origin https://gitlab.lab.nkontur.com/root/homelab.git
   git push -u origin main
   ```
5. **Configure CI/CD variables** in GitLab (Settings → CI/CD → Variables):
   - `ROUTER_PRIVATE_KEY_BASE64`
   - `VAULT_UNSEAL_KEYS`
   - `VAULT_TOKEN`
   - `PIHOLE_PASSWORD`
   - `OMAPI_SECRET`
   - `BACKBLAZE_ACCESS_KEY_ID`, `BACKBLAZE_SECRET_ACCESS_KEY`
   - `RESTIC_PASSWORD`
   - `TAILSCALE_AUTH_KEY`, `TAILSCALE_API_TOKEN`
   - `CLOUDFLARE_API_KEY`, `CLOUDFLARE_ZONE_ID`
   - `NAMESILO_API_KEY`
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

## Phase 4: Data Restore (~1–4 hours)

Data restore can happen at two points depending on your situation:

**Option A: During bootstrap.sh (Phase 2)**
The bootstrap script prompts to restore from Backblaze B2 before starting GitLab.
Use this when GitLab data itself needs recovery.

**Option B: Via CI job (after Phase 3)**
Trigger the `router:restore` manual job in the bootstrap stage. This restores
`/persistent_data/application` and `/persistent_data/docker/volumes` automatically.

Required CI variables for `router:restore`:
- `RESTIC_REPOSITORY` — e.g. `s3:s3.us-east-005.backblazeb2.com/nkontur-homelab`
- `RESTIC_PASSWORD` — Repository encryption password
- `B2_ACCOUNT_ID` — Backblaze B2 application key ID
- `B2_ACCOUNT_KEY` — Backblaze B2 application key

For large data (Nextcloud, Plex), restore manually on the router:

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

After any restore, restart services:

```bash
cd /persistent_data/application/ansible_state
docker compose down && docker compose up -d
```

---

## Phase 5: Layer 2 — Full Deploy

Once core services and data are restored, run the normal CI pipeline:

1. Push a commit (or re-run the pipeline)
2. The `router:deploy` job runs the full Ansible playbook
3. All remaining services come up

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
4. Run scripts/bootstrap.sh (will offer to restore from B2)
5. Set GitLab password, create project, register runner
6. Push repo, set CI variables (including B2_ACCOUNT_ID, B2_ACCOUNT_KEY, RESTIC_*)
7. Trigger router:bootstrap CI job
8. Trigger router:restore CI job (if data not already restored in step 4)
9. Run normal deploy pipeline
10. Verify: docker ps, DNS, Vault, external access
```
