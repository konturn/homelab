# zwave-backup

Router side of the Z-Wave store backup. `zwave-backup.timer` (02:15, before
`restic-backup.timer` at 03:00) runs `/usr/local/sbin/zwave-backup`, which:

1. pings the Z-Wave Pi. No reply: log "skipped", refresh `last_ok`, exit 0
   (the host-down signals already alert; this avoids a second alert);
2. over SSH as root (router to Pi, so no IoT-to-router access is needed),
   tars the `zwave-config` Docker volume (zwave-js-ui's `store/`, including
   `store/backups/`, excluding `logs/` and `.config-db/`) to
   `/persistent_data/application/zwave-backup/zwave-store-<ts>.tar.gz`;
3. checks the archive lists `./settings.json`, keeps the newest 7, and
   writes `last_success` and `last_ok` under `/var/lib/zwave-backup/`.

restic picks the archives up at 03:00 with the rest of
`/persistent_data/application`, so offsite history is restic's 90 days.

Monitoring: telegraf reads `/var/lib/zwave-backup/last_ok` (measurement
`zwave_backup`), and the anomaly detector's `zwave_backup` signal goes
moderate after 26h and severe after 36h.

Restore: docs/DISASTER_RECOVERY.md, "Z-Wave Pi".
