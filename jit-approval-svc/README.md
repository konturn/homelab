# JIT Approval Service

A Go HTTP service that brokers JIT (Just-In-Time) credential access between the Prometheus agent and HashiCorp Vault, with human-in-the-loop approval via Telegram inline buttons.

## Architecture

```
Agent → POST /request → Approval Service → Telegram inline buttons → Noah approves/denies
Agent → GET /status/:id → polls for result
On approve → Approval Service → Vault API → mint short-lived token → return to agent
```

## API

### `POST /request`

Submit a credential request.

```bash
curl -X POST http://jit-approval-svc:8080/request \
  -H "Content-Type: application/json" \
  -d '{
    "requester": "prometheus",
    "resource": "homeassistant",
    "tier": 1,
    "reason": "Check sensor readings"
  }'
```

Response:
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "pending"
}
```

### `GET /status/:id`

Poll for approval status. Credential is returned exactly once (claimed on first poll after approval).

```bash
curl http://jit-approval-svc:8080/status/req-a1b2c3d4e5f6
```

Response (pending):
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "pending"
}
```

Response (approved, first poll):
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "approved",
  "credential": {
    "token": "hvs.XXXXX",
    "lease_ttl": "30m0s"
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
| 0 | 5 min | Auto | Grafana, InfluxDB, Tautulli |
| 1 | 15 min | Auto | Home Assistant, Plex, Radarr, Sonarr |
| 2 | 30 min | Manual (Telegram) | GitLab API, Portainer, Docker |
| 3 | 60 min | Manual (Telegram) | SSH, Vault admin, network config |

## Configuration

All configuration via environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `VAULT_ADDR` | Yes | `https://vault.lab.nkontur.com:8200` | Vault server address |
| `VAULT_ROLE_ID` | Yes | — | Vault AppRole role ID |
| `VAULT_SECRET_ID` | Yes | — | Vault AppRole secret ID |
| `TELEGRAM_BOT_TOKEN` | Yes | — | Telegram bot token for approval messages |
| `TELEGRAM_CHAT_ID` | No | `8531859108` | Noah's Telegram chat ID |
| `TELEGRAM_WEBHOOK_SECRET` | No | — | Secret for webhook verification |
| `LISTEN_ADDR` | No | `:8080` | HTTP listen address |
| `REQUEST_TIMEOUT` | No | `300` | Seconds before pending requests auto-timeout |
| `ALLOWED_REQUESTERS` | No | `prometheus` | Comma-separated requester allowlist |

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
docker run -e VAULT_ROLE_ID=... -e VAULT_SECRET_ID=... -e TELEGRAM_BOT_TOKEN=... jit-approval-svc
```

See `docker-compose.snippet.yml` for homelab integration.

## Logging

All output is structured JSON to stdout, designed for the Docker Loki log driver.

```json
{"ts":"2026-02-06T14:30:00Z","level":"info","event":"request_received","request_id":"req-abc123","requester":"prometheus","resource":"homeassistant","tier":1,"reason":"Check sensor readings"}
```

Events logged: `request_received`, `approval_sent`, `approved`, `denied`, `timeout`, `token_issued`, `credential_claimed`, `http_request`, `health_check`, `error`.

## Security

- Webhook endpoint validates Telegram secret token
- Only configured requesters can submit requests
- Only callbacks from configured Telegram chat ID are processed
- Credentials returned exactly once (claim-on-first-poll)
- No credential data in logs (request_id and resource only, never tokens)
- Request auto-timeout (5 min default)

## Design Doc

See `/docs/jit-access-design.md` for the full architecture and security analysis.
