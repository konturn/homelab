# Homelab Backup Strategy

Comprehensive backup documentation for the homelab infrastructure, covering all critical services and data.

## Overview

The homelab runs containerized services with data stored on ZFS (mpool) with automated restic backups to Backblaze B2. This document provides detailed backup and restore procedures for each service.

## Critical Data Inventory

### Storage Architecture

- **Primary storage**: ZFS pool (`mpool`) mounted at `/mpool/`
- **Container data**: `/persistent_data/application/` (mapped to `{{ docker_persistent_data_path }}` in compose)
- **Docker volumes**: `/persistent_data/docker/volumes/` 
- **Network shares**: `/mpool/samba_share/nfs/`

### Data Categories

1. **User Data** - Files, documents, media
2. **Application Configs** - Service configurations, databases
3. **Secrets** - API keys, certificates, passwords
4. **System State** - Container volumes, logs

## Service-Specific Backup Details

### GitLab
**Data locations:**
- Config: `/persistent_data/application/gitlab/config/`
- Data: `/persistent_data/application/gitlab/data/` (repositories, databases, uploads)
- Logs: `/persistent_data/application/gitlab/logs/`

**Critical components:**
- Git repositories (`/var/opt/gitlab/git-data/`)
- PostgreSQL database
- GitLab Rails secrets (`/etc/gitlab/gitlab.rb`)
- Uploads and avatars
- CI/CD pipeline artifacts

**Restore procedure:**
1. Stop GitLab container: `docker stop gitlab`
2. Restore data directories from backup
3. Start container: `docker start gitlab`
4. Run reconfigure: `docker exec gitlab gitlab-ctl reconfigure`
5. Verify: `docker exec gitlab gitlab-rake gitlab:check`

### Home Assistant
**Data locations:**
- Config: `/persistent_data/application/homeassistant/`
- Addons: `/persistent_data/application/homeassistant/addons/`
- Database: `/persistent_data/application/homeassistant/home-assistant_v2.db` (SQLite)

**Critical components:**
- `configuration.yaml` and all YAML configs
- `home-assistant_v2.db` (historical data)
- `secrets.yaml` (API keys, passwords)
- Custom components and addons
- Lovelace dashboards (`/.storage/`)

**Restore procedure:**
1. Stop Home Assistant: `docker stop homeassistant`
2. Restore configuration directory
3. Fix permissions: `chown -R 1000:1000 /persistent_data/application/homeassistant/`
4. Start container: `docker start homeassistant`
5. Verify all entities and automations load correctly

### Nextcloud
**Data locations:**
- App data: `/mpool/nextcloud/nextcloud/` (bind mount)
- Database: `nextcloud_db` Docker volume (MariaDB)
- Config: Container-managed

**Critical components:**
- User files in `/mpool/nextcloud/nextcloud/data/`
- Database with user accounts, shares, app configs
- `config.php` with instance configuration

**Restore procedure:**
1. Stop containers: `docker stop nextcloud nextcloud_database`
2. Restore data directory: `/mpool/nextcloud/`
3. Restore database volume: `nextcloud_db`
4. Start database first: `docker start nextcloud_database`
5. Start Nextcloud: `docker start nextcloud`
6. Run occ commands to verify: `docker exec nextcloud php occ status`

### Plex
**Data locations:**
- Config: `/mpool/plex/config/` (metadata, databases, settings)
- Media: `/mpool/plex/Movies/`, `/mpool/plex/TV/`, `/mpool/plex/Photos/`, `/mpool/plex/Family/`
- Transcode: `/mpool/plex/transcode/` (ephemeral, not backed up)

**Critical components:**
- Plex database (`/config/Library/Application Support/Plex Media Server/`)
- Metadata and artwork caches
- User accounts and watch history
- Server settings and preferences

**Restore procedure:**
1. Stop Plex: `docker stop plex`
2. Restore config directory: `/mpool/plex/config/`
3. Start Plex: `docker start plex`
4. Verify libraries scan correctly
5. Check user accounts and watch progress

### Bitwarden/Vaultwarden
**Data locations:**
- Database: `/persistent_data/application/bitwarden/data/db.sqlite3`
- Attachments: `/persistent_data/application/bitwarden/data/attachments/`
- Icons: `/persistent_data/application/bitwarden/data/icon_cache/`
- Config: `/persistent_data/application/bitwarden/global.override.env`

**Critical components:**
- SQLite database with encrypted vaults
- User attachments and files
- Configuration and secrets

**Restore procedure:**
1. Stop Bitwarden: `docker stop bitwarden`
2. Restore data directory: `/persistent_data/application/bitwarden/data/`
3. Restore env file: `/persistent_data/application/bitwarden/global.override.env`
4. Start container: `docker start bitwarden`
5. Test login and verify all vaults accessible

### InfluxDB
**Data locations:**
- Database: `influxdb-storage` Docker volume
- Config: `/persistent_data/application/influxdb/config/`

**Critical components:**
- Time series data (metrics from all services)
- Bucket configurations
- User tokens and permissions
- Database schemas

**Restore procedure:**
1. Stop InfluxDB: `docker stop influxdb`
2. Restore volume: `docker volume rm influxdb-storage && docker volume create influxdb-storage`
3. Restore from backup: Mount backup and copy data
4. Start InfluxDB: `docker start influxdb`
5. Verify buckets and data: `influx bucket list`

### Paperless-ngx
**Data locations:**
- Config: `/persistent_data/application/paperless/`
- Documents: `/mpool/nextcloud/paperless/` (shared with Nextcloud)

**Critical components:**
- Document database and OCR indexes
- User uploaded files
- Search indexes and metadata
- User accounts and tags

**Restore procedure:**
1. Stop Paperless: `docker stop paperless-ngx`
2. Restore config: `/persistent_data/application/paperless/`
3. Restore documents: `/mpool/nextcloud/paperless/`
4. Start container: `docker start paperless-ngx`
5. Run management commands: `docker exec paperless-ngx python manage.py document_sanity_checker`

### Mosquitto (MQTT)
**Data locations:**
- Config: `/persistent_data/application/mqtt/conf/`
- Data: `mosquitto` Docker volume (retained messages)

**Critical components:**
- `mosquitto.conf` configuration
- User authentication files
- Retained MQTT messages
- TLS certificates

**Restore procedure:**
1. Stop Mosquitto: `docker stop mosquitto`
2. Restore config: `/persistent_data/application/mqtt/conf/`
3. Restore volume: `mosquitto`
4. Start container: `docker start mosquitto`
5. Verify with: `mosquitto_sub -t '$SYS/#' -C 1`

### Zigbee2mqtt
**Data locations:**
- Config: `/persistent_data/application/zigbee2mqtt/`
- Database: `/persistent_data/application/zigbee2mqtt/database.db`

**Critical components:**
- Zigbee network database (device pairings)
- Device configurations and names
- Network topology and security keys
- Configuration YAML

**Restore procedure:**
1. Stop Zigbee2mqtt: `docker stop zigbee2mqtt`
2. Restore configuration directory
3. Ensure Zigbee coordinator is connected
4. Start container: `docker start zigbee2mqtt`
5. Verify all devices are accessible

### Additional Services

**Grafana:**
- Data: `grafana-storage` Docker volume
- Config: `/persistent_data/application/grafana/`
- Contains dashboards, datasources, users

**Nginx (all instances):**
- Configs: `/persistent_data/application/{nginx,lab_nginx,iot_nginx}/conf/`
- Logs: `/var/log/{nginx,lab_nginx,iot_nginx}/`
- Certificates: `/persistent_data/application/certs/`

## Current Backup Strategy

### Automated Backups (Restic)

**Backend:** Backblaze B2 (`s3:s3.us-east-005.backblazeb2.com/nkontur-homelab`)
**Schedule:** Configured via Ansible cron jobs
**Retention:** 30 daily, 12 monthly, 5 yearly

**Backed up paths:**
- `/mpool/nextcloud` (user files, Paperless docs)
- `/persistent_data/application` (all container configs)
- `/persistent_data/docker/volumes` (Docker volumes, excluding large caches)
- `/mpool/plex/config` (Plex metadata)
- `/mpool/plex/Photos` & `/mpool/plex/Family` (personal media)
- `/var/log` (system logs)
- `/root` (root home directory)

**Excluded:**
- `/mpool/plex/transcode` (ephemeral)
- Large media files (movies/TV shows - can be re-downloaded)
- `/mpool/plex/Movies` and `/mpool/plex/TV` (optional, storage-dependent)

### ZFS Snapshots

**Strategy:** Local ZFS snapshots for quick recovery
**Recommended schedule:**
- Hourly snapshots (keep 24)
- Daily snapshots (keep 7) 
- Weekly snapshots (keep 4)

```bash
# Create snapshot
zfs snapshot mpool@backup-$(date +%Y%m%d-%H%M)

# List snapshots
zfs list -t snapshot

# Restore from snapshot
zfs rollback mpool@backup-20240204-1200
```

### Off-Site Strategy

**Primary:** Restic to Backblaze B2 (encrypted, deduplicated)
**Secondary:** Consider additional cloud provider for redundancy
**Physical:** Periodic backup to external drive for air-gap protection

## Restore Procedures

### Full System Recovery

1. **Prepare system:**
   ```bash
   # Ensure Docker is running
   systemctl start docker
   
   # Create ZFS pool if needed
   zpool import mpool || zpool create mpool /dev/disk/by-id/...
   ```

2. **Restore from backup:**
   ```bash
   # Initialize restic
   export RESTIC_REPOSITORY="s3:s3.us-east-005.backblazeb2.com/nkontur-homelab"
   export RESTIC_PASSWORD="..."
   
   # List available snapshots
   restic snapshots
   
   # Restore specific paths
   restic restore latest --target /restore
   
   # Copy restored data to correct locations
   cp -a /restore/mpool/* /mpool/
   cp -a /restore/persistent_data/* /persistent_data/
   ```

3. **Start services incrementally:**
   ```bash
   # Start core infrastructure first
   docker start gitlab
   docker start homeassistant
   
   # Then dependent services
   docker start mosquitto
   docker start zigbee2mqtt
   
   # Finally user-facing services  
   docker start plex
   docker start nextcloud
   ```

### Individual Service Recovery

1. Stop the affected service
2. Restore data from backup or ZFS snapshot
3. Fix permissions if needed
4. Start service and verify functionality
5. Check dependent services

### Database Recovery

**For SQL databases (MariaDB/PostgreSQL):**
```bash
# Stop application using the database
docker stop nextcloud

# Restore database files or dump
# Start database container
docker start nextcloud_database

# For dumps, restore via SQL
docker exec -i nextcloud_database mysql -u root -p nextcloud < backup.sql

# Start application
docker start nextcloud
```

**For SQLite databases:**
```bash
# Stop application
docker stop bitwarden

# Restore .sqlite file
cp backup/db.sqlite3 /persistent_data/application/bitwarden/data/

# Fix permissions
chown 1000:1000 /persistent_data/application/bitwarden/data/db.sqlite3

# Start application  
docker start bitwarden
```

## Backup Verification & Testing

### Automated Verification

**Restic integrity:**
```bash
# Check repository consistency
restic check

# Verify backups can be read
restic snapshots

# Test restore of small file
restic restore latest --target /tmp/test --include /etc/hostname
```

**ZFS integrity:**
```bash
# Scrub ZFS pool monthly
zpool scrub mpool
zpool status mpool
```

### Manual Testing Schedule

**Monthly:** 
- Verify restic repository health
- Test restore of one service (rotate services)
- Validate backup coverage of new services

**Quarterly:**
- Full disaster recovery test on separate hardware
- Update this documentation
- Review and update backup exclusions

**Annually:**
- Test full system restore
- Validate offsite backup access
- Review retention policies
- Update encryption keys

### Service-Specific Tests

**GitLab:**
```bash
# Verify backup integrity
docker exec gitlab gitlab-rake gitlab:backup:create
docker exec gitlab gitlab-rake gitlab:backup:restore
```

**Nextcloud:**
```bash
# Test file integrity
docker exec nextcloud php occ files:scan --all
docker exec nextcloud php occ files:cleanup
```

**Home Assistant:**
```bash
# Validate configuration
docker exec homeassistant python -m homeassistant --script check_config
```

## Monitoring & Alerting

### Backup Success Monitoring

- **Restic logs** via systemd journal (`syslog_identifier=restic`), visible in Loki
- **Grafana provisioned alerts** for stale backups and restic errors
- **Email alerts** on backup failures (via Grafana contact points)
- **Morning digest** includes restic backup status summary

> **Note:** Backup credentials are managed via Vault (`homelab/data/restic`).
> JIT T2 approval is required for credential access.

### Storage Monitoring

- **ZFS pool health** alerts
- **Disk space monitoring** for backup destinations  
- **Backup repository growth** tracking

### Recovery Time Objectives (RTO)

| Service | RTO | RPO | Priority |
|---------|-----|-----|----------|
| GitLab | 2 hours | 24 hours | Critical |
| Home Assistant | 30 minutes | 1 hour | Critical |
| Bitwarden | 1 hour | 24 hours | Critical |
| Nextcloud | 4 hours | 24 hours | High |
| Plex | 8 hours | 7 days | Medium |
| Other services | 12 hours | 24 hours | Low |

## Security Considerations

### Backup Encryption

- **Restic:** AES-256 encryption with unique repository password
- **Transport:** HTTPS/TLS for all backup transfers
- **At-rest:** Backblaze B2 server-side encryption

### Access Control

- **Backup credentials** stored in Ansible vault
- **Separate service account** for backup operations only
- **Minimal permissions** for backup destination access

### Key Management

- **Backup passwords** in password manager (Bitwarden)
- **Regular rotation** of backup service credentials
- **Recovery key storage** in secure offline location

## Disaster Scenarios

### Hardware Failure
- **ZFS pool corruption:** Restore from restic backup
- **Disk failure:** ZFS resilver from redundant disks
- **Total system loss:** Full restore to new hardware

### Data Corruption
- **Application database:** Restore from recent backup
- **File corruption:** ZFS snapshot rollback or restic restore
- **Ransomware:** Offline backup restoration

### Service Misconfiguration
- **Config file corruption:** Git history or backup restore
- **Database schema corruption:** Restore from known-good backup
- **Permission issues:** Reset from backup with correct ownership

## Backup Evolution

### Planned Improvements

1. **Incremental database backups** for large databases
2. **Cross-region backup replication** for geographic redundancy  
3. **Automated recovery testing** with scheduled validation
4. **Backup deduplication optimization** to reduce storage costs
5. **Real-time replication** for critical services

### Regular Review Items

- Backup coverage of new services
- Storage cost optimization
- Recovery time improvement
- Documentation accuracy
- Access credential rotation

---

**Last updated:** 2026-02-17  
**Review schedule:** Quarterly  
**Owner:** Infrastructure Team