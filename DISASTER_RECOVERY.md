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
6. [Phase 4.5: Vault & JIT Recovery](#phase-45-vault--jit-recovery-20-min)
7. [Phase 5: Data Restore](#phase-5-data-restore-1-4-hours)
8. [Phase 6: Verification](#phase-6-verification-15-min)
9. [Appendix A: Secrets Reference](#appendix-a-secrets-reference)
10. [Appendix B: Known Gaps](#appendix-b-known-gaps--future-improvements)

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

# Vault (needed in Phase 4.5 — Vault stays sealed without these)
export VAULT_UNSEAL_KEYS="<3 of the 5 keys, one per line>"
export VAULT_TOKEN="<vault root token>"

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

## Phase 4.5: Vault & JIT Recovery (~20 min)

**Do this before Phase 5.** Vault is the credential broker for the whole lab: the
JIT approval service, the `openclaw` agent, and CI deploys all authenticate
through it. Until Vault is unsealed, none of them can start doing useful work,
and `fetch-vault-secrets` falls back to CI environment variables for everything.

### 4.5.1 The playbook that does this

Phase 4.5 runs `ansible/bootstrap.yml`, **not** `router.yml`. It is a separate
playbook that brings up the core service layer (vault, pihole, nginx, lab_nginx,
iot_nginx, gitlab) and then initialises and unseals Vault:

```bash
cd /root/homelab
export CI_PROJECT_DIR=/root/homelab   # bootstrap.yml templates from this path

ansible-playbook -i ansible/inventory.yml ansible/bootstrap.yml \
  --connection=local
```

Run just the Vault phase with `--tags vault` if the rest is already up.

### 4.5.2 Which case are you in?

Vault uses the **file** storage backend (`storage "file" { path = "/vault/file" }`),
bind-mounted from `/persistent_data/application/vault`. So:

| Did `/persistent_data/application/vault/file` survive? | Case |
|---|---|
| Yes (ZFS pool imported intact, or restored from restic first) | **A — unseal** |
| No (fresh pool, or Vault data lost) | **B — re-initialise** |

```bash
# Check before running anything
ls -la /persistent_data/application/vault/file/
```

If you are in Case B but the data exists in a restic snapshot, it is far less
work to restore `/persistent_data/application/vault` from Phase 5 **first** and
then come back here as Case A. Case B loses every secret in Vault.

### 4.5.3 Case A — Vault data survived (unseal only)

Vault is initialised; it just needs 3 of its 5 unseal keys.

```bash
export VAULT_UNSEAL_KEYS="$(cat <<'EOF'
<key1>
<key2>
<key3>
EOF
)"
```

`bootstrap.yml` writes these to
`/persistent_data/application/vault/unseal/unseal-keys` (dir `0700`, file
`0600`, root only) and then unseals over the API. The
`auto-unseal.sh` entrypoint reads the same file on every subsequent container
start, so once the file is in place restarts need no intervention.

If the keys file is missing or wrong, Vault still starts — it just stays sealed.
That is deliberate. Unseal by hand with:

```bash
docker exec vault vault operator unseal <key1>
docker exec vault vault operator unseal <key2>
docker exec vault vault operator unseal <key3>
```

Verify:

```bash
curl -sk https://vault.lab.nkontur.com:8200/v1/sys/seal-status | jq '{sealed, initialized}'
# want: {"sealed": false, "initialized": true}
```

Stop here if sealed=false. Everything below is Case B.

### 4.5.4 Case B — Vault data lost (re-initialise)

`bootstrap.yml` initialises automatically when it finds an uninitialised Vault
(`secret_shares: 5`, `secret_threshold: 3`) and writes the result to:

```
/persistent_data/application/vault/init-output.json   (mode 0600)
```

> ⚠️ **Back that file up off-box immediately.** It contains the five new unseal
> keys and the new root token. It is the only copy. If you lose it you are
> rebuilding Vault from scratch a second time.

Then paste the new keys into the `VAULT_UNSEAL_KEYS` CI/CD variable (protected)
and the root token into `VAULT_TOKEN`, or every future pipeline re-seals on
restart.

Everything in Vault is now gone, so it has to be rebuilt in this order:

**1. Policies, auth methods and roles — `terraform/vault/`**

```bash
export VAULT_TOKEN="<new root token from init-output.json>"
export VAULT_ADDR="https://vault.lab.nkontur.com:8200"
```

Normally the `vault:configure` CI job applies this (runs on `main` when
`terraform/vault/**` changes). Its Terraform state lives in **GitLab**
(`/api/v4/projects/<id>/terraform/state/vault`), so if GitLab was also lost and
restored from restic, the state comes back with it. If the state is gone,
Terraform will plan to create everything from nothing — which is correct here,
but read the plan before applying.

This creates the JWT backend (`ci-deploy` role) and the AppRole backend with
roles `openclaw`, `jit-approval-svc`, `vault-admin` and `vault-read`.

**2. Re-issue AppRole secret_ids**

`role_id` is stable per role; `secret_id` is not and is not stored anywhere
recoverable. Run the manual **`vault:rotate-approles`** pipeline job, which
generates fresh secret_ids, verifies a login with each before adopting it, and
writes `VAULT_APPROLE_ROLE_ID` / `VAULT_APPROLE_SECRET_ID` back into the GitLab
CI variables. It needs `APPROLE_ROTATION_PAT` set, or the rotation succeeds in
Vault and silently fails to update CI.

**3. Repopulate KV**

`bootstrap.yml` enables KV v2 at `secret/`, but only the mount — not the data.
Service credentials under `homelab/data/*` (API keys for radarr/sonarr/plex/
ombi/nzbget/deluge/paperless/prowlarr, the `openclaw` tokens, the JIT API key,
the Telegram bot token) must be written back from your off-box copy or
regenerated at each service.

### 4.5.5 JIT approval service

`jit-approval-svc` has `depends_on: vault: condition: service_healthy`, so it
will not start until Vault is up — expect it to sit waiting, not to fail. Its
entire environment (`VAULT_ROLE_ID`, `VAULT_SECRET_ID`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `JIT_API_KEY`, `GITLAB_ADMIN_TOKEN`) is templated by
Ansible from Vault, so it must be redeployed **after** 4.5.4 step 3, not before:

```bash
docker compose -f /persistent_data/application/ansible_state/docker-compose.yml \
  -p docker up -d jit-approval-svc
```

The Telegram webhook **re-registers itself** on startup — `main.go` calls
`setWebhook` whenever `TELEGRAM_WEBHOOK_URL` is set. There is no manual
registration step. What it needs is for `https://jit-webhook.nkontur.com` to
resolve and terminate publicly again, so if approvals are not arriving, check
DNS and the ingress path before suspecting the service.

```bash
docker logs jit-approval-svc | grep -E 'webhook_registered|webhook_register_failed'
curl -s http://<jit_ip>:8080/health
```

### 4.5.6 Verification

```bash
# Vault unsealed
curl -sk https://vault.lab.nkontur.com:8200/v1/sys/seal-status | jq '.sealed'   # false

# AppRole login works end to end
curl -sk -X POST -d "{\"role_id\":\"$ROLE_ID\",\"secret_id\":\"$SECRET_ID\"}" \
  https://vault.lab.nkontur.com:8200/v1/auth/approle/login | jq -e '.auth.client_token' >/dev/null \
  && echo "AppRole OK"

# JIT healthy and reachable
docker inspect --format '{{.State.Health.Status}}' jit-approval-svc   # healthy
```

Then request one low-tier credential end to end and confirm the Telegram
approval prompt arrives. That single test exercises Vault auth, the AppRole, the
JIT service and the webhook path in one go.

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
| noVNC | `curl -I https://novnc-dev2.lab.nkontur.com` | 200 OK (requires Mac Mini Screen Sharing) |

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
| `VAULT_UNSEAL_KEYS` | Unseal Vault (3 of 5, one per line) | Phase 4.5 (`bootstrap.yml`) |
| `VAULT_TOKEN` | Vault root token — enable KV v2, apply Terraform | Phase 4.5, `vault:configure` |
| `APPROLE_ROTATION_PAT` | Write rotated secret_ids back to CI variables | `vault:rotate-approles` |

### Optional Secrets

| Secret | Purpose |
|--------|---------|
| `GRAFANA_TOKEN` | Grafana API access |
| `TAILSCALE_API_TOKEN` | Tailscale API (not auth) |

### Current Secret Storage

⚠️ **All secrets currently live in GitLab CI variables only.**

This is a chicken-and-egg problem: if the router dies, GitLab is gone, and so are the secrets needed to restore it.

Vault does not solve this — it makes it sharper. Vault's own unseal keys are a
GitLab CI variable (`VAULT_UNSEAL_KEYS`), and Vault runs on the router being
recovered. Losing the router loses GitLab, which loses the keys, which loses
every secret Vault held. `VAULT_UNSEAL_KEYS` and the root token must be part of
whichever off-box mitigation below you adopt, or Phase 4.5 has no Case A.

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
8. Unseal Vault: ansible-playbook -i ansible/inventory.yml ansible/bootstrap.yml
   --connection=local --tags vault   (needs VAULT_UNSEAL_KEYS)
9. Restore data: restic restore latest --target / --include <path>
10. Restart: docker compose down && docker compose up -d
11. Verify: docker ps, DNS, DHCP, external access, Vault sealed=false

Secrets location: ___________________________
Emergency contact: ___________________________
Last tested: ___________________________
```

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2025-02-01 | Moltbot | Initial version based on DR tracing session |
| 2026-09-01 | Moltbot | Added Phase 4.5 (Vault & JIT recovery); documented `ansible/bootstrap.yml`; added Vault secrets to Appendix A |
