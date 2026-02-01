---
name: nzbget
description: Usenet download client management via NZBGet JSON-RPC API. Check queue, add NZBs, manage downloads.
---

# NZBGet Skill

Manage Usenet downloads via NZBGet's JSON-RPC API.

## Configuration

Environment variables (already configured):
- `NZBGET_URL`: http://nzbget:6789
- `NZBGET_USERNAME`: API username
- `NZBGET_PASSWORD`: API password

## Authentication

Use HTTP Basic Auth with JSON-RPC:
```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"methodname","params":[],"id":1}'
```

## Core Operations

### Get Status

```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"status","id":1}' | jq '.result | {
    DownloadRate,
    FreeDiskSpaceMB,
    DownloadedSizeMB,
    RemainingSizeMB,
    DownloadPaused
  }'
```

### List Queue (Active Downloads)

```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"listgroups","id":1}' | jq '.result[] | {
    NZBName,
    Status,
    RemainingSizeMB,
    DownloadedSizeMB
  }'
```

### Get History

```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"history","params":[false],"id":1}' | jq '.result[:5] | .[] | {
    Name,
    Status,
    DownloadedSizeMB
  }'
```

### Add NZB by URL

```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{
    "method": "append",
    "params": [
      "filename.nzb",
      "http://url-to-nzb-file",
      "",
      0,
      false,
      false,
      "",
      0,
      "SCORE"
    ],
    "id": 1
  }'
```

### Pause/Resume Downloads

```bash
# Pause all
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"pausedownload","id":1}'

# Resume all  
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"resumedownload","id":1}'
```

### Get Config

```bash
curl -s "$NZBGET_URL/jsonrpc" \
  -u "$NZBGET_USERNAME:$NZBGET_PASSWORD" \
  -d '{"method":"config","id":1}' | jq '.result[] | select(.Name | test("Dir|Path"))'
```

## Common Workflows

### "What's downloading?"
1. Call `listgroups` for active queue
2. Call `status` for overall stats

### "Check if download finished"
1. Call `history` and look for item by name
2. Check Status field
