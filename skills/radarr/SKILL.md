---
name: radarr
description: Movie collection management via Radarr API. Search, add, monitor movies. Check download queue and history.
---

# Radarr API

Movie collection manager for Usenet and BitTorrent. Monitors RSS feeds, interfaces with download clients, and organizes your library.

## Environment

```bash
RADARR_URL="http://radarr.lab.nkontur.com"  # Or internal Docker network URL
RADARR_API_KEY="your-api-key"               # Settings → General → API Key
```

## Authentication

All requests require the API key via header:
```bash
-H "X-Api-Key: $RADARR_API_KEY"
```

## Common Workflows

### Search for a Movie
```bash
# Search by title (returns TMDB results)
curl -s "$RADARR_URL/api/v3/movie/lookup?term=inception" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[0] | {title, year, tmdbId, overview}'
```

### Check if Movie Exists in Library
```bash
# List all movies in library
curl -s "$RADARR_URL/api/v3/movie" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[] | {id, title, year, hasFile, monitored}'

# Get specific movie by ID
curl -s "$RADARR_URL/api/v3/movie/123" \
  -H "X-Api-Key: $RADARR_API_KEY"
```

### Add Movie to Library
```bash
# First, lookup the movie to get full metadata
MOVIE=$(curl -s "$RADARR_URL/api/v3/movie/lookup?term=inception" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.[0]')

# Add it (requires qualityProfileId and rootFolderPath)
curl -s -X POST "$RADARR_URL/api/v3/movie" \
  -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$(echo $MOVIE | jq '. + {
    qualityProfileId: 1,
    rootFolderPath: "/movies",
    monitored: true,
    addOptions: {searchForMovie: true}
  }')"
```

### Check Download Queue
```bash
curl -s "$RADARR_URL/api/v3/queue" \
  -H "X-Api-Key: $RADARR_API_KEY" | jq '.records[] | {title, status, sizeleft, timeleft}'
```

### Trigger Manual Search
```bash
curl -s -X POST "$RADARR_URL/api/v3/command" \
  -H "X-Api-Key: $RADARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "MoviesSearch", "movieIds": [123]}'
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

## Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v3/movie` | GET | List all movies |
| `/api/v3/movie/{id}` | GET | Get specific movie |
| `/api/v3/movie` | POST | Add movie |
| `/api/v3/movie/{id}` | DELETE | Remove movie |
| `/api/v3/movie/lookup` | GET | Search TMDB |
| `/api/v3/queue` | GET | Download queue |
| `/api/v3/history` | GET | Download history |
| `/api/v3/command` | POST | Trigger commands |
| `/api/v3/qualityprofile` | GET | Quality profiles |
| `/api/v3/rootfolder` | GET | Root folders |
| `/api/v3/tag` | GET | Tags |
| `/api/v3/system/status` | GET | System info |

## Commands Reference

Commands are triggered via POST to `/api/v3/command`:

| Command | Payload | Purpose |
|---------|---------|---------|
| `MoviesSearch` | `{movieIds: [1,2,3]}` | Search for specific movies |
| `RefreshMovie` | `{movieIds: [1]}` | Refresh movie metadata |
| `RssSync` | `{}` | Trigger RSS sync |
| `Backup` | `{}` | Trigger backup |

## Local Notes

**Instance URL:** (set after MR !9 merges)
**Quality Profiles:** (discover with API call)
**Root Folder:** (discover with API call)

## Gotchas & Lessons

*(Update this section as you learn things)*

- TMDB ID is the key identifier for movies
- `addOptions.searchForMovie: true` triggers immediate search when adding
- Quality profile IDs are instance-specific — always query first
- Delete endpoint has optional `deleteFiles=true` query param

## External Docs

- [Radarr Wiki](https://wiki.servarr.com/radarr)
- [API Docs](https://radarr.video/docs/api/) (Swagger spec at `/api/v3/swagger.json`)
