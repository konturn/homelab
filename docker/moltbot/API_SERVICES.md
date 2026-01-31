# Moltbot API Services

This document lists the external services that Moltbot can access via their APIs.

## Environment Variables Required

Set these in the router's environment (e.g., via `.env` file or secrets management):

### Media Management (arr stack)

| Service | URL (internal) | Environment Variables |
|---------|----------------|----------------------|
| **Radarr** | http://radarr:7878 | `RADARR_API_KEY` — Settings > General > API Key |
| **Sonarr** | http://sonarr:8989 | `SONARR_API_KEY` — Settings > General > API Key |
| **Prowlarr** | http://prowlarr:9696 | `PROWLARR_API_KEY` — Settings > General > API Key |
| **Plex** | http://plex:32400 | `PLEX_TOKEN` — [Get X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| **Ombi** | http://ombi:3579 | `OMBI_API_KEY` — Settings > Ombi > API Key |

### Download Clients

| Service | URL (internal) | Environment Variables |
|---------|----------------|----------------------|
| **NZBGet** | http://nzbget:6789 | `NZBGET_USERNAME`, `NZBGET_PASSWORD` — Settings > Security |
| **Deluge** | http://deluge:8112 | `DELUGE_PASSWORD` — WebUI password |

### Document Management

| Service | URL (internal) | Environment Variables |
|---------|----------------|----------------------|
| **Paperless-NGX** | http://paperless-ngx:8000 | `PAPERLESS_TOKEN` — Admin > Auth Tokens |

### Monitoring

| Service | URL (internal) | Environment Variables |
|---------|----------------|----------------------|
| **InfluxDB** | http://influxdb:8086 | `INFLUXDB_TOKEN` — Data > Tokens |

## Example API Usage

### Radarr — Search for Movies
```bash
curl -H "X-Api-Key: $RADARR_API_KEY" "$RADARR_URL/api/v3/movie"
```

### Sonarr — Search for TV Shows
```bash
curl -H "X-Api-Key: $SONARR_API_KEY" "$SONARR_URL/api/v3/series"
```

### Plex — Get Libraries
```bash
curl "$PLEX_URL/library/sections?X-Plex-Token=$PLEX_TOKEN"
```

### Paperless — List Documents
```bash
curl -H "Authorization: Token $PAPERLESS_TOKEN" "$PAPERLESS_URL/api/documents/"
```

## Network Notes

All services run on the `internal` network (10.3.x.x). The container names resolve via Docker DNS within the same network.
