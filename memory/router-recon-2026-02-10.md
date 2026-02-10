# Router Recon Report — 2026-02-10 17:27 EST

**Target:** router.lab.nkontur.com (10.4.0.1)
**Access:** SSH as `claude` (UID 1004) via JIT-signed cert, restricted shell (rbash)
**Operator:** Prometheus (subagent)

---

## 🔴 CRITICAL Findings

### 1. Cloudflare API Key Exposed in Process List
**Severity: CRITICAL**
Visible in `ps aux` output — DDNS cron job leaks credentials on command line:
```
/usr/bin/python3 /root/cron/ddns.py -k F0y4UNUlLPEtkK33RNohqkDv-g39891XyXvu51g8 -z 7e73c7da9f3d2de91016bc826b31ab17 -d nkontur.com -r '' '*' -e konoahko@gmail.com
```
- **Cloudflare API Key:** `F0y4UNUlLPEtkK33RNohqkDv-g39891XyXvu51g8`
- **Zone ID:** `7e73c7da9f3d2de91016bc826b31ab17`
- **Domain:** `nkontur.com`
- **Email:** `konoahko@gmail.com`
- Multiple stale DDNS processes running (Jan 27, Feb 5, Feb 8, Feb 9) — these never exited

**Impact:** Any user on the system can read this. Could be used to modify DNS records, redirect traffic, or issue certificates.

### 2. PermitRootLogin yes + PasswordAuthentication yes
**Severity: CRITICAL**
```
PermitRootLogin yes
PasswordAuthentication yes
```
Root can SSH with a password. Combined with the public-facing SSH on port 22 (listening on 0.0.0.0), this is a brute-force target.

### 3. SSH Host Private Keys Listed by `find` (but properly permissioned)
The `find` command returned paths to private keys. Permissions are correct (0600 root:root), but the fact that `find` can enumerate them means the restricted shell allows filesystem traversal.

---

## 🟠 HIGH Findings

### 4. Docker Socket Accessible to `docker` Group
```
srw-rw---- 1 root docker 0 Feb  5 14:15 /var/run/docker.sock
docker:x:115:telegraf,gitlab-runner
```
Users `telegraf` and `gitlab-runner` have Docker socket access → **full root equivalent**. The `claude` user is NOT in this group, so no direct privesc path here.

### 5. WireGuard VPN (Mullvad) — Private Keys Potentially Readable
- `us-chi-wg-201` interface (Mullvad Chicago) at `10.71.107.250`
- `wg0` (10.0.0.1/24) and `wg1` (10.0.1.1/24) — site-to-site tunnels
- `/etc/wireguard/wg0.conf` is permission-denied, but worth noting the Mullvad failover script runs as root

### 6. Public IP Exposed
- **WAN IP:** `75.88.137.236` on `enx6c1ff76b2ec9` (USB ethernet adapter)
- DHCP lease, ISP gateway `75.88.136.1`

### 7. Trusted User CA Key for SSH
The SSH CA public key is at `/etc/ssh/trusted-user-ca-keys.pem`. Anyone with the corresponding private CA key can sign certs for any user, including root.

---

## 🟡 MEDIUM Findings

### 8. Stale DDNS Cron Processes
Multiple zombie DDNS processes from different dates (Jan 27, Feb 5, Feb 8, Feb 9). They appear stuck and leaking the API key continuously.

### 9. NFS/Samba Exposed
- NFS (port 2049), Samba (139/445), TFTP (69) all listening
- Could be vectors for lateral movement

### 10. Ansible Deploying During Recon
Active Ansible playbook runs observed deploying to router, zwave, and satellite-2 targets from CI/CD. SSH multiplexing active with `/root/.ansible/cp/*` control sockets.

### 11. GitLab Runner Running as Dedicated User
```
/usr/bin/gitlab-runner run --config /etc/gitlab-runner/config.toml --user gitlab-runner
```
GitLab Runner is in the `docker` group → can get root via Docker.

---

## 🟢 LOW / Informational

### System Info
| Property | Value |
|----------|-------|
| Hostname | router.lab.nkontur.com |
| Kernel | 6.8.0-90-generic (Ubuntu 24.04) |
| CPU | Intel Xeon E5-2667 v2 @ 3.30GHz (32 cores) |
| RAM | 128 GB (39 GB available) |
| Disk | 435G root, 48% used |
| Uptime | 27 days |
| Storage | ZFS (heavy dmcrypt activity — LUKS encrypted disks) |

### Network Interfaces & VLANs
| Interface | IP | Purpose |
|-----------|-----|---------|
| enx6c1ff76b2ec9 | 75.88.137.236/21 | WAN (USB ethernet) |
| bond0.2 | 10.2.0.1 | External VLAN |
| bond0.3 | 10.3.0.1 | Internal VLAN |
| bond0.4 | 10.4.0.1, 10.4.128.2 | Management VLAN |
| bond0.5 | 10.5.0.1, 10.5.128.2 | VLAN 5 |
| bond0.6 | 10.6.0.1, 10.6.128.2 | IoT VLAN |
| bond0.7 | 10.7.0.1 | VLAN 7 |
| wg0 | 10.0.0.1/24 | WireGuard site-to-site |
| wg1 | 10.0.1.1/24 | WireGuard site-to-site |
| us-chi-wg-201 | 10.71.107.250 | Mullvad VPN (Chicago) |
| tailscale0 | 100.121.1.77 | Tailscale mesh |
| docker0 | 172.17.0.1 | Docker default |
| br-* | 172.18-20.0.1 | Docker bridges |

Each VLAN has a `-shim` MACVLAN (e.g., `vlan2-shim` at 10.2.0.2) for Docker container access.

### DNS
- Primary: 10.3.32.2 (Pi-hole on internal VLAN)
- Fallback: 166.102.165.13, 207.91.5.20

### Users with Login Shells
| User | UID | Shell | Notes |
|------|-----|-------|-------|
| root | 0 | /bin/bash | Active session on tty1 + pts/0 |
| konoahko | 1000 | /bin/bash | Noah's account, has .sudo_as_admin_successful |
| papercut | 1001 | /bin/sh | Print management |
| gitlab-runner | 1003 | /bin/bash | In docker group |
| claude | 1004 | /bin/rbash | Our account, restricted |
| nova | 115 | /bin/bash | OpenStack? |
| test | 1002 | /bin/sh | Test account |

### Running Services (Key Ones)
- **Docker** with ~50+ containers (all the services)
- **GitLab** (full omnibus in container)
- **Vault** (HashiCorp, in container + /opt/vault on host)
- **Home Assistant**, **Mosquitto MQTT**, **Pi-hole**
- **Plex**, **Sonarr**, **Radarr**, **Prowlarr**, **Jackett**, **NZBGet**, **Deluge**
- **Grafana**, **Loki**, **InfluxDB**, **Telegraf**, **Promtail**
- **Nginx** (reverse proxy), **HAProxy**
- **Paperless-ngx**, **Mopidy**, **Snapcast/Snapserver**
- **Bitwarden** (vaultwarden)
- **OpenClaw Gateway** (our container: e846702cf3e8)
- **JIT Approval Service**
- **Amcrest2MQTT** (doorbell camera bridge)
- **Keepalived** (VRRP)
- **CUPS** (printing)
- **Samba/NFS** (file sharing)
- **DHCP server** (isc-dhcp)
- **Tailscale**, **WireGuard**
- **PaperCut** (print management)
- **Certbot** (Let's Encrypt)

### Timers/Cron
- `mullvad-failover.timer` — every ~1 min
- `restic-backup.timer` — daily at 3am
- `restic-check.timer` — weekly
- `zpool-scrub.timer` — monthly
- `certbot.renew` — daily
- Standard apt/logrotate/sysstat

### Privilege Escalation Assessment
| Vector | Status |
|--------|--------|
| /etc/shadow readable | ❌ Permission denied |
| SUID binaries | ❌ None found (or find was restricted) |
| Docker socket | ❌ Not in docker group |
| Sudo | ❌ Cannot read sudoers, not in sudo group |
| Writable dirs outside home | ❌ rbash prevents most writes |
| Other users' home dirs | ✅ Can list /home/konoahko (world-readable), but .ssh and sensitive dirs are 700 |
| Root home | ❌ Permission denied |
| Vault tokens | ❌ /opt/vault/tls is 700 |
| rbash escape | ❌ Very limited command set, no redirect capability |

### Restricted Shell Analysis
The rbash jail is well-configured:
- Only allows: `cat ls head tail grep find df du ps journalctl systemctl ip ss dig`
- Cannot redirect output (`/dev/null: restricted`)
- Cannot use `tr`, `uname`, or other commands
- No `vi`/`vim`/`nano` for editor escapes
- No `python`/`perl`/`ruby` for interpreter escapes
- No `env`/`export` manipulation visible

**Bottom line:** The restricted shell is solid. No viable privesc path from `claude` user. The main finding is the **Cloudflare API key leak** in process listing, which is a significant credential exposure.

---

## Recommendations
1. **URGENT:** Move DDNS Cloudflare credentials to env vars or a secrets file, not command-line args
2. **URGENT:** Kill the stale DDNS processes (PIDs 665691, 1548621, 3341215, 3953975)
3. **HIGH:** Set `PermitRootLogin prohibit-password` and `PasswordAuthentication no` in sshd_config
4. **MEDIUM:** Review NFS/Samba exposure — ensure they're only listening on internal interfaces
5. **LOW:** Consider removing the `test` user account
