# Moltbot API Services

This document lists the external services that Moltbot can access via their APIs.

## How Moltbot gets credentials

Service API keys are **not** in the `openclaw-gateway` environment. They live in
Vault and Moltbot obtains them on demand through the JIT approval service
(`docker/jit-approval-svc`), using the helpers in the workspace's
`tools/jit-lib.sh`:

- `jit_service_key <service>`: tier 1 (auto-approved) scoped Vault token, then
  reads the key from `homelab/data/docker/<service>`.
- `jit_grafana_token`, `jit_influxdb_token`: tier 1 dynamic tokens.
- `gitlab_token`: tier 1 short-lived GitLab token for the moltbot user
  (resource `gitlab-moltbot`); there is no static GitLab token.

The only secrets in the container environment are the gateway's own (OpenAI,
Telegram, Brave, Claude OAuth, gateway token) and the Vault AppRole used to log
in for JIT. Ansible's `fetch-vault-secrets` role reads those from Vault and
renders them into `docker-compose.yml`.

### To add a new service key

1. Store the key in Vault under `homelab/data/docker/<service>` (field `api_key`).
2. Add the resource to `resourceTier` in
   `docker/jit-approval-svc/internal/vault/vault.go` and make sure the tier's
   Vault policy (`terraform/vault/policies.tf`) covers the path.

## Services

### Media Management (arr stack)

| Service | Container URL | JIT resource (tier) | How to Get |
|---------|---------------|---------------------|------------|
| **Radarr** | http://radarr:7878 | `radarr` (1) | Settings > General > API Key |
| **Sonarr** | http://sonarr:8989 | `sonarr` (1) | Settings > General > API Key |
| **Prowlarr** | http://prowlarr:9696 | `prowlarr` (1) | Settings > General > API Key |
| **Plex** | http://plex:32400 | `plex` (1) | [Get X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/) |
| **Ombi** | http://ombi:3579 | `ombi` (1) | Settings > Ombi > API Key |

### Download Clients

| Service | Container URL | JIT resource (tier) | How to Get |
|---------|---------------|---------------------|------------|
| **NZBGet** | http://nzbget:6789 | `nzbget` (1) | Settings > Security |
| **Deluge** | http://deluge:8112 | `deluge` (1) | WebUI password |

### Document Management

| Service | Container URL | JIT resource (tier) | How to Get |
|---------|---------------|---------------------|------------|
| **Paperless-NGX** | http://paperless-ngx:8000 | `paperless` (2, Telegram approval) | Minted by the JIT service's Paperless backend |

### Monitoring

| Service | Container URL | JIT resource (tier) | How to Get |
|---------|---------------|---------------------|------------|
| **InfluxDB** | http://influxdb:8086 | `influxdb` (1) | Read-only token minted by the JIT service |

## Example API Usage

```bash
source ~/.openclaw/workspace/tools/jit-lib.sh
```

### Radarr — Search for Movies
```bash
curl -H "X-Api-Key: $(jit_service_key radarr)" "http://radarr:7878/api/v3/movie"
```

### Sonarr — Search for TV Shows
```bash
curl -H "X-Api-Key: $(jit_service_key sonarr)" "http://sonarr:8989/api/v3/series"
```

### Plex — Get Libraries
```bash
curl "http://plex:32400/library/sections?X-Plex-Token=$(jit_service_key plex)"
```

### Paperless — List Documents
```bash
TOKEN=$(jit_get paperless 2 "List documents")   # waits for Noah's approval
curl -H "Authorization: Token $TOKEN" "http://paperless-ngx:8000/api/documents/"
```

## Network Notes

All services run on the `internal` network (10.3.x.x). The container names resolve via Docker DNS within the same network.
