# JIT Approval Service

A Go HTTP service that brokers JIT (Just-In-Time) credential access between the Prometheus agent and HashiCorp Vault, with human-in-the-loop approval via Telegram inline buttons.

## Architecture

```
Agent → POST /request → Approval Service → Telegram inline buttons → Noah approves/denies
Agent → GET /status/:id → polls for result
On approve (or immediately, for tier 1) → Backend Registry → Dynamic backend or Vault → mint credential → return to agent
```

### Dynamic vs Static Backends

The service supports two types of credential backends:

**Dynamic backends** generate real ephemeral credentials directly from the upstream service (e.g., a Grafana service account token, a Plex transient token). These are preferred because they:
- Provide native credentials the service understands
- Have service-managed lifecycle (auto-expiry, auto-cleanup)
- Don't require Vault token management on the consumer side

**Static backend** mints a Vault token with the resource's own policy, `jit-static-<resource>`, which can read only that resource's secret (`staticResourcePaths` in `internal/vault/vault.go`, `local.jit_static_resources` in `terraform/vault/policies.tf`). The tier decides approval and TTL, not what the token can read. Only resources in `staticResourcePaths` use it; a resource whose dynamic backend is not configured is refused rather than given a Vault token.

The secrets the dynamic backends mint from (upstream admin tokens, OAuth clients) and the service's own secret are listed in `BrokerOnlyPaths` (`internal/backend/vault_dynamic.go`). Only the service's AppRole policy can read them: requests for them through the `vault` resource are refused, and every per-request policy denies them.

**Failure behavior:** If a dynamic backend fails (service unreachable, auth error, etc.), the request fails and `dynamic_backend_failed` is logged. There is no fallback to a static Vault token.

### Backend Assignment

| Resource | Backend | Credential Type |
|----------|---------|----------------|
| `grafana` | Dynamic (GrafanaBackend) | Service account token with expiration |
| `influxdb` | Dynamic (InfluxDBBackend) | Read-only authorization token (auto-cleaned after TTL) |
| `homeassistant` | Dynamic (HomeAssistantBackend) | OAuth access token (30 min, via refresh flow) |
| `gitlab` | Dynamic (GitLabBackend) | Project access token (project 4 by default), revoked best-effort when the request expires |
| `gitlab-moltbot` | Dynamic (GitLabAgentBackend) | Impersonation token for the agent's own GitLab user, revoked by a sweeper after the TTL (1 h) |
| `paperless` | Dynamic (PaperlessBackend) | Paperless API token (persistent per user; access gated by approval) |
| `tailscale` | Dynamic (TailscaleBackend) | OAuth access token (client credentials) |
| `gmail-read`, `gmail-send` | Dynamic (GmailBackend) | Google OAuth2 access token (read-only / send scope) |
| `ssh-*` (router, satellite, zwave, macmini, user hosts; `-elevated` variants) | Dynamic (SSHBackend) | Vault-signed SSH certificate |
| `vault` | Dynamic (VaultDynamicBackend) | Vault token with a per-request policy for the requested `vault_paths` |
| Plex, Radarr, Sonarr, Prowlarr, Ombi, NZBGet, Deluge, Tautulli, MQTT, Pi-hole, IPMI | Static (Vault) | Scoped Vault token |

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

### `POST /webhook/refresh`

Re-registers the Telegram webhook (e.g. after a public IP change). Requires `X-JIT-API-Key`; rate limited to once per 5 minutes.

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

### Action prompts (Unix socket, host clients only)

When `ACTIONS_SOCKET` is set, the service also serves a small API on that Unix
socket (`internal/actions`). It lets a trusted program on the Docker host show
Noah a message with inline buttons and learn which one he pressed; the service
only relays the decision and never acts on it. The socket directory is a host
bind mount (`/persistent_data/application/jit-approval-svc/actions`, uid 1000,
mode 0700), so host filesystem permissions are the access control. It is not
reachable from any container network and needs no API key.

| Method | Path | Body / query | Notes |
|--------|------|--------------|-------|
| POST | `/v1/prompts` | `{id, text, buttons: [{action, label}], ttl_seconds}` | `id` `^[a-z0-9][a-z0-9-]{3,39}$`, `action` `^[a-z][a-z0-9-]{1,15}$`, at most 4 buttons, HTML text (caller escapes), default TTL 72 h |
| POST | `/v1/prompts/{id}` | `{text, buttons, ttl_seconds}` | edit the message; non-empty buttons re-arm it, empty buttons close it |
| GET | `/v1/decisions?wait=N` | | presses since the last call, each delivered once; long poll up to 25 s |
| POST | `/v1/notify` | `{text}` | plain message, no buttons |
| GET | `/v1/health` | | |

```bash
curl --unix-socket /persistent_data/application/jit-approval-svc/actions/actions.sock \
  -d '{"id":"p-0001","text":"Test","buttons":[{"action":"ok","label":"OK"}]}' http://x/v1/prompts
```

Buttons carry callback data `act:<action>:<id>` and arrive on the existing
webhook, after its secret-token and chat-ID checks. A press is accepted once,
only for a button the prompt offered and before it expires; the message is
then edited to "requested, working…" until the client updates it with the
outcome. Prompts live in memory: after a restart, old buttons answer "no
longer active" and the client re-sends what it still needs.

## Tier System

| Tier | TTL | Approval | Resources |
|------|-----|----------|-----------|
| 1 | 15 min | Auto | grafana, influxdb, plex, radarr, sonarr, prowlarr, ombi, nzbget, deluge, tautulli, mqtt, gmail-read, ssh-macmini, gitlab-moltbot (1 h) |
| 2 | 30 min | Manual (Telegram) | gitlab, homeassistant, paperless, gmail-send, tailscale, pihole, vault, ipmi, ssh-router, ssh-satellite, ssh-zwave, ssh-nkontur/konoahko/konturn (8 h), `ssh-*-elevated` |
| 3 | 60 min | Manual (Telegram) | Critical (reserved) |

The resource-to-tier map is `resourceTier` in `internal/vault/vault.go`; a request below a resource's minimum tier is rejected. Per-resource TTL overrides are in `internal/config/config.go`. Requests are rate limited per resource per requester: `JIT_RATE_LIMIT_MAX` per `JIT_RATE_LIMIT_WINDOW_MIN` minutes (code defaults 50 per 15; the deployment sets 72).

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
| `TELEGRAM_WEBHOOK_URL` | No | — | If set, the webhook is registered with Telegram at startup |
| `LISTEN_ADDR` | No | `:8080` | HTTP listen address |
| `REQUEST_TIMEOUT` | No | `1800` | Seconds before pending requests auto-timeout (deployed with `300`) |
| `ALLOWED_REQUESTERS` | No | `prometheus` | Comma-separated requester allowlist |
| `JIT_RATE_LIMIT_MAX` | No | `50` | Requests per resource per requester per window (deployment: 72) |
| `JIT_RATE_LIMIT_WINDOW_MIN` | No | `15` | Rate-limit window in minutes |
| `ACTIONS_SOCKET` | No | — | Unix socket path for the action-prompt API |
| `HA_URL` | No | `https://homeassistant.lab.nkontur.com` | Home Assistant URL for dynamic backend |
| `GRAFANA_URL` | No | `https://grafana.lab.nkontur.com` | Grafana URL for dynamic backend |
| `INFLUXDB_URL` | No | `https://influxdb.lab.nkontur.com` | InfluxDB URL for dynamic backend |
| `GITLAB_URL` | No | `https://gitlab.lab.nkontur.com` | GitLab URL for the `gitlab` and `gitlab-moltbot` backends |
| `GITLAB_ADMIN_TOKEN` | No | — | Maintainer token for the `gitlab` backend (project access tokens) |
| `GITLAB_PROJECT_ID` | No | `4` | Default project for `gitlab` tokens |
| `GITLAB_AGENT_USER` | No | `moltbot` | User the `gitlab-<user>` backend impersonates |
| `GITLAB_AGENT_ADMIN_TOKEN` | No | — | Administrator token for impersonation tokens; the backend is disabled without it |
| `GITLAB_AGENT_SCOPES` | No | `api,read_api,read_repository,write_repository` | Scope allowlist for agent tokens |
| `TAILSCALE_API_URL` | No | `https://api.tailscale.com` | Tailscale API for the dynamic backend |
| `PAPERLESS_URL` | No | — | Paperless-ngx URL; enables the dynamic backend |
| `SSH_VAULT_PATH` | No | `ssh-client-signer` | Vault SSH secrets engine mount |
| `GOOGLE_TOKEN_URL` | No | `https://oauth2.googleapis.com/token` | Google token endpoint for the Gmail backends |

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
| InfluxDB | `homelab/data/docker/influxdb` | `admin_token`, `org_id` |
| Paperless | `homelab/data/docker/paperless` | `username`, `password` |
| Tailscale | `homelab/data/infrastructure/tailscale` | `oauth_client_id`, `oauth_client_secret` |
| Gmail | `homelab/data/email/google-oauth` | `client_id`, `client_secret`, `refresh_token_read` / `refresh_token_send` |

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

See the `jit-approval-svc` service in `docker/docker-compose.yml` for the homelab deployment.

## Logging

All output is structured JSON to stdout, designed for the Docker Loki log driver.

```json
{"ts":"2026-02-06T14:30:00Z","level":"info","event":"backend_credential_minted","backend":"grafana","resource":"grafana","tier":0,"ttl":"5m0s"}
```

Events logged include: `request_received`, `approval_sent`, `auto_approve`, `approved`, `denied`, `timeout`, `token_issued`, `backend_credential_minted`, `credential_claimed`, `dynamic_backend_failed`, `request_rate_limited`, `backend_registered`, `influxdb_cleanup_start`, `influxdb_cleanup_success`, `http_request`.

## Security

- Webhook endpoint validates Telegram secret token (required)
- `/request` and `/status/:id` endpoints require `X-JIT-API-Key` header authentication
- Only configured requesters can submit requests
- Only callbacks from configured Telegram chat ID are processed
- Credentials returned exactly once (claim-on-first-poll)
- No credential data in logs (request_id and resource only, never tokens)
- Request auto-timeout (`REQUEST_TIMEOUT`, 5 min as deployed)
- Rate limit: `JIT_RATE_LIMIT_MAX` requests per resource per requester per 15 minutes (72 in the deployment)
- All HTTP clients have 10-second timeouts
- InfluxDB tokens are auto-cleaned via goroutine after TTL expiry

## Design Doc

See `/docs/jit-access-design.md` for the full architecture and security analysis.
