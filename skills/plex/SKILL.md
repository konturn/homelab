---
name: plex
description: Plex Media Server API. Browse libraries, search content, check sessions, manage playback.
---

# Plex Skill

Interact with Plex Media Server. Note: Plex returns XML by default.

## Configuration

Environment variables (already configured):
- `PLEX_URL`: http://plex:32400
- `PLEX_TOKEN`: X-Plex-Token for authentication

## Authentication

Include token in URL or header:
```bash
# URL parameter (common)
curl "$PLEX_URL/endpoint?X-Plex-Token=$PLEX_TOKEN"

# Or header
curl "$PLEX_URL/endpoint" -H "X-Plex-Token: $PLEX_TOKEN"
```

## Libraries

Current libraries:
- **1** = Movies
- **3** = Family Stuff (movies)
- **5** = Family Photos
- **6** = TV Shows

### List Libraries

```bash
curl -s "$PLEX_URL/library/sections?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'key="[0-9]+" type="[^"]+" title="[^"]+"'
```

### Browse Library Contents

```bash
# All items in library
curl -s "$PLEX_URL/library/sections/1/all?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+"' | head -20

# Recently added
curl -s "$PLEX_URL/library/sections/1/recentlyAdded?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+"' | head -10
```

## Search

### Global Search

```bash
curl -s "$PLEX_URL/search?query=inception&X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+"' | head -10
```

### Search with Type Filter

```bash
# type=1 for movies, type=2 for shows, type=4 for episodes
curl -s "$PLEX_URL/search?query=breaking&type=2&X-Plex-Token=$PLEX_TOKEN"
```

## Now Playing / Sessions

### Active Sessions

```bash
curl -s "$PLEX_URL/status/sessions?X-Plex-Token=$PLEX_TOKEN"
# Empty: <MediaContainer size="0">
# Playing: contains <Video> or <Track> elements with user info
```

### Transcode Sessions

```bash
curl -s "$PLEX_URL/transcode/sessions?X-Plex-Token=$PLEX_TOKEN"
```

## Server Info

### Server Status

```bash
curl -s "$PLEX_URL/?X-Plex-Token=$PLEX_TOKEN" | head -10
# Shows: friendlyName, version, platform, machineIdentifier
```

### Server Identity

```bash
curl -s "$PLEX_URL/identity?X-Plex-Token=$PLEX_TOKEN"
```

## Library Management

### Refresh Library

```bash
# Refresh specific library
curl -s "$PLEX_URL/library/sections/1/refresh?X-Plex-Token=$PLEX_TOKEN"
```

### Get Item Metadata

```bash
# Get detailed info for a specific item by ratingKey
curl -s "$PLEX_URL/library/metadata/{ratingKey}?X-Plex-Token=$PLEX_TOKEN"
```

### On Deck (Continue Watching)

```bash
curl -s "$PLEX_URL/library/onDeck?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+"'
```

## Playlists

### List Playlists

```bash
curl -s "$PLEX_URL/playlists?X-Plex-Token=$PLEX_TOKEN"
```

## Clients & Devices

### List Clients

```bash
curl -s "$PLEX_URL/clients?X-Plex-Token=$PLEX_TOKEN"
```

### List Devices

```bash
curl -s "$PLEX_URL/devices?X-Plex-Token=$PLEX_TOKEN"
```

## Common Workflows

### "What's playing right now?"
```bash
curl -s "$PLEX_URL/status/sessions?X-Plex-Token=$PLEX_TOKEN" | grep -E '(title=|User)'
```

### "What was recently added?"
```bash
curl -s "$PLEX_URL/library/recentlyAdded?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+"' | head -10
```

### "Find a specific movie"
```bash
curl -s "$PLEX_URL/search?query=movie+name&X-Plex-Token=$PLEX_TOKEN" | grep -oP 'title="[^"]+" year="[^"]+"'
```

### "What should I continue watching?"
```bash
curl -s "$PLEX_URL/library/onDeck?X-Plex-Token=$PLEX_TOKEN" | grep -oP 'grandparentTitle="[^"]*" title="[^"]*"'
```

## Notes

- All responses are XML by default
- Use `grep -oP` patterns to extract specific attributes
- `ratingKey` is the unique ID for items
- Library section numbers are stable but specific to this server
- Server is "Kontur--Plex" running version 1.43.0
