---
name: radarr
description: Movie collection management via Radarr API. Search, add, monitor movies. Check download queue and history.
---

# Radarr Skill

Manage movies via Radarr's API.

## Configuration

Environment variables (already configured):
- `RADARR_URL`: http://radarr:7878
- `RADARR_API_KEY`: API key for authentication

## Authentication

Include in all requests:
```
X-Api-Key: $RADARR_API_KEY
```

## Core Operations

### List All Movies

```bash
curl -s "$RADARR_URL/api/v3/movie" -H "X-Api-Key: $RADARR_API_KEY" | jq 'length'
# Returns ~5000+ movies
```

### Search for a Movie

```bash
# Search by title
curl -s "$RADARR_URL/api/v3/movie/lookup?term=inception" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[] | {title, year, tmdbId, overview}'
```

### Get Movie Details

```bash
curl -s "$RADARR_URL/api/v3/movie/{id}" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '{title, year, hasFile, path, sizeOnDisk}'
```

### Check Download Queue

```bash
curl -s "$RADARR_URL/api/v3/queue" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.records[] | {title: .title, status, sizeleft, timeleft}'
```

### Upcoming Releases (Calendar)

```bash
START=$(date +%Y-%m-%d)
END=$(date -d '+30 days' +%Y-%m-%d)
curl -s "$RADARR_URL/api/v3/calendar?start=$START&end=$END" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[] | {title, inCinemas, physicalRelease}'
```

### Add a Movie

```bash
# First search for TMDB ID, then add
curl -s -X POST "$RADARR_URL/api/v3/movie" \
  -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tmdbId": 27205,
    "title": "Inception",
    "qualityProfileId": 1,
    "rootFolderPath": "/movies",
    "monitored": true,
    "addOptions": {"searchForMovie": true}
  }'
```

### Get Quality Profiles

```bash
curl -s "$RADARR_URL/api/v3/qualityprofile" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[] | {id, name}'
```

### Get Root Folders

```bash
curl -s "$RADARR_URL/api/v3/rootfolder" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[] | {id, path, freeSpace}'
```

### Trigger Search for Movie

```bash
curl -s -X POST "$RADARR_URL/api/v3/command" \
  -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "MoviesSearch", "movieIds": [123]}'
```

### History (Recent Activity)

```bash
curl -s "$RADARR_URL/api/v3/history?pageSize=10" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.records[] | {title: .movie.title, eventType, date}'
```

## Common Workflows

### "Add this movie and download it"
1. Search: `lookup?term=movie+name`
2. Get tmdbId from results
3. POST to `/movie` with `searchForMovie: true`

### "What's downloading?"
1. GET `/queue` for current downloads
2. Check `.records[].status` and `.timeleft`

### "Find something to watch"
1. GET `/movie` and filter by `hasFile: true`
2. Or check `/calendar` for upcoming releases
