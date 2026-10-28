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
| `B2_ACCOUNT_ID` | Backblaze B2 application key ID (the append-only key in Vault `backup/backblaze` is enough to restore) |
| `B2_ACCOUNT_KEY` | Backblaze B2 application key |
| B2 account login | Only for the account owner: lifecycle rules, keys, and early permanent deletion. Never stored on the router or in Vault |
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
   - `CLOUDFLARE_API_KEY`, `CLOUDFLARE_ZONE_ID` (SSL certs use Cloudflare DNS-01 via `networking/ssl/renew.sh`)
   - `GRAFANA_TOKEN`
   - `GITLAB_RUNNER_TOKEN`, `IMAGES_GITLAB_RUNNER_TOKEN`
5. **GitLab instance settings.** A restored GitLab keeps them. On a fresh
   GitLab, the project has more pipeline schedules than the default limit
   of 10 allows: run the `gitlab:settings` job (Run pipeline on `main` with
   `SCHEDULED_TASK=gitlab-settings`), or on the router
   `scripts/gitlab-settings.sh`. It raises the `ci_pipeline_schedules` plan
   limit to 20 inside the gitlab container. By hand:
   `docker exec gitlab gitlab-rails runner 'Plan.default.actual_limits.update!(ci_pipeline_schedules: 20)'`.
   See [GITLAB_SETTINGS.md](GITLAB_SETTINGS.md).

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

0. If the host was reinstalled without restoring `/etc/ssh/ssh_host_*`, its
   host keys changed and CI (strict host key checking) will refuse to connect.
   Update `ansible/known_hosts` first: see [DEPLOY.md](DEPLOY.md#ssh-host-keys).
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

## Offsite repository tampered with

Use this when the router was compromised, or `restic snapshots` against B2 is
missing history, or `restic check` reports missing packs/indexes.

The router's B2 key cannot permanently delete anything. Whatever an attacker
"deleted" or overwrote is still in the bucket as a hidden or older file version
for **35 days** after the change (bucket lifecycle rule, see `docs/backups.md`,
*Deletion resistance*). Act inside that window.

1. **Freeze.** Stop the router timers (`systemctl stop restic-backup.timer
   restic-prune.timer restic-check.timer`) or keep the router off the network.
   From a clean machine, in the B2 web console, delete the B2 application key the
   router used and create a new one with the same restricted capabilities. If the
   timeline is unclear, raise the bucket's lifecycle `daysFromHidingToDeleting` (for
   example to 365) to stop the clock while you investigate.
2. **Find a point in time** before the first tampering. B2's file browser with
   *Show hidden files* shows when hide markers and new versions appeared.
3. **Read the repository as of that time** from a clean machine with
   [rclone](https://rclone.org/b2/) (b2 backend, any key with `listFiles` +
   `readFiles` on the bucket). Nothing is changed in the bucket:

   ```bash
   # rclone remote "b2" configured with the key; T is UTC, before the attack
   T=2026-01-01T00:00:00Z
   rclone serve restic --b2-version-at "$T" b2:nkontur-homelab --addr 127.0.0.1:8000 &
   export RESTIC_REPOSITORY=rest:http://127.0.0.1:8000/
   export RESTIC_PASSWORD="<restic-password>"
   # the point-in-time view is read-only, so restic must not try to lock it
   restic snapshots --no-lock
   restic check --no-lock
   restic restore --no-lock <snapshot-id> --target /restore --include /persistent_data/application
   ```

   For a full copy instead (for example to re-seed a new bucket):
   `rclone copy --b2-version-at "$T" b2:nkontur-homelab /restore/restic-repo`.
4. **Repair the live bucket**, if you want to keep using it, by unhiding files in the
   B2 console (or copying the point-in-time view to a new bucket with
   `rclone sync --b2-version-at`) and then running `restic check` against it.
   Only then re-enable the router timers with the new key.

Do not run `restic forget` or `restic prune` against the tampered bucket until
step 3 has produced a good restore.

---

## Z-Wave Pi (new SD card or SSD)

The Z-Wave network lives in the USB stick (Zooz ZST10 700) and in
zwave-js-ui's store. The store is backed up nightly to the router
(`docs/backups.md`, "Z-Wave"); the S0/S2 keys are also in Vault at
`homelab/docker/zwave-js-ui` (fields `s0_legacy`, `s2_unauthenticated`,
`s2_authenticated`, `s2_access_control`, and `s2_authenticated_lr` /
`s2_access_control_lr` when Long Range keys exist). Without the keys, secure
devices (locks, garage) have to be excluded and re-included.

1. Flash Raspberry Pi OS Lite onto the new card or SSD and boot the Pi with
   the Z-Wave stick plugged in. It gets 10.6.128.6 from the router's DHCP
   (`zwave_mac` in the inventory).
2. Enable root SSH with the router's key, then update the pinned host keys:
   the new install has new ones, so both `ansible/known_hosts` (CI,
   `docs/DEPLOY.md` "SSH host keys") and the router's `/root/.ssh/known_hosts`
   (used by `zwave-backup.service`) need the new entries
   (`ssh-keygen -R 10.6.128.6; ssh-keygen -R zwave.lab.nkontur.com`, then
   connect once from the router and check the fingerprint).
3. Run a full zwave deploy (*Run pipeline* on `main`, or let the nightly full
   deploy do it). This installs Docker, creates the empty `zwave-config`
   volume and starts zwave-js-ui. Then stop the container straight away:
   `docker stop zwavejs2mqtt`.
   A fresh zwave-js-ui may generate new security keys. If a deploy copies them
   to Vault before the store is restored (the deploy log says "Stored keys
   differed; the previous ones are version N"), the real keys are still in
   Vault as that earlier version of `homelab/docker/zwave-js-ui`
   (`vault kv get -version=N homelab/docker/zwave-js-ui`). Once the restored
   store is running, the next deploy writes the restored keys back.
4. Copy the newest archive from the router and unpack it into the volume:
   ```bash
   # on the router
   ls -t /persistent_data/application/zwave-backup/   # newest first
   scp /persistent_data/application/zwave-backup/zwave-store-<ts>.tar.gz root@10.6.128.6:/root/
   # on the Pi
   mp=$(docker volume inspect -f '{{.Mountpoint}}' zwave-config)
   rm -rf "${mp:?}"/* "${mp:?}"/.[!.]*
   tar -C "$mp" -xzf /root/zwave-store-<ts>.tar.gz
   docker start zwavejs2mqtt
   ```
   Older archives: restic snapshots of the router (`restic restore latest
   --target /tmp/zw --include /persistent_data/application/zwave-backup`).
5. Check in the zwave-js-ui UI (http://10.6.128.6:8091) that the controller
   comes up with the old home ID and that the nodes keep their names.
   If `settings.json` was lost but the network cache was not, enter the keys
   from Vault under *Settings → Z-Wave → Security keys* (read them with a
   Vault token that can read `homelab/docker/zwave-js-ui`; do not paste them
   anywhere else).
6. Home Assistant reconnects through the Z-Wave JS websocket on port 3000.
7. Reboot once so the hardware watchdog dtparam (`configure-pi-watchdog`)
   takes effect.

If the stick itself is lost, the store alone does not bring the network back:
restore the controller NVM from a zwave-js-ui NVM backup (in the archive under
`backups/nvm/`, when NVM backups are enabled) onto a replacement stick with
*Advanced actions → NVM management → Restore*.

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
