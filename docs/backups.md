# Homelab Backup Strategy

Comprehensive backup documentation for the homelab infrastructure, covering all critical services and data.

## Overview

The homelab runs containerized services with data stored on ZFS (mpool) with automated restic backups to Backblaze B2. This document provides detailed backup and restore procedures for each service.

## Critical Data Inventory

### Storage Architecture

- **Primary storage**: ZFS pool (`mpool`) mounted at `/mpool/`
- **Container data**: `/persistent_data/application/` (mapped to `{{ docker_persistent_data_path }}` in compose). `/persistent_data` is a single LUKS + ext4 SSD, not ZFS: no redundancy and no ZFS snapshots, so restic is its only copy
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
- Database: `nextcloud_db_lts` Docker volume (`docker_nextcloud_db_lts`, MariaDB 12.3 LTS)
- Config: Container-managed

**Critical components:**
- User files in `/mpool/nextcloud/nextcloud/data/`
- Database with user accounts, shares, app configs
- `config.php` with instance configuration

**Restore procedure:**
1. Stop containers: `docker stop nextcloud nextcloud_database`
2. Restore data directory: `/mpool/nextcloud/`
3. Restore database volume: `nextcloud_db_lts`
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
- Database: `docker_influxdb-storage` Docker volume (`/var/lib/influxdb2`)
- Config: `/persistent_data/application/influxdb/config/`

**Critical components:**
- Time series data (metrics from all services)
- Bucket configurations
- User tokens and permissions
- Database schemas

**Restore procedure:**
1. Stop InfluxDB: `docker stop influxdb`
2. Restore the volume's files from restic (`/persistent_data/docker/volumes/docker_influxdb-storage`)
3. If InfluxDB will not start from them, use the weekly `influx backup` instead (*Database Recovery*, InfluxDB)
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
- Data: `docker_mosquitto` Docker volume (retained messages)

**Critical components:**
- `mosquitto.conf` configuration
- User authentication files
- Retained MQTT messages
- TLS certificates

**Restore procedure:**
1. Stop Mosquitto: `docker stop mosquitto`
2. Restore config: `/persistent_data/application/mqtt/conf/`
3. Restore volume: `docker_mosquitto`
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

### Z-Wave (zwave-js-ui on the Z-Wave Pi)
**Data locations (on the Pi, `zwave.lab.nkontur.com`, 10.6.128.6):**
- Store: Docker named volume `zwave-config`, mounted at `/usr/src/app/store`
  (`docker/docker-compose-zwave.yml`), on the Pi's SD card
- `settings.json` holds the zwave-js-ui settings, including the S0/S2 network
  security keys; `cache/` holds the Z-Wave JS network cache (`<homeid>.jsonl`)
  with node interviews and names; `backups/` holds zwave-js-ui's own
  NVM/store backups when they are enabled in its settings

**Backup (router pulls, nothing on the IoT VLAN connects to the router):**
- `zwave-backup.timer` on the router, daily at 02:15 (+ up to 10 min random
  delay), runs `/usr/local/sbin/zwave-backup` (role `ansible/roles/zwave-backup`)
- It SSHes to the Pi as root, tars the volume (excluding `logs/` and the
  re-downloadable `.config-db/`) and writes
  `/persistent_data/application/zwave-backup/zwave-store-<YYYYmmdd-HHMMSS>.tar.gz`
  (mode 0600; the archive contains the security keys)
- **Local retention: the newest 7 archives.** Longer history comes from restic:
  the archives sit under `/persistent_data/application`, which is in
  `restic_folders`, and the pull finishes before `restic-backup.timer` (03:00),
  so each night's archive is in that night's B2 snapshot and kept for restic's
  90 days. Restic is not installed on the Pi.
- The S0/S2 keys are also copied to Vault on every zwave deploy (nightly full
  deploy included): `homelab/docker/zwave-js-ui` (role
  `ansible/roles/zwave-security-keys`, written only when they change)

**Monitoring:** each good run writes `/var/lib/zwave-backup/last_ok`; telegraf
sends it as measurement `zwave_backup`, and the anomaly detector's
`zwave_backup` signal goes moderate at 26h and severe at 36h. A night when the
Pi does not answer ping counts as good (logged as "skipped"): the host-down
signals (`network_devices`, `telegraf_hosts`, `zwave_influx_data`) already
alert on that, so a known-down Pi does not alert twice. While the Pi is down
no new archive is written; the last 7 archives (and restic's history) remain.

**Restore:** see `docs/DISASTER_RECOVERY.md`, "Z-Wave Pi".

### Additional Services

**Grafana:**
- Data: `docker_grafana-storage` Docker volume
- Config: `/persistent_data/application/grafana/`
- Contains dashboards, datasources, users

**Nginx (all instances):**
- Configs: `/persistent_data/application/{nginx,lab_nginx,iot_nginx}/conf/`
- Logs: `/var/log/{nginx,lab_nginx,iot_nginx}/`
- Certificates: `/persistent_data/application/certs/`

## Current Backup Strategy

### Automated Backups (Restic)

**Backend:** Backblaze B2 (`s3:s3.us-east-005.backblazeb2.com/nkontur-homelab`)
**Schedule:** `restic-backup.timer`, daily at 03:00 local. Runs commonly take 2-4 hours.
**Deep check:** `restic-check.timer`, Sundays at 09:00 local (`restic check --read-data-subset=5%`), deliberately outside the backup window because the repository takes a single lock.
**Integrity check:** `ExecStartPre=restic check` runs a full metadata check *before every
backup*, so repository reachability and structural integrity are verified daily, not just weekly.
**Retention:** `restic forget --keep-within 90d` as `ExecStartPost` on every backup run, and
`restic prune` weekly from `restic-prune.timer` (Saturdays 12:00 local, `--retry-lock 2h`). That is
the **only** snapshot retention rule. Every snapshot taken in the last 90 days is kept and
**nothing older than 90 days survives** in the repository view; there is no daily/monthly/yearly
tier. Underneath that, B2 keeps every file restic forgets or prunes for a further 35 days as a
hidden version (see *Deletion resistance* below).

**Frigate:** its own jobs write to the same repository with tags `frigate*` and their own
30-day forget (role `frigate-backup`, docs/frigate.md "Backup"). They never run alongside
`restic-backup.service`, whose `check` and `forget` take an exclusive lock without `--retry-lock`.

> **Corrected 2026-10-10.** This line previously read "30 daily, 12 monthly, 5 yearly". That was
> never configured. `--keep-within` has been the sole `forget` flag since the role was written
> (`90e1bca`, 2026-02-05) and the value went `30d` -> `90d` in `059a6be` (2026-02-07); no
> `--keep-daily/--keep-monthly/--keep-yearly` has ever appeared in
> `ansible/roles/restic/templates/restic-backup.service.j2`. Confirmed against the router's journal
> rather than the template alone: `Applying Policy: keep all snapshots within 90d of the newest`,
> and `forget` reports `keep 90 snapshots: / remove 2 snapshots:` per path on each run.
>
> **Consequence to be aware of when planning a restore:** corruption or encryption discovered more
> than 90 days after it happened has no clean snapshot to go back to, and the "Ransomware: offline
> backup restoration" scenario below assumes a long tail that does not exist. Adding a
> `--keep-monthly`/`--keep-yearly` tier is a Backblaze storage-cost decision, so it is left to the
> repo owner rather than changed here — only the documentation is corrected.

### Logical database dumps

restic copies every database's raw data files, but a file copy of a running
InnoDB, PostgreSQL or InfluxDB (TSM) engine is not crash-consistent and may not
start. So each database is also dumped online, through its own engine, by
`/usr/local/sbin/restic-db-dumps` (template
`ansible/roles/restic/templates/restic-db-dumps.j2`, inventory
`restic_db_dumps` in `ansible/router.yml`). Both copies are kept: the raw files
are the fast path, the dumps are the path that is known to restore.

**Where:** `/persistent_data/application/db-dumps/` (0700, files 0600), inside
the `/persistent_data/application` restic folder. Only the latest dump is kept
on disk; restic keeps the history (90 days).

**When:**

- `restic-db-dumps.service` (group `nightly`): `restic-backup.service` has
  `Wants=` + `After=` on it, so every backup, scheduled or by hand, first takes
  a fresh set of dumps. `Wants=` and not `Requires=`: a failed dump never stops
  the file backup.
- `restic-db-dumps-weekly.service` (group `weekly`), from
  `restic-db-dumps-weekly.timer`, Saturdays 21:00: databases too large to dump
  every night. Sunday's 03:00 backup uploads the result.

| Dump | Engine / how | File | Group |
|---|---|---|---|
| `gitlab` | GitLab omnibus PostgreSQL, `pg_dump -Fc` of `gitlabhq_production` | `gitlab.pgdump.zst` | nightly |
| `gitlab-registry` | same, container registry metadata DB `registry` | `gitlab-registry.pgdump.zst` | nightly |
| `nextcloud` | MariaDB, `mariadb-dump --single-transaction` of `nextcloud` | `nextcloud.sql.zst` | nightly |
| `wordpress` | MySQL, `mysqldump --single-transaction`; skipped while the `wordpress` compose profile is stopped | `wordpress.sql.zst` | nightly |
| `vaultwarden`, `paperless`, `grafana`, `navidrome`, `ombi`, `ombi-external` | SQLite, `sqlite3 .dump` (one read transaction, as the file owner) | `<name>.sql.zst` | nightly |
| `influxdb` | InfluxDB 2, `influx backup` (full: metadata, tokens, every bucket) | `influxdb/` (manifest + shard `.tar.gz`) | weekly |

Measured on the first live run (2026-11-07): `gitlab` 527 MB in 59 s,
`nextcloud` 42 MB in 6 s, `gitlab-registry` and every SQLite dump under 2 MB
and 1 s each, so the nightly set adds about a minute before the backup.
`influxdb` (35 GB of TSM data) took 24 minutes and wrote 15 GB (143 shard
tarballs), mostly gzip inside `influxd`, which is why it is weekly. Every dump
was then restored into a throwaway, network-less container and compared with
the live database (row and point counts matched).

Every dump goes to a `.tmp` name, is verified (`pg_restore -l` lists table
data; the SQL dump ends in its `-- Dump completed` / `COMMIT;` trailer; the
InfluxDB manifest parses and every file it names is present with the recorded
size), and only then replaces the previous
one. A failed dump leaves the last good one in place. Output is `zstd
--rsyncable` so restic still deduplicates unchanged parts between nights.

Credentials never leave the containers that already hold them: the MariaDB and
MySQL dumps read the root password from the container's own environment through
`MYSQL_PWD`, PostgreSQL uses the local socket as `gitlab-psql`, and the InfluxDB
operator token is read from the `influxdb` container into a 0600 env-file that
is deleted when the backup ends.

**Alerting:** each run logs `restic-db-dumps: OK|SKIP|FAIL <name> ...` and one
`restic-db-dumps: SUMMARY group=<group> ok= skipped= failed=` line. Grafana
alerts *Nightly Database Dumps Missing or Failing* (no clean nightly summary in
48 h) and *Weekly Database Dumps Missing or Failing* (8 days) require a clean
summary, and *Restic Unit Failed* covers both units. `OnFailure=` also sends the
usual `restic-notify-failure@` mail.

**Run by hand** (outside 02:15-04:00):

```bash
systemctl start restic-db-dumps.service          # whole nightly set
/usr/local/sbin/restic-db-dumps nightly gitlab   # one dump
journalctl -u restic-db-dumps.service -n 20 | grep restic-db-dumps
```

**Deliberately not dumped:**

- *Home Assistant recorder* (`home-assistant_v2.db`, ~14 GB SQLite): history
  only. A dump would cost a 14 GB rewrite every night; the file copy (WAL
  sidecars excluded) restores to the last checkpoint, and HA
  configuration lives in YAML and `.storage/` JSON, not in this database.
- *Sonarr, Radarr, Lidarr, Prowlarr*: each writes its own weekly backup zip
  (`<config>/Backups/scheduled/`) through SQLite's backup API, and restic
  copies those. *Plex* does the same every three days
  (`.../Plug-in Support/Databases/*.db-YYYY-MM-DD`).
- *Vault*: the `file` storage backend has no online dump (`vault operator
  raft snapshot` exists only for Raft storage). restic copies its per-key
  files; moving Vault to Raft would make a snapshot possible and is a
  separate change.
- *Caches and rebuildable data*: Trivy DBs, Pelias/Valhalla map data,
  Paperless' Redis.

### Deletion resistance (offsite copy)

**Threat:** someone with root on the router can read `/root/.restic_env`, and through the
router everything it can reach. The offsite copy must survive that for at least 30 days.

**Design: append-only key plus versioned retention.** Two B2 properties do the work:

1. A B2 application key **without `deleteFiles`** cannot remove a file version. Its "deletes"
   (S3 `DeleteObject` without a version id, or `b2_hide_file`) only add a *hide marker*; the data
   stays in the bucket as a hidden version. Uploading over an existing name likewise keeps the
   old version.
2. The bucket lifecycle rule `daysFromHidingToDeleting` permanently removes a hidden or superseded
   version only after that many days. Changing lifecycle rules needs `writeBuckets`, which the
   router key does not hold.

So restic on the router runs `forget` and `prune` normally with the restricted key: everything it
"deletes" is hidden, and B2 expires it after the retention window. No delete-capable credential is
needed for routine operation, so none is stored on the router, in Vault, or in CI.

| Setting | Value |
|---|---|
| Router key (Vault `backup/backblaze`) | bucket-scoped to `nkontur-homelab`; `listBuckets, readBuckets, listFiles, readFiles, writeFiles` only |
| Bucket lifecycle | one bucket-wide rule: `daysFromHidingToDeleting = 35`, `daysFromUploadingToHiding` unset |
| Object Lock | not used (see below) |
| Prune cadence | weekly, so repacking churn (billed for the window) stays small |

**Effective protection window:** any version an attacker hides or overwrites stays recoverable for
35 days after that action (lifecycle runs once a day, so up to ~36). Within that window the
repository can be rebuilt as it was before the attack: see `docs/DISASTER_RECOVERY.md`,
*Offsite repository tampered with*. The attacker can still stop new backups and upload junk
(cost, not loss); `restic-backup-stale` and the morning digest catch the first.

**Continuous verification:** `/usr/local/sbin/restic-b2-policy-check` runs as `ExecStartPre` of
every backup (it can never block the backup). With the key restic actually uses, it checks the
key's bucket scope and capabilities, checks the lifecycle rules against
`restic_b2_min_hidden_retention_days`, uploads a tiny probe (`.restic-b2-policy-probe` at the bucket
root, ignored by restic) and confirms B2 refuses `b2_delete_file_version` on it. It prints
`restic-b2-policy: OK ...` only when everything passes; the **Restic B2 Deletion Resistance Not
Verified** alert fires when no OK line appeared in 48h. A check running on the router cannot defend
against an attacker who is already root there; its job is to catch drift, such as a full-access key
put back into Vault or the lifecycle rule shortened from the B2 console.

**Why not Object Lock?** B2 can enable Object Lock on an existing bucket, but restic never
refreshes retention on objects it has already written. Because restic deduplicates, the oldest
packs hold most of the data and are referenced by every recent snapshot; once their lock lapses
they are as deletable as before. Lock alone would therefore not protect against a delete-capable
key, and with an append-only key it adds nothing against a router compromise. It would only add
protection against a compromised B2 account, at the price of extra billed storage and the
irreversibility of enabling it. It stays an option for that threat.

**Things that must stay true:**
- The B2 master key and any key with `deleteFiles` live only in the account owner's password
  manager, never on the router, in Vault, or in CI. Vault runs on the router.
- Do not set the bucket back to "Keep only the last version" (that is
  `daysFromHidingToDeleting = 1`, the pre-2026-10 setting).
- Permanently removing data early (for example to cut costs) is a manual action from the B2 web
  console by the account owner.

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

### Mac mini

The Mac mini (`macmini.lab.nkontur.com`) is backed up by the router, not by itself: the Mac
holds no restic password and no B2 key. Role `macmini-backup` installs
`restic-macmini-pull.service`, which `restic-backup.service` starts and waits for
(`restic_backup_pre_units`, after `restic-db-dumps`), so it runs every night at 03:00 just before
the upload. A failed pull is alerted on (Restic Unit Failed, OnFailure mail) but never stops the
rest of the backup.

1. Over the router's root SSH, `tmutil localsnapshot` takes an APFS snapshot of the Mac's Data
   volume, and the script mounts it read-only. Everything is copied as of that one instant, so
   Photos and Messages (SQLite with WAL) are crash-consistent even while the apps run; nothing
   has to be quit. The snapshot is unmounted and deleted at the end of the run.
2. `rsync -a --delete` (incremental) from the snapshot to `/mpool/backups/macmini/data/`: all of
   `/Users` (home directories incl. Photos library, `Library/Messages`, Mail, Notes, Calendar,
   `Application Support`, `Preferences`, keychains, dotfiles and SSH keys), `/etc`, root's home,
   crontabs, `/Library/LaunchDaemons|LaunchAgents|Preferences`, Homebrew's `etc`.
   Excluded: `Library/Caches` at any depth, `.Trash`, the Spotlight index
   (`Library/Metadata/CoreSpotlight`), Xcode DerivedData/simulators, VM and Docker disk images,
   and iCloud files evicted from the disk (dataless; listed per run). Extended attributes are
   not kept (macOS ships openrsync, which cannot send them).
3. Inventories of the live system into `meta/`: `Brewfile` (`brew bundle dump`), brew versions,
   `/Applications`, `pkgutil --pkgs`, `system_profiler`, `sw_vers`.

Root over SSH has Full Disk Access on the Mac, which the TCC-protected paths need. If the pull
starts failing with "Operation not permitted", re-enable it in System Settings, General, Sharing,
Remote Login, "Allow full disk access for remote users".

The staging copy is on the LUKS-encrypted pool (the script refuses any other filesystem), mode
0700. restic backs it up as one folder with tag `macmini` and without the global `*-wal`
excludes, since the WAL files come from the same snapshot as their databases. Retention is the
main 90-day `forget`. Restore: `restic restore latest --tag macmini --target /tmp/r --include
/mpool/backups/macmini/data/Users/<user>/...`, then copy each database together with its `-wal`.

### ZFS Snapshots

**Strategy:** Local ZFS snapshots for quick recovery, from the `zfs-auto-snapshot`
package (role `configure-base`). Only datasets with
`com.sun:auto-snapshot=true` are snapshotted: `mpool/nextcloud`, `mpool/plex`,
`mpool/backups` (not `mpool/frigate`, `mpool/dv-scratch` or `mpool` itself, and
`/persistent_data` is not ZFS).
**Schedule** (`/etc/cron.d/zfs-auto-snapshot`, from `zfs_auto_snapshot_schedule`
in `ansible/roles/configure-base/defaults/main.yml`): every 15 min (keep 8),
hourly (keep 24), daily at 23:59 (keep 14), weekly Sunday 23:59 (keep 4),
monthly on the 1st (keep 6). That is the only schedule: the role removes the
package's `/etc/cron.{hourly,daily,weekly,monthly}/zfs-auto-snapshot` scripts,
which used to run every label a second time with other `--keep` values. A
lower keep prunes the oldest snapshots of that label on its next run. To stop
snapshotting a dataset, `zfs set com.sun:auto-snapshot=false <dataset>` (its
existing snapshots are then left alone, not pruned).

**Per-dataset exceptions** (`zfs_auto_snapshot_dataset_overrides`): `mpool/plex`
keeps about one week, since its snapshots pin deleted media. It gets frequent
and hourly as above, 7 dailies (a `--destroy-only --label=daily --keep=7` run at
00:15 trims it after the 23:59 daily), and no weekly or monthly
(`com.sun:auto-snapshot:weekly=false` and `:monthly=false`, set by the role).
The script skips a dataset for any label whose property is false, even when
the dataset is named on the command line, so a lower keep has to be a separate
destroy-only line rather than an exclusion plus its own snapshot line.

```bash
# List snapshots
zfs list -t snapshot mpool/nextcloud

# Copy one file back (snapshots are browsable read-only)
ls /mpool/nextcloud/.zfs/snapshot/

# Roll a whole dataset back (destroys everything newer)
zfs rollback -r mpool/nextcloud@zfs-auto-snap_daily-<date>
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

Prefer the raw files restored by restic when the database starts cleanly from
them and the data looks right. When it does not (InnoDB or PostgreSQL crash
recovery fails, InfluxDB panics on a shard, SQLite reports corruption), restore
from the logical dump instead (see *Logical database dumps* above).

**Get a dump out of restic** (any snapshot of `/persistent_data/application`):

```bash
restic snapshots --path /persistent_data/application
restic restore <snapshot-id> --target /tmp/r --include /persistent_data/application/db-dumps
ls -la /tmp/r/persistent_data/application/db-dumps/
```

**GitLab (PostgreSQL, `gitlab.pgdump.zst`, `gitlab-registry.pgdump.zst`).**
The dump only makes sense with the matching `/etc/gitlab/gitlab-secrets.json`
(from `/persistent_data/application/gitlab/config/`, restored with the rest of
`/persistent_data/application`): it holds the keys for the encrypted columns.
Run the same GitLab version that took the dump.

```bash
docker exec gitlab gitlab-ctl stop puma
docker exec gitlab gitlab-ctl stop sidekiq
zstd -dc gitlab.pgdump.zst | docker exec -i -u gitlab-psql gitlab \
  /opt/gitlab/embedded/bin/pg_restore -h /var/opt/gitlab/postgresql \
  -d gitlabhq_production --clean --if-exists   # check the error count it prints at the end
# Registry metadata, the same way, into -d registry
docker exec gitlab gitlab-ctl restart
docker exec gitlab gitlab-rake gitlab:check SANITIZE=true
docker exec gitlab gitlab-rake gitlab:doctor:secrets
```

**Nextcloud (MariaDB, `nextcloud.sql.zst`).** The dump carries
`CREATE DATABASE IF NOT EXISTS` and drops each table before recreating it.

```bash
docker exec -u www-data nextcloud php occ maintenance:mode --on
zstd -dc nextcloud.sql.zst | docker exec -i nextcloud_database \
  sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mariadb -uroot'
docker exec -u www-data nextcloud php occ maintenance:mode --off
docker exec -u www-data nextcloud php occ status
```

WordPress (`wordpress.sql.zst`) is the same against `wordpress_db`, with
`mysql` as the client.

**SQLite (`vaultwarden`, `paperless`, `grafana`, `navidrome`, `ombi`,
`ombi-external`).** Rebuild the file from the SQL text, as its owner, with the
app stopped:

```bash
docker stop bitwarden
cd /persistent_data/application/bitwarden/data
mv db.sqlite3 db.sqlite3.broken; rm -f db.sqlite3-wal db.sqlite3-shm
zstd -dc vaultwarden.sql.zst | sqlite3 db.sqlite3
sqlite3 db.sqlite3 'PRAGMA integrity_check;'   # expect: ok
chown --reference=db.sqlite3.broken db.sqlite3
docker start bitwarden
```

The database paths are the `target` values in `restic_db_dumps`
(`ansible/router.yml`).

**InfluxDB (`influxdb/`, weekly).** A full restore replaces every bucket,
dashboard, task, user and token with the backup's, so tokens issued after the
backup stop working. Up to a week of metrics since the dump is lost unless the
raw shards restored by restic are usable.

Since InfluxDB 2.9 tokens are stored hashed, so a backup no longer holds the
operator token in plain text. `restore --full` then needs `--operator-token`:
the operator token the restored instance should use (the one in the
container's `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`). Other tokens in the backup
keep working for clients that already hold them.

```bash
# The CLI runs in a throwaway container in influxdb's network namespace. The
# token is the operator token of the instance being restored *into*.
docker exec influxdb printenv DOCKER_INFLUXDB_INIT_ADMIN_TOKEN | sed 's/^/INFLUX_TOKEN=/' > /root/influx.env
chmod 600 /root/influx.env
docker run --rm --network container:influxdb --env-file /root/influx.env \
  -v /tmp/r/persistent_data/application/db-dumps/influxdb:/backup:ro \
  --entrypoint sh "$(docker inspect -f '{{.Config.Image}}' influxdb)" \
  -c 'influx restore --full /backup --host http://127.0.0.1:8086 --operator-token "$INFLUX_TOKEN"'
rm /root/influx.env
```

To recover one bucket without touching the rest, use
`restore --bucket <name> --new-bucket <name>-restored /backup` instead of
`--full`.

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
# zpool-scrub.timer scrubs mpool monthly (1st, 00:00); by hand:
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
- **Grafana provisioned alerts** for stale backups, restic errors, failed restic
  units (`restic-backup`, `restic-check`, `restic-prune`), unscheduled timers,
  incomplete snapshots, and offsite deletion resistance not verified
  (`restic-b2-policy`, see *Deletion resistance* above)
- **Email alerts** on backup failures (via Grafana contact points)
- **Morning digest** includes restic backup status summary

> **There is no separate `backup-verify` timer, by design (2026-10-10).** `base/backup-verify/`
> used to sit in the repo looking like an independent freshness/integrity verifier. Nothing ever
> deployed it: no Ansible task referenced any of its three files in any commit, its unit pointed
> `EnvironmentFile=` at `/etc/default/restic-env` while the restic role writes `/root/.restic_env`
> (so systemd could not have started it even if installed), no `backup-verify.service` value
> appears in Loki's `unit` label over the longest window Loki will answer (30 days — it rejects
> anything longer), and its three InfluxDB measurements (`backup_verify`,
> `backup_stats`, `backup_verify_run`) are absent from `schema.measurements()`, which lists every
> measurement the bucket has *ever* received. It was added 2026-02-05 and superseded two days later
> by `059a6be`, which built the same assurance properly out of `restic-check.service` plus the
> provisioned alerts above. The files have been removed so that the next reader does not count them
> as coverage; `git show 94eac7a:base/backup-verify/backup-verify.sh` recovers them (full clone; the hash is unreachable in a shallow one) if repository-side
> verification metrics are ever wanted as a deliberate piece of work. Tracked in #142.

> **Note:** Backup credentials are managed via Vault (`homelab/backup/restic` for the repository
> password, `homelab/backup/backblaze` for the append-only B2 key).
> JIT T2 approval is required for credential access.

#### Incomplete snapshots

`restic-backup.service` backs up each path as a separate `ExecStart` line, so a
hard failure part-way through drops every path after it plus `forget`/`prune`.
restic exit code `3` (snapshot written, some source file unreadable) is treated
as success by the unit for that reason; the `Restic Backup Incomplete` alert is
what surfaces it. Details and remediation: `ansible/roles/restic/README.md`.

### Storage Monitoring

- **ZFS pool and disk** alerts (Grafana): *ZFS Pool Unhealthy*, *ZFS Pool Health
  Collection Failed*, *Disk Vanished*, *Disk Temperature High*, *SMART Disk Error*
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

- **Backup credentials** stored in HashiCorp Vault (`homelab/backup/restic`, `homelab/backup/backblaze`) and rendered to `/root/.restic_env`
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

**Last updated:** 2026-11-08  
**Review schedule:** Quarterly  
**Owner:** Infrastructure Team