---
name: ombi
description: Ombi media request management. Search, request, and track movies/TV shows. Integrates with Radarr/Sonarr.
---

# Ombi Skill

Manage media requests through Ombi. Users can request movies and TV shows, which get sent to Radarr/Sonarr for download.

## Configuration

Environment variables (already configured):
- `OMBI_URL`: http://ombi:3579
- `OMBI_API_KEY`: API key for authentication

## Authentication

Include in all requests:
```
ApiKey: $OMBI_API_KEY
```

## Request Status

### Get Request Counts

```bash
curl -s "$OMBI_URL/api/v1/Request/count" -H "ApiKey: $OMBI_API_KEY"
# Returns: {"pending":4,"approved":251,"available":553}
```

## Movie Requests

### List All Movie Requests

```bash
curl -s "$OMBI_URL/api/v1/Request/movie" -H "ApiKey: $OMBI_API_KEY" | jq 'length'
# 654 requests
```

### List Movie Requests (Paginated)

```bash
# count = number of items, position = offset
curl -s "$OMBI_URL/api/v1/Request/movie?count=10&position=0" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[] | {id, title, approved, available}'
```

### Filter by Status

```bash
# statusFilter: 1=pending, 2=approved, 3=available, 4=denied
curl -s "$OMBI_URL/api/v1/Request/movie?statusFilter=1" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[] | {title, requestedDate}'
```

### Search Movies

```bash
curl -s "$OMBI_URL/api/v1/Search/movie/inception" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[0] | {title, releaseDate, id}'
```

### Request a Movie

```bash
curl -s -X POST "$OMBI_URL/api/v1/Request/movie" \
  -H "ApiKey: $OMBI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"theMovieDbId": 27205}'
```

### Approve a Movie Request

```bash
curl -s -X POST "$OMBI_URL/api/v1/Request/movie/approve" \
  -H "ApiKey: $OMBI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id": 123}'
```

### Deny a Movie Request

```bash
curl -s -X PUT "$OMBI_URL/api/v1/Request/movie/deny" \
  -H "ApiKey: $OMBI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"id": 123, "reason": "Already have it"}'
```

## TV Requests

### List All TV Requests

```bash
curl -s "$OMBI_URL/api/v1/Request/tv" -H "ApiKey: $OMBI_API_KEY" | jq 'length'
# 151 requests
```

### List TV Requests (Paginated)

```bash
curl -s "$OMBI_URL/api/v1/Request/tv?count=10&position=0" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[] | {id, title}'
```

### Search TV Shows

```bash
curl -s "$OMBI_URL/api/v1/Search/tv/breaking+bad" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[0] | {title, id, firstAired}'
```

### Request a TV Show

```bash
curl -s -X POST "$OMBI_URL/api/v1/Request/tv" \
  -H "ApiKey: $OMBI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tvDbId": 81189,
    "requestAll": true
  }'
```

### Request Specific Seasons

```bash
curl -s -X POST "$OMBI_URL/api/v1/Request/tv" \
  -H "ApiKey: $OMBI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "tvDbId": 81189,
    "seasons": [
      {"seasonNumber": 1, "episodes": [{"episodeNumber": 1}, {"episodeNumber": 2}]}
    ]
  }'
```

## Status Check

### Server Status

```bash
curl -s "$OMBI_URL/api/v1/Status" -H "ApiKey: $OMBI_API_KEY"
# Returns: 200
```

## Common Workflows

### "Request a movie"
1. Search: `GET /api/v1/Search/movie/{title}`
2. Get theMovieDbId from results
3. POST to `/api/v1/Request/movie` with the ID

### "What's pending approval?"
```bash
curl -s "$OMBI_URL/api/v1/Request/movie?statusFilter=1" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[] | {title, requestedDate}'
```

### "What's been requested but not available?"
```bash
curl -s "$OMBI_URL/api/v1/Request/movie" -H "ApiKey: $OMBI_API_KEY" | \
  jq '[.[] | select(.available == false and .approved == true)] | .[] | {title}'
```

### "Approve all pending requests"
```bash
# Get pending, then approve each
curl -s "$OMBI_URL/api/v1/Request/movie?statusFilter=1" \
  -H "ApiKey: $OMBI_API_KEY" | jq '.[].id' | while read id; do
    curl -s -X POST "$OMBI_URL/api/v1/Request/movie/approve" \
      -H "ApiKey: $OMBI_API_KEY" \
      -H "Content-Type: application/json" \
      -d "{\"id\": $id}"
done
```

## Notes

- Use v1 API (v2 endpoints have issues)
- theMovieDbId for movies, tvDbId for TV shows
- Status filters: 1=pending, 2=approved, 3=available, 4=denied
- Approved requests are sent to Radarr/Sonarr automatically
- Available means the file exists in the media library
