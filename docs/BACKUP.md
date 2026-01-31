# Backup Strategy

This document outlines what needs to be backed up in the homelab infrastructure and recommended approaches for disaster recovery.

## Overview

The homelab has two primary data storage locations:

1. **ZFS Pool (`/mpool`)** - Large media and user data
2. **Persistent Data (`/persistent_data/application`)** - Container configurations and application state

## Critical Data Locations

### Tier 1: CRITICAL (Must Backup)

These contain irreplaceable data or configurations that are complex to recreate:

| Location | Description | Priority |
|----------|-------------|----------|
| `/persistent_data/application/bitwarden/` | Password vault data | **HIGHEST** |
| `/persistent_data/application/homeassistant/` | HA config, automations, history | **HIGHEST** |
| `/persistent_data/application/gitlab/` | GitLab repos, config, CI data | **HIGHEST** |
| `/persistent_data/application/zigbee2mqtt/` | Zigbee device pairings, network state | HIGH |
| `/persistent_data/application/certs/` | SSL certificates (Let's Encrypt) | HIGH |
| `/mpool/nextcloud/nextcloud` | User files, documents | HIGH |
| `/mpool/nextcloud/paperless` | Scanned documents | HIGH |
| `nextcloud_db` (Docker volume) | Nextcloud database | HIGH |
| `wordpress_db` (Docker volume) | Blog database | MEDIUM |

### Tier 2: IMPORTANT (Should Backup)

Configurations that took time to set up:

| Location | Description |
|----------|-------------|
| `/persistent_data/application/nginx/` | External nginx configs |
| `/persistent_data/application/lab_nginx/` | Internal nginx configs |
| `/persistent_data/application/iot_nginx/` | IoT nginx configs |
| `/persistent_data/application/pihole/` | Pi-hole blocklists, DNS |
| `/persistent_data/application/radarr/` | Movie tracking database |
| `/persistent_data/application/sonarr/` | TV show tracking database |
| `/persistent_data/application/prowlarr/` | Indexer configs |
| `/persistent_data/application/grafana/` | Dashboards, data sources |
| `/persistent_data/application/influxdb/` | Metrics history |
| `/persistent_data/application/moltbot/` | Moltbot workspace and state |
| `/persistent_data/application/mqtt/` | MQTT broker config |
| `/persistent_data/application/paperless/` | Paperless-ngx config |

### Tier 3: NICE TO HAVE (Optional)

Can be recreated but convenient to restore:

| Location | Description |
|----------|-------------|
| `/persistent_data/application/nzbget/` | Download client config |
| `/persistent_data/application/jackett/` | Indexer configs |
| `/persistent_data/application/deluge/` | Torrent client config |
| `/persistent_data/application/snapserver/` | Multiroom audio config |
| `/persistent_data/application/mopidy/` | Music player config |
| `/persistent_data/application/wordpress/` | Blog files |

### NOT Required to Backup

| Location | Reason |
|----------|--------|
| `/mpool/plex/Movies`, `/mpool/plex/TV` | Media can be re-downloaded |
| `/mpool/plex/transcode` | Temporary transcode data |
| `/mpool/audioserve/audiobooks` | Media can be re-downloaded |
| Docker container logs | Ephemeral |
| `/persistent_data/application/registry/` | Container images can be rebuilt |

## Docker Named Volumes

These volumes store application databases and must be backed up:

```bash
# List volumes
docker volume ls

# Critical volumes:
# - nextcloud_db     (MariaDB database)
# - wordpress_db     (MySQL database)
# - influxdb-storage (Metrics database)
# - grafana-storage  (Dashboards)
# - mosquitto        (MQTT retained messages)
```

**Backup approach:** Use `docker run --rm -v <volume>:/data -v /backup:/backup alpine tar czf /backup/<volume>.tar.gz /data`

## Recommended Backup Strategy

### Option 1: Restic to Backblaze B2 (Recommended)

[Restic](https://restic.net/) provides encrypted, deduplicated backups with excellent performance.

```bash
# Initialize repository (one-time)
export B2_ACCOUNT_ID="your-account-id"
export B2_ACCOUNT_KEY="your-account-key"
restic init -r b2:bucket-name:homelab-backup

# Backup critical paths
restic backup \
  /persistent_data/application/bitwarden \
  /persistent_data/application/homeassistant \
  /persistent_data/application/gitlab \
  /persistent_data/application/zigbee2mqtt \
  /persistent_data/application/certs \
  /mpool/nextcloud/nextcloud \
  /mpool/nextcloud/paperless \
  --exclude="*.log" \
  --exclude="cache" \
  --exclude="*.tmp"

# Prune old backups (keep 7 daily, 4 weekly, 12 monthly)
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
```

### Option 2: ZFS Snapshots + Send

For ZFS-native backup to another pool or remote system:

```bash
# Create recursive snapshot
zfs snapshot -r mpool@backup-$(date +%Y%m%d)

# Send to remote (incremental after first)
zfs send -R mpool@backup-20240131 | ssh backup-server zfs recv backuppool/mpool
```

### Option 3: Borg Backup

Alternative to Restic with similar features:

```bash
borg init --encryption=repokey ssh://backup@remote/path/to/repo
borg create ::backup-{now} /persistent_data/application /mpool/nextcloud
```

## Database Backup Procedures

### GitLab

```bash
# GitLab built-in backup
docker exec gitlab gitlab-backup create

# Backups stored in: /persistent_data/application/gitlab/data/backups/
```

### MariaDB (Nextcloud)

```bash
docker exec nextcloud_database mysqldump -u nextcloud -p nextcloud > nextcloud_db.sql
```

### InfluxDB

```bash
docker exec influxdb influx backup /backup
```

## Recovery Procedures

### Full System Recovery

1. **Install base system** (Ubuntu with ZFS)
2. **Restore ZFS pools** from snapshots or import existing
3. **Clone homelab repo** and run Ansible
4. **Restore `/persistent_data/application`** from backup
5. **Restore Docker volumes** from tar archives
6. **Start containers** with `docker compose up -d`

### Single Service Recovery

1. Stop the service: `docker stop <service>`
2. Restore config: `restic restore latest --target / --include /persistent_data/application/<service>`
3. Restart: `docker start <service>`

### Database Recovery

```bash
# Nextcloud database
docker exec -i nextcloud_database mysql -u nextcloud -p nextcloud < nextcloud_db.sql

# GitLab
docker exec gitlab gitlab-backup restore BACKUP=<timestamp>
```

## Backup Schedule Recommendations

| Data Type | Frequency | Retention |
|-----------|-----------|-----------|
| Bitwarden | Daily | 90 days |
| Home Assistant | Daily | 30 days |
| GitLab | Daily | 30 days |
| Nextcloud files | Daily | 30 days |
| Nextcloud DB | Daily | 14 days |
| All configs | Weekly | 12 weeks |
| Full system | Monthly | 6 months |

## Verification

Run the backup verification script to check data health:

```bash
./scripts/backup-check.sh
```

## TODO

- [ ] Choose backup destination (B2, another NAS, offsite server)
- [ ] Set up restic/borg with credentials
- [ ] Create systemd timer for automated backups
- [ ] Test restore procedure quarterly
- [ ] Set up backup monitoring/alerting
