# Disaster Recovery Runbook

Complete guide to recovering the homelab from bare metal. Target audience: Noah with an internet connection and a basic Ubuntu USB stick.

**Estimated Total Recovery Time:** 1-3 hours (depending on data restore volume)

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Phase 1: Base OS Install](#phase-1-base-os-install-30-min)
3. [Phase 2: Network Bootstrap](#phase-2-network-bootstrap-manual-15-min)
4. [Phase 3: Disk Setup](#phase-3-disk-setup-30-min)
5. [Phase 4: Single-Shot Bootstrap](#phase-4-single-shot-bootstrap-30-min--restore-time)
6. [Phase 5: Verification](#phase-5-verification-15-min)
7. [Appendix A: Secrets Reference](#appendix-a-secrets-reference)
8. [Appendix B: Secrets File Format](#appendix-b-secrets-file-format)
9. [Appendix C: Known Gaps](#appendix-c-known-gaps--future-improvements)

---

## Prerequisites

### What You Need

- [ ] Ubuntu Server 22.04 LTS USB installer
- [ ] Physical access to router hardware (or IPMI access from 10.4.128.7)
- [ ] Internet connection (separate from the homelab network)
- [ ] Access to secrets (see [Appendix A](#appendix-a-secrets-reference))

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
sudo passwd root
sudo sed -i 's/^#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo systemctl restart sshd

apt update && apt upgrade -y
apt install -y git curl wget vim htop python3
```

---

## Phase 2: Network Bootstrap (MANUAL, ~15 min)

### 2.1 Identify Network Interfaces

```bash
ip link show
```

Expected:
- `enx6c1ff76b2ec9` — WAN (USB Ethernet, DHCP from ISP)
- `enp4s0f0`, `enp4s0f1` — Bond members (to managed switch)

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

```bash
netplan apply
```

### 2.3 Verify Connectivity

```bash
ping -c 3 8.8.8.8       # Internet
ip addr show bond0       # Bond up
```

---

## Phase 3: Disk Setup (~30 min)

### 3.1 Unlock LUKS Encrypted Drives

```bash
blkid | grep crypto_LUKS

cryptsetup luksOpen /dev/sdX cryptdata1
cryptsetup luksOpen /dev/sdY cryptdata2
# etc.
```

### 3.2 Import ZFS Pools

```bash
apt install -y zfsutils-linux

zpool import mpool
zpool import persistent_data
zfs mount -a

# Verify
zpool status
df -h | grep -E "mpool|persistent"
```

---

## Phase 4: Single-Shot Bootstrap (~30 min + restore time)

This is where the magic happens. One command does everything:

### Option A: Restore from Backup (recommended)

```bash
cd /root
git clone https://github.com/<mirror>/homelab.git
cd homelab
bash scripts/bootstrap.sh --restore
```

### Option B: Fresh Install

```bash
bash scripts/bootstrap.sh --fresh
```

Or with a pre-populated secrets file (avoids interactive prompts):

```bash
bash scripts/bootstrap.sh --fresh --secrets-file /path/to/secrets.env.gpg
```

### What the Script Does (12 Steps)

| Step | Action | Notes |
|------|--------|-------|
| 1 | Install Docker + compose | Idempotent |
| 2 | Create macvlan networks | internal, external, iot, mgmt |
| 3 | Install restic | For backup/restore |
| 4 | Restore from B2 | `--restore` only, prompts for creds |
| 5 | Start GitLab | Uses restored data if available |
| 6 | Wait for GitLab health | Up to 10 minutes |
| 7 | Install gitlab-runner | Package install |
| 8 | Start Vault | Container with TLS |
| 9 | Bootstrap Vault | Unseal (restore) or init+seed (fresh) |
| 10 | Set CI/CD variables | 5 vars via GitLab API |
| 11 | Register runner | Via GitLab API |
| 12 | Trigger pipeline | Wait for green |

### What You'll Be Prompted For

**Restore mode:**
- B2 credentials (account ID, key, repo, password)
- Which data paths to restore
- GitLab personal access token (api scope)
- Vault unseal keys (if auto-unseal file not in backup)
- Vault root token (for CI/CD variable injection)

**Fresh mode:**
- All of the above, plus every Vault secret interactively
  (or provide `--secrets-file` to skip prompts)

### Expected Output

```
[bootstrap] Step 12/12: First pipeline
[bootstrap] Pipeline #42 triggered.
[bootstrap]   ... pipeline status: running (60s / 1200s)
[bootstrap]   ... pipeline status: running (135s / 1200s)
[bootstrap] 🎉 Pipeline #42 passed! First run is GREEN. ✓
[bootstrap] 🎉 BOOTSTRAP COMPLETE 🎉
```

---

## Phase 5: Verification (~15 min)

### Core Services

| Service | Check | Expected |
|---------|-------|----------|
| Docker | `docker ps` | All containers running |
| DNS | `dig @10.3.32.2 gitlab.lab.nkontur.com` | Resolves |
| GitLab | `curl -I http://localhost` | 200 OK |
| Vault | `curl -sk https://vault.lab.nkontur.com:8200/v1/sys/health` | `"sealed":false` |
| Nginx | `curl -I https://nkontur.com` | 200 OK |

### Network

```bash
ping -c 1 10.2.32.1  # External nginx
ping -c 1 10.3.32.2  # Internal pihole
ping -c 1 10.6.32.3  # IoT mosquitto
```

### Backups

```bash
systemctl status restic-backup.timer
```

---

## Appendix A: Secrets Reference

### Required for Recovery

| Secret | Purpose | When Needed |
|--------|---------|-------------|
| `LUKS_PASSWORD` | Decrypt data drives | Phase 3 |
| `B2_ACCOUNT_ID` | Backblaze storage access | Phase 4 (restore) |
| `B2_ACCOUNT_KEY` | Backblaze storage auth | Phase 4 (restore) |
| `RESTIC_PASSWORD` | Decrypt backups | Phase 4 (restore) |
| Vault unseal keys (3 of 5) | Unseal Vault | Phase 4 (step 9) |
| Vault root token | CI/CD var injection | Phase 4 (step 10) |
| GitLab PAT (api scope) | API calls | Phase 4 (steps 10-12) |

### Storage Recommendations

1. **1Password vault** — all secrets in a "Homelab DR" vault
2. **Encrypted USB** in a safe deposit box
3. **GPG-encrypted secrets file** in cloud storage (for `--secrets-file` flag)

---

## Appendix B: Secrets File Format

For non-interactive bootstrap with `--secrets-file`:

```bash
# Bootstrap credentials
GITLAB_BOOTSTRAP_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
B2_ACCOUNT_ID=000xxxxxxxxxxxx
B2_ACCOUNT_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
RESTIC_REPOSITORY=s3:s3.us-east-005.backblazeb2.com/nkontur-homelab
RESTIC_PASSWORD=xxxxxxxxxxxx

# Vault seed secrets (fresh mode only)
# Pattern: VAULT_SEED_<PATH>_<FIELD> (path / → _, - → _)
VAULT_SEED_API_KEYS_ACLAWDEMY_API_KEY=xxx
VAULT_SEED_API_KEYS_ANTHROPIC_API_KEY=xxx
VAULT_SEED_API_KEYS_BRAVE_API_KEY=xxx
VAULT_SEED_API_KEYS_OPENAI_API_KEY=xxx
VAULT_SEED_DOCKER_AUDIOSERVE_SECRET=xxx
VAULT_SEED_DOCKER_GRAFANA_ADMIN_PASSWORD=xxx
VAULT_SEED_DOCKER_GRAFANA_SMTP_PASSWORD=xxx
VAULT_SEED_DOCKER_GRAFANA_TOKEN=xxx
# ... (all paths from ansible/roles/fetch-vault-secrets/defaults/main.yml)
VAULT_SEED_BACKUP_BACKBLAZE_ACCESS_KEY_ID=xxx
VAULT_SEED_BACKUP_RESTIC_PASSWORD=xxx
VAULT_SEED_NETWORKING_CLOUDFLARE_API_KEY=xxx
VAULT_SEED_NETWORKING_CLOUDFLARE_ZONE_ID=xxx
VAULT_SEED_NETWORKING_NAMESILO_API_KEY=xxx
```

Encrypt: `gpg -c secrets.env` → `secrets.env.gpg`

---

## Appendix C: Known Gaps & Future Improvements

### 🟡 Repository Mirror

The homelab repo lives on self-hosted GitLab. Consider maintaining a GitHub
mirror for bootstrapping when GitLab is unavailable:

```bash
git remote add github git@github.com:nkontur/homelab.git
git push github main
```

The repo is also included in the restic backup under `/persistent_data/application/gitlab`.

### 🟡 LUKS Key Management

Current: Single password for all encrypted drives.

Consider adding a recovery keyfile stored separately:
```bash
cryptsetup luksAddKey /dev/sdX /path/to/keyfile
```

### 🟢 Future: Automated DR Testing

- Schedule quarterly DR drills
- Spin up VM, test restore procedure
- Document any steps that have drifted

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
7. Answer prompts. Wait. Done.

Secrets location: ___________________________
Last tested: ___________________________
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2026-02-06 | Moltbot | Rewrite for single-shot bootstrap (steps 8-12 automated) |
| 2025-02-01 | Moltbot | Initial version based on DR tracing session |
