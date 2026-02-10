# JIT Approval Service

A Go HTTP service that brokers JIT (Just-In-Time) credential access between the Prometheus agent and HashiCorp Vault, with human-in-the-loop approval via Telegram inline buttons.

## Architecture

```
Agent → POST /request → Approval Service → Telegram inline buttons → Noah approves/denies
Agent → GET /status/:id → polls for result
On approve → Backend Registry → Dynamic backend or Vault → mint credential → return to agent
```

### Dynamic vs Static Backends

The service supports two types of credential backends:

**Dynamic backends** generate real ephemeral credentials directly from the upstream service (e.g., a Grafana service account token, a Plex transient token). These are preferred because they:
- Provide native credentials the service understands
- Have service-managed lifecycle (auto-expiry, auto-cleanup)
- Don't require Vault token management on the consumer side

**Static backend** (fallback) mints a scoped Vault token that the consumer uses to read the static secret from Vault. This is the original behavior and is used for services without a dynamic backend.

**Fallback behavior:** If a dynamic backend fails (service unreachable, auth error, etc.), the service automatically falls back to the static Vault token approach and logs a warning.

### Backend Assignment

| Resource | Backend | Credential Type |
|----------|---------|----------------|
| Grafana | Dynamic (GrafanaBackend) | Service account token with expiration |
| InfluxDB | Dynamic (InfluxDBBackend) | Read-only authorization token (auto-cleaned after TTL) |
| Plex | Static (Vault) | API key from Vault (transient token API deprecated in Plex 1.43+) |
| Home Assistant | Dynamic (HomeAssistantBackend) | OAuth access token (30 min, via refresh flow) |
| Radarr, Sonarr, Ombi, NZBGet, Deluge, Paperless, GitLab | Static (Vault) | Scoped Vault token |
| Gmail | Static (Vault) | Email + app password from Vault |

## API

### `POST /request`

Submit a credential request.

```bash
curl -X POST http://jit-approval-svc:8080/request \
  -H "Content-Type: application/json" \
  -H "X-JIT-API-Key: $JIT_API_KEY" \
  -d '{
    "requester": "prometheus",
    "resource": "homeassistant",
    "tier": 2,
    "reason": "Check sensor readings"
  }'
```

Requires `X-JIT-API-Key` header matching the configured `JIT_API_KEY`. Returns 401 if missing or invalid.

Response:
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "pending"
}
```

### `GET /status/:id`

Poll for approval status. Credential is returned exactly once (claimed on first poll after approval).

Requires `X-JIT-API-Key` header (same as `/request`). Returns 401 if missing or invalid.

```bash
curl http://jit-approval-svc:8080/status/req-a1b2c3d4e5f6 \
  -H "X-JIT-API-Key: $JIT_API_KEY"
```

Response (pending):
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "pending"
}
```

Response (approved, first poll — dynamic backend):
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "approved",
  "credential": {
    "token": "glsa_xxxxx",
    "lease_ttl": "5m0s",
    "metadata": {
      "backend": "grafana",
      "type": "service_account_token",
      "service_account_id": "5"
    }
  }
}
```

Response (approved, first poll — static backend):
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "approved",
  "credential": {
    "token": "hvs.XXXXX",
    "lease_ttl": "15m0s",
    "metadata": {
      "backend": "static",
      "type": "vault_token"
    }
  }
}
```

### `GET /health`

Health check.

```json
{
  "status": "ok",
  "vault": "ok",
  "requests_in_store": 3
}
```

### `POST /telegram/webhook`

Telegram webhook endpoint for inline button callbacks. Validates `X-Telegram-Bot-Api-Secret-Token` header.

## Tier System

| Tier | TTL | Approval | Resources |
|------|-----|----------|-----------|
| 1 | 15 min | Auto | Grafana, InfluxDB, Plex, Radarr, Sonarr, Ombi, NZBGet, Deluge, Paperless, Gmail |
| 2 | 30 min | Manual (Telegram) | GitLab, Home Assistant |
| 3 | 60 min | Manual (Telegram) | Critical (reserved) |

Tiers represent **approval trust level**, not backend type. Dynamic vs static credential backends are orthogonal to the tier system.

## Configuration

All configuration via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VAULT_ADDR` | Yes | `https://vault.lab.nkontur.com:8200` | Vault server address |
| `VAULT_ROLE_ID` | Yes | — | Vault AppRole role ID |
| `VAULT_SECRET_ID` | Yes | — | Vault AppRole secret ID |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token for approval messages |
| `TELEGRAM_CHAT_ID` | No | `8531859108` | Noah's Telegram chat ID |
| `TELEGRAM_WEBHOOK_SECRET` | Yes | — | Secret for webhook verification |
| `JIT_API_KEY` | Yes | — | API key for `/request` endpoint auth (passed as `X-JIT-API-Key` header) |
| `LISTEN_ADDR` | No | `:8080` | HTTP listen address |
| `REQUEST_TIMEOUT` | No | `300` | Seconds before pending requests auto-timeout |
| `ALLOWED_REQUESTERS` | No | `prometheus` | Comma-separated requester allowlist |
| `HA_URL` | No | `https://homeassistant.lab.nkontur.com` | Home Assistant URL for dynamic backend |
| `GRAFANA_URL` | No | `https://grafana.lab.nkontur.com` | Grafana URL for dynamic backend |
| `PLEX_URL` | No | `http://plex.lab.nkontur.com:32400` | Plex URL for dynamic backend |
| `INFLUXDB_URL` | No | `https://influxdb.lab.nkontur.com` | InfluxDB URL for dynamic backend |

### Disabling Dynamic Backends

To disable a dynamic backend and force static Vault token mode, set the URL to empty:

```bash
HA_URL=""  # Disables Home Assistant dynamic backend, falls back to Vault token
```

If the URL is not set at all, the default homelab URL is used and the dynamic backend is enabled.

### Vault Secrets for Dynamic Backends

Dynamic backends read their upstream credentials from Vault:

| Backend | Vault Path | Required Fields |
|---------|-----------|-----------------|
| Home Assistant | `homelab/data/docker/homeassistant` | `refresh_token`, `client_id` |
| Grafana | `homelab/data/docker/grafana` | `jit_admin_token`, `service_account_id` |
| Plex | `homelab/data/docker/plex` | `token` |
| InfluxDB | `homelab/data/docker/influxdb` | `admin_token`, `org_id` |
| Gmail | `homelab/data/email/gmail` | `email`, `app_password` |

## Build

```bash
cd jit-approval-svc
go build -o jit-approval-svc .
```

## Test

```bash
go test ./...
```

## Docker

```bash
docker build -t jit-approval-svc .
docker run -e VAULT_ROLE_ID=... -e VAULT_SECRET_ID=... -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_WEBHOOK_SECRET=... -e JIT_API_KEY=... jit-approval-svc
```

See `docker-compose.snippet.yml` for homelab integration.

## Logging

All output is structured JSON to stdout, designed for the Docker Loki log driver.

```json
{"ts":"2026-02-06T14:30:00Z","level":"info","event":"backend_credential_minted","backend":"grafana","resource":"grafana","tier":0,"ttl":"5m0s"}
```

Events logged: `request_received`, `approval_sent`, `approved`, `denied`, `timeout`, `token_issued`, `backend_credential_minted`, `credential_claimed`, `dynamic_backend_failed_fallback`, `backend_registered`, `influxdb_cleanup_start`, `influxdb_cleanup_success`, `http_request`, `health_check`, `error`.

## Security

- Webhook endpoint validates Telegram secret token (required)
- `/request` and `/status/:id` endpoints require `X-JIT-API-Key` header authentication
- Only configured requesters can submit requests
- Only callbacks from configured Telegram chat ID are processed
- Credentials returned exactly once (claim-on-first-poll)
- No credential data in logs (request_id and resource only, never tokens)
- Request auto-timeout (5 min default)
- Dynamic backend failures fall back to static Vault tokens (defense in depth)
- All HTTP clients have 10-second timeouts
- InfluxDB tokens are auto-cleaned via goroutine after TTL expiry

## Design Doc

See `/docs/jit-access-design.md` for the full architecture and security analysis.
