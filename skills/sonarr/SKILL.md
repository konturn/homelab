---
name: sonarr
description: TV show collection management via Sonarr API. Search, add, monitor series. Check download queue, calendar, and history.
---

# Sonarr Skill

Manage TV shows via Sonarr's API (v3).

## Configuration

Environment variables (already configured):
- `SONARR_URL`: http://sonarr:8989
- `SONARR_API_KEY`: API key for authentication

## Authentication

Include in all requests:
```
X-Api-Key: $SONARR_API_KEY
```

## Core Operations

### List All Series

```bash
curl -s "$SONARR_URL/api/v3/series" -H "X-Api-Key: $SONARR_API_KEY" | jq 'length'
# Returns series count (413+ shows)
```

### Get Series Details

```bash
curl -s "$SONARR_URL/api/v3/series/{id}" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '{title, year, path, monitored, episodeFileCount, status}'
```

### Search for a Series

```bash
# Search by title
curl -s "$SONARR_URL/api/v3/series/lookup?term=breaking+bad" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.[] | {title, year, tvdbId, overview}'
```

### Get Episodes for a Series

```bash
curl -s "$SONARR_URL/api/v3/episode?seriesId={id}" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.[] | {seasonNumber, episodeNumber, title, hasFile}'
```

### Check Download Queue

```bash
curl -s "$SONARR_URL/api/v3/queue" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.records[] | {title, status, sizeleft, timeleft}'
```

### Upcoming Episodes (Calendar)

```bash
START=$(date +%Y-%m-%d)
END=$(date -d '+7 days' +%Y-%m-%d)
curl -s "$SONARR_URL/api/v3/calendar?start=$START&end=$END" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.[] | {seriesTitle: .series.title, episodeTitle: .title, airDate: .airDateUtc}'
```

### Add a Series

```bash
# First search for TVDB ID, then add
curl -s -X POST "$SONARR_URL/api/v3/series" \
  -H "X-Api-Key: $SONARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tvdbId": 81189,
    "title": "Breaking Bad",
    "qualityProfileId": 4,
    "rootFolderPath": "/tv",
    "monitored": true,
    "seasonFolder": true,
    "addOptions": {
      "searchForMissingEpisodes": true
    }
  }'
```

### Get Quality Profiles

```bash
curl -s "$SONARR_URL/api/v3/qualityprofile" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.[] | {id, name}'
# Common: 1=Any, 4=HD-1080p, 5=Ultra-HD
```

### Get Root Folders

```bash
curl -s "$SONARR_URL/api/v3/rootfolder" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.[] | {id, path, freeSpace}'
# Default: id=1, path="/tv"
```

### Trigger Search for Series/Season

```bash
# Search entire series
curl -s -X POST "$SONARR_URL/api/v3/command" \
  -H "X-Api-Key: $SONARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "SeriesSearch", "seriesId": 123}'

# Search specific season
curl -s -X POST "$SONARR_URL/api/v3/command" \
  -H "X-Api-Key: $SONARR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "SeasonSearch", "seriesId": 123, "seasonNumber": 1}'
```

### History (Recent Activity)

```bash
curl -s "$SONARR_URL/api/v3/history?pageSize=10&includeSeries=true&includeEpisode=true" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '.records[] | {seriesTitle: .series.title, episodeTitle: .episode.title, eventType, date}'
```

### System Status

```bash
curl -s "$SONARR_URL/api/v3/system/status" \
  -H "X-Api-Key: $SONARR_API_KEY" | jq '{version, startTime}'
```

## Common Workflows

### "Add this show and download it"
1. Search: `series/lookup?term=show+name`
2. Get tvdbId from results
3. POST to `/series` with `searchForMissingEpisodes: true`

### "What's downloading?"
1. GET `/queue` for current downloads
2. Check `.records[].status` and `.timeleft`

### "What's airing this week?"
1. GET `/calendar?start=today&end=+7days`
2. Shows episodes with air dates

### "Check on a specific show"
1. GET `/series` and filter by title
2. GET `/episode?seriesId=X` for episode list
3. Check `hasFile` for downloaded status
