# Backup Configuration

Backups are handled by **restic** via the Ansible role in `ansible/router.yml`.

## Current Setup

| Component | Value |
|-----------|-------|
| Tool | [Restic](https://restic.net/) |
| Backend | Backblaze B2 |
| Repository | `s3:s3.us-east-005.backblazeb2.com/nkontur-homelab` |
| Ansible Role | `konturn/ansible-restic` |

## What's Being Backed Up

Configured in `ansible/router.yml`:

- `/mpool/nextcloud` — User files, Paperless documents
- `/persistent_data/application` — All container configs (HA, GitLab, Bitwarden, etc.)
- `/persistent_data/docker/volumes` — Docker named volumes (excludes nextcloud volume)
- `/mpool/plex/config` — Plex configuration
- `/mpool/plex/Photos` — Photo library
- `/mpool/plex/Family` — Family videos
- `/var/log` — System logs
- `/root` — Root home directory

## Common Commands

```bash
# Check backup status
restic snapshots

# List files in latest backup
restic ls latest

# Restore a specific path
restic restore latest --target /restore --include /persistent_data/application/homeassistant

# Check repository health
restic check
```

## Environment Variables

The Ansible role expects these environment variables (set during deployment):

- `BACKBLAZE_ACCESS_KEY_ID`
- `BACKBLAZE_SECRET_ACCESS_KEY`
- `RESTIC_PASSWORD`

## Modifying Backup Paths

Edit `ansible/router.yml` under the `restic` role's `restic_folders` variable, then run the playbook.
