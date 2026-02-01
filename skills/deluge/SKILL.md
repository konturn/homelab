---
name: deluge
description: BitTorrent client management via Deluge Web API. Add torrents, check status, manage downloads.
---

# Deluge Skill

Manage torrents via Deluge's Web JSON-RPC API.

## Configuration

Environment variables (already configured):
- `DELUGE_URL`: http://deluge:8112
- `DELUGE_PASSWORD`: Web UI password

## Authentication

Deluge requires session-based auth. First login to get a cookie:

```bash
# Login and capture session cookie
SESSION_COOKIE=$(curl -s -c - "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -d '{"method":"auth.login","params":["'"$DELUGE_PASSWORD"'"],"id":1}' \
  | grep _session_id | awk '{print $NF}')

# Use cookie in subsequent requests
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{"method":"...", "params":[...], "id":1}'
```

## Core Operations

### Get All Torrents

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{
    "method": "core.get_torrents_status",
    "params": [{}, ["name", "state", "progress", "download_payload_rate", "upload_payload_rate", "eta"]],
    "id": 1
  }' | jq '.result | to_entries | .[] | {
    name: .value.name,
    state: .value.state,
    progress: .value.progress
  }'
```

### Get Specific Torrent

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{
    "method": "core.get_torrent_status",
    "params": ["TORRENT_HASH", ["name", "state", "progress", "total_size", "eta"]],
    "id": 1
  }'
```

### Add Torrent by Magnet

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{
    "method": "core.add_torrent_magnet",
    "params": ["magnet:?xt=urn:btih:...", {}],
    "id": 1
  }'
```

### Add Torrent by URL

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{
    "method": "core.add_torrent_url",
    "params": ["http://url-to-torrent-file", {}],
    "id": 1
  }'
```

### Pause/Resume Torrent

```bash
# Pause
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{"method": "core.pause_torrent", "params": [["TORRENT_HASH"]], "id": 1}'

# Resume
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{"method": "core.resume_torrent", "params": [["TORRENT_HASH"]], "id": 1}'
```

### Remove Torrent

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{
    "method": "core.remove_torrent",
    "params": ["TORRENT_HASH", true],
    "id": 1
  }'
# Second param: true = remove data, false = keep data
```

### Get Session Stats

```bash
curl -s "$DELUGE_URL/json" \
  -H "Content-Type: application/json" \
  -H "Cookie: _session_id=$SESSION_COOKIE" \
  -d '{"method": "core.get_session_status", "params": [["download_rate", "upload_rate", "num_peers"]], "id": 1}'
```

## Common Workflows

### "What's seeding?"
1. Login to get session
2. `core.get_torrents_status` with state filter
3. Look for `state: "Seeding"`

### "Add a magnet link"
1. Login to get session
2. `core.add_torrent_magnet` with the magnet URI
3. Check result for torrent hash

## Notes

- Session cookies expire; re-login if you get auth errors
- Torrent hashes are 40-char hex strings
- States: Downloading, Seeding, Paused, Error, Queued
