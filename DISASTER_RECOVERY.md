# Disaster Recovery Runbook

Complete guide to recovering the homelab from bare metal. Target audience: Noah with an internet connection and a basic Ubuntu USB stick.

**Estimated Total Recovery Time:** 2-6 hours (depending on data restore volume)

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Phase 1: Base OS Install](#phase-1-base-os-install-30-min)
3. [Phase 2: Network Bootstrap](#phase-2-network-bootstrap-manual-15-min)
4. [Phase 3: Disk Setup](#phase-3-disk-setup-30-min)
5. [Phase 4: Run Ansible](#phase-4-run-ansible-20-min)
6. [Phase 5: Data Restore](#phase-5-data-restore-1-4-hours)
7. [Phase 6: Verification](#phase-6-verification-15-min)
8. [Appendix A: Secrets Reference](#appendix-a-secrets-reference)
9. [Appendix B: Known Gaps](#appendix-b-known-gaps--future-improvements)

---

## Prerequisites

### What You Need

- [ ] Ubuntu Server 22.04 LTS USB installer
- [ ] Physical access to router hardware (or IPMI access from 10.4.128.7)
- [ ] Internet connection (separate from the homelab network)
- [ ] Access to secrets (see [Appendix A](#appendix-a-secrets-reference))
- [ ] Another computer to SSH from and run Ansible

### Hardware Reference

| Component | Details |
|-----------|---------|
| Router | Custom server with dual 10GbE NICs (bond0) |
| WAN NIC | `enx6c1ff76b2ec9` (USB Ethernet adapter) |
| IPMI | 10.4.128.7 (requires separate network access) |
| Boot drive | NVMe (OS only) |
| Data drives | LUKS-encrypted, ZFS pools |

---

## Phase 1: Base OS Install (~30 min)

### 1.1 Boot from USB

1. Connect USB installer to router
2. Access IPMI console or connect monitor/keyboard
3. Boot from USB (F11 or BIOS boot menu)

### 1.2 Ubuntu Server Installation

Choose these options during install:

| Setting | Value |
|---------|-------|
| Language | English |
| Keyboard | US |
| Install type | Ubuntu Server (minimized) |
| Network | DHCP on WAN interface initially |
| Storage | **Use entire NVMe for OS only** - Do NOT touch data drives |
| Username | `root` enabled, or create user and enable root later |
| SSH | Install OpenSSH server |
| Snaps | None |

### 1.3 Post-Install Basics

```bash
# If you created a non-root user, enable root
sudo passwd root
sudo sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo systemctl restart sshd

# Update system
apt update && apt upgrade -y

# Install essentials
apt install -y git curl wget vim htop
```

---

## Phase 2: Network Bootstrap (MANUAL, ~15 min)

⚠️ **CRITICAL**: Ansible cannot run until basic networking is configured. This step is manual.

### 2.1 Identify Network Interfaces

```bash
ip link show
```

Expected interfaces:
- `enx6c1ff76b2ec9` — WAN (USB Ethernet, gets DHCP from ISP)
- `enp4s0f0`, `enp4s0f1` — Bond members (to managed switch)
- `enp2s0f1` — Direct connection (10.100.0.2/24)

### 2.2 Create Minimal Netplan

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

Apply:

```bash
netplan apply
```

### 2.3 Verify Connectivity

```bash
# Should have internet
ping -c 3 8.8.8.8

# Bond should be up
ip addr show bond0

# Should be able to reach the switch (if powered)
ping -c 3 10.4.128.3
```

**Note**: Full VLAN configuration happens via Ansible. This minimal config just gets us SSH-able and able to pull the repo.

---

## Phase 3: Disk Setup (~30 min)

### 3.1 Identify Data Drives

```bash
lsblk
```

Look for your data drives (likely `/dev/sda`, `/dev/sdb`, etc.). The NVMe with the OS should be separate.

### 3.2 Unlock LUKS Encrypted Drives

For each encrypted drive:

```bash
# List LUKS devices
blkid | grep crypto_LUKS

# Unlock each device (you'll need LUKS_PASSWORD)
cryptsetup luksOpen /dev/sdX cryptdata1
cryptsetup luksOpen /dev/sdY cryptdata2
# etc.
```

**LUKS Password**: Decode from `LUKS_PASSWORD_BASE64`:
```bash
echo "$LUKS_PASSWORD_BASE64" | base64 -d
```

### 3.3 Import ZFS Pools

```bash
# Install ZFS
apt install -y zfsutils-linux

# Scan for pools
zpool import

# Import pools (adjust names based on your setup)
zpool import mpool
zpool import persistent_data

# Verify
zpool status
zfs list
```

### 3.4 Verify Mount Points

Expected mounts:
- `/mpool` — Media, Nextcloud data
- `/persistent_data` — Application configs, Docker volumes

```bash
# Check mounts
df -h | grep -E "mpool|persistent"
```

If not auto-mounted, check `/etc/fstab` or mount manually:

```bash
zfs mount -a
```

---

## Phase 4: Run Ansible (~20 min)

### 4.1 Clone the Repository

**Option A: From GitLab (if restored or mirrored)**
```bash
cd /root
git clone https://gitlab.lab.nkontur.com/root/homelab.git
cd homelab
```

**Option B: From backup/mirror (if GitLab not available)**
```bash
# See Appendix B for repo mirror setup
# For now, restore from restic first (Phase 5) then clone from restored GitLab
```

### 4.2 Install Ansible

```bash
apt install -y python3-pip
pip3 install ansible
ansible-galaxy install -r ansible/requirements.yml
```

### 4.3 Prepare Secrets

Export all required environment variables. These are normally in GitLab CI.

```bash
# Core infrastructure
export ROUTER_PRIVATE_KEY_BASE64="<from secure storage>"
export LUKS_PASSWORD_BASE64="<from secure storage>"

# Backup access
export BACKBLAZE_ACCESS_KEY_ID="<from secure storage>"
export BACKBLAZE_SECRET_ACCESS_KEY="<from secure storage>"
export RESTIC_PASSWORD="<from secure storage>"

# Network services
export OMAPI_SECRET="<from secure storage>"
export TAILSCALE_AUTH_KEY="<from secure storage>"

# DNS/SSL
export CLOUDFLARE_API_KEY="<from secure storage>"
export CLOUDFLARE_ZONE_ID="<from secure storage>"
export NAMESILO_API_KEY="<from secure storage>"

# Optional services
export GRAFANA_TOKEN="<from secure storage>"
export TAILSCALE_API_TOKEN="<from secure storage>"
```

### 4.4 Create SSH Key

```bash
# Decode the router private key
echo "$ROUTER_PRIVATE_KEY_BASE64" | base64 -d > /root/.ssh/id_rsa
chmod 600 /root/.ssh/id_rsa
```

### 4.5 Run Ansible Locally

For disaster recovery, run Ansible against localhost:

```bash
cd /root/homelab

# Run the router playbook (use --connection=local for same host)
ansible-playbook -i ansible/inventory.yml ansible/router.yml \
  --connection=local \
  -e ansible_host=127.0.0.1
```

This will:
- Configure all network VLANs
- Set up Docker and Docker Compose
- Deploy all container configurations
- Configure DHCP, DNS, iptables
- Set up Wireguard VPN
- Configure backup schedules

### 4.6 Verify Services Starting

```bash
# Check Docker containers
docker ps

# Should see containers starting up
# Some will fail until data is restored (Phase 5)
```

---

## Phase 5: Data Restore (~1-4 hours)

### 5.1 Configure Restic

```bash
# Set restic environment
export AWS_ACCESS_KEY_ID="$BACKBLAZE_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$BACKBLAZE_SECRET_ACCESS_KEY"
export RESTIC_PASSWORD="$RESTIC_PASSWORD"
export RESTIC_REPOSITORY="s3:s3.us-east-005.backblazeb2.com/nkontur-homelab"
```

### 5.2 Check Available Snapshots

```bash
restic snapshots
```

Note the snapshot ID for the most recent good backup.

### 5.3 Restore Order (IMPORTANT)

Restore in this order to minimize service interdependencies:

#### Priority 1: Service Configurations
```bash
# Application configs (GitLab, Home Assistant, Bitwarden, etc.)
restic restore latest --target / --include /persistent_data/application

# Verify
ls -la /persistent_data/application/
```

#### Priority 2: Database State
```bash
# Docker volumes (PostgreSQL, MariaDB, Redis, etc.)
restic restore latest --target / --include /persistent_data/docker/volumes

# Verify
ls -la /persistent_data/docker/volumes/
```

#### Priority 3: User Files
```bash
# Nextcloud data (largest, takes longest)
restic restore latest --target / --include /mpool/nextcloud

# Verify
ls -la /mpool/nextcloud/
```

#### Priority 4: Media Metadata
```bash
# Plex configuration and metadata
restic restore latest --target / --include /mpool/plex/config

# Photos and family videos (optional, large)
restic restore latest --target / --include /mpool/plex/Photos
restic restore latest --target / --include /mpool/plex/Family
```

### 5.4 Restart Services

After restoring data:

```bash
cd /persistent_data/application/ansible_state
docker compose down
docker compose up -d

# Watch logs
docker compose logs -f
```

---

## Phase 6: Verification (~15 min)

### 6.1 Core Services Checklist

| Service | Check Command | Expected |
|---------|---------------|----------|
| Docker | `docker ps` | All containers running |
| DNS | `dig @localhost google.com` | Resolves |
| DHCP | Check client gets IP | 10.x.x.x range |
| GitLab | `curl -I https://gitlab.lab.nkontur.com` | 200 OK |
| Nginx | `curl -I https://nkontur.com` | 200 OK |

### 6.2 Network Verification

```bash
# VLANs are up
ip addr | grep bond0

# Can reach each VLAN
ping -c 1 10.2.32.1  # External nginx
ping -c 1 10.3.32.2  # Internal pihole
ping -c 1 10.6.32.3  # IoT mosquitto
```

### 6.3 Backup Verification

```bash
# Ensure backups are scheduled
systemctl status restic-backup.timer

# Run a test backup
restic backup --dry-run /persistent_data/application
```

### 6.4 External Access

- [ ] Can access `https://nkontur.com` from internet
- [ ] Tailscale shows router as connected
- [ ] Wireguard VPN connects (test from phone)

---

## Appendix A: Secrets Reference

### Required Secrets

| Secret | Purpose | Where Used |
|--------|---------|------------|
| `LUKS_PASSWORD_BASE64` | Decrypt data drives | Phase 3 (disk unlock) |
| `ROUTER_PRIVATE_KEY_BASE64` | SSH key for Ansible | Phase 4 (Ansible) |
| `RESTIC_PASSWORD` | Decrypt backups | Phase 5 (restore) |
| `BACKBLAZE_ACCESS_KEY_ID` | B2 storage access | Phase 5 (restore) |
| `BACKBLAZE_SECRET_ACCESS_KEY` | B2 storage auth | Phase 5 (restore) |
| `OMAPI_SECRET` | DHCP dynamic updates | Ansible (DHCP config) |
| `TAILSCALE_AUTH_KEY` | Tailscale node auth | Ansible (Tailscale role) |
| `CLOUDFLARE_API_KEY` | DDNS updates | Ansible (cron job) |
| `CLOUDFLARE_ZONE_ID` | DNS zone identifier | Ansible (cron job) |
| `NAMESILO_API_KEY` | SSL cert renewal | Ansible (cron job) |

### Optional Secrets

| Secret | Purpose |
|--------|---------|
| `GRAFANA_TOKEN` | Grafana API access |
| `TAILSCALE_API_TOKEN` | Tailscale API (not auth) |

### Current Secret Storage

⚠️ **All secrets currently live in GitLab CI variables only.**

This is a chicken-and-egg problem: if the router dies, GitLab is gone, and so are the secrets needed to restore it.

**Recommended**: See Appendix B for mitigation strategies.

---

## Appendix B: Known Gaps & Future Improvements

### 🔴 Critical Gap: Secrets Bootstrap

**Problem**: Secrets only exist in GitLab CI, which runs on the router being recovered.

**Mitigations** (choose one or more):

1. **Encrypted secrets file in external storage**
   - Store `secrets.env.gpg` in a separate cloud storage (Google Drive, 1Password, etc.)
   - Decrypt with a memorized passphrase
   
2. **Print physical backup**
   - Store encrypted secrets (or recovery key) in a safe deposit box
   
3. **Secondary GitLab mirror**
   - Push CI variables to a GitHub Actions secret or external GitLab instance

### 🔴 Critical Gap: Repository Mirror

**Problem**: The homelab repo lives on self-hosted GitLab. No external mirror exists.

**Mitigations**:

1. **GitHub mirror**
   ```bash
   # Set up as secondary remote
   git remote add github git@github.com:nkontur/homelab.git
   git push github main
   ```
   
2. **Include repo in restic backup**
   - Already backing up `/persistent_data/application/gitlab`, which includes the repos

### 🟡 LUKS Key Management

**Current**: Single password for all encrypted drives.

**Recommended**:
- Add a recovery keyfile stored separately from the password
- `cryptsetup luksAddKey /dev/sdX /path/to/keyfile`

### 🟡 Boot Sequence Dependencies

**Order matters**:
1. Network interfaces must be up before Docker networks work
2. ZFS pools must import before containers can bind-mount data
3. DHCP/DNS must run before other services can resolve internal names

Consider adding systemd dependencies or a startup script that waits for prerequisites.

### 🟢 Future: Automated DR Testing

- Schedule quarterly DR drills
- Spin up VM, test restore procedure
- Document any steps that have drifted

---

## Quick Reference Card

For printing/laminating:

```
DISASTER RECOVERY QUICK REFERENCE

1. Install Ubuntu Server 22.04 on boot drive
2. Create minimal netplan (WAN DHCP + bond0)
3. Unlock LUKS drives: cryptsetup luksOpen /dev/sdX name
4. Import ZFS: zpool import mpool && zpool import persistent_data
5. Clone repo: git clone <homelab repo>
6. Export secrets as env vars
7. Run: ansible-playbook -i ansible/inventory.yml ansible/router.yml --connection=local
8. Restore data: restic restore latest --target / --include <path>
9. Restart: docker compose down && docker compose up -d
10. Verify: docker ps, DNS, DHCP, external access

Secrets location: ___________________________
Emergency contact: ___________________________
Last tested: ___________________________
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2025-02-01 | Moltbot | Initial version based on DR tracing session |
