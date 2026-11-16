# JIT Approval Service

A Go HTTP service that brokers JIT (Just-In-Time) credential access between the OpenClaw agents and HashiCorp Vault, with human-in-the-loop approval via Telegram inline buttons. Each agent has its own key; the broker decides per requester and tier (see [Approval matrix](#approval-matrix) and `docs/design/openclaw-identities.md`).

## Architecture

```
Agent → POST /request (its own key) → requester × tier → auto | auto-fyi | approve | deny
  approve → Telegram inline buttons → Noah approves/denies
Agent → GET /status/:id → polls for result (only its own requests)
On approve, or immediately for auto / auto-fyi → Backend Registry → Dynamic backend or Vault → mint credential → return to agent
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

The `vault` backend writes a policy `jit-vault-<request id>` whose first line
records the token's expiry (`# jit-expires-at: <RFC 3339>`), and deletes it
5 minutes after the TTL. The request store and that timer are in memory, so
the broker also sweeps Vault at startup and hourly: a `jit-vault-*` policy
is deleted once its recorded expiry is 5 minutes past. A policy without the
marker (written by an older version) is deleted only after the broker has run
longer than the longest TTL it issues plus 5 minutes, since its token must
then have expired.

## API

### `POST /request`

Submit a credential request.

```bash
curl -X POST http://jit-approval-svc:8080/request \
  -H "Content-Type: application/json" \
  -H "X-JIT-API-Key: $JIT_API_KEY" \
  -d '{
    "resource": "homeassistant",
    "tier": 2,
    "reason": "Check sensor readings"
  }'
```

Requires an `X-JIT-API-Key` header holding one requester's key; the broker
identifies the requester from it (401 if it matches none). `requester` in the
body is optional: if present it must name the key's owner, or the request is
refused (403). A `tier` below the resource's tier is raised to it. A request
the approval matrix denies is refused with 403 and leaves nothing pending.

Response:
```json
{
  "request_id": "req-a1b2c3d4e5f6",
  "status": "pending"
}
```

### `GET /status/:id`

Poll for approval status. Credential is returned exactly once (claimed on first poll after approval).

Requires `X-JIT-API-Key` header (same as `/request`). Returns 401 if missing or invalid, and 404 for a request another requester made.

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

| Tier | TTL | Resources |
|------|-----|-----------|
| 1 | 15 min | grafana, influxdb, plex, radarr, sonarr, prowlarr, ombi, nzbget, deluge, tautulli, mqtt, gmail-read, ssh-macmini, gitlab-moltbot (1 h) |
| 2 | 30 min | gitlab, homeassistant, paperless, gmail-send, tailscale, pihole, vault (scoped paths), ssh-satellite, ssh-zwave, ssh-nkontur/konoahko/konturn (8 h) |
| 3 | 30 min | ssh-router, every `ssh-*-elevated`, ipmi |

Tiers are resource-based: the resource-to-tier map is `resourceTier` in `internal/vault/vault.go`, and a request below a resource's tier is raised to it. No Vault admin or policy path is reachable at any tier: the `vault` resource only grants read/list/create/update under `homelab/data/`, never a broker-only path. Per-resource TTL overrides are in `internal/config/config.go`. Requests are rate limited per resource per requester: `JIT_RATE_LIMIT_MAX` per `JIT_RATE_LIMIT_WINDOW_MIN` minutes (code defaults 50 per 15; the deployment sets 72).

Tiers represent **approval trust level**, not backend type. Dynamic vs static credential backends are orthogonal to the tier system.

### Approval matrix

Who gets what without a tap depends on the requester (`JIT_APPROVAL_MATRIX`, default `config.DefaultApprovalMatrix`):

| Requester | Tier 1 | Tier 2 | Tier 3 |
|---|---|---|---|
| `openclaw-noah` | auto | auto-fyi | approve |
| `openclaw-ops` | auto | approve | deny |
| anyone else | deny | deny | deny |

- **auto**: minted at once.
- **auto-fyi**: minted at once, plus one informational Telegram line (no buttons) per grant.
- **approve**: Telegram approve/deny buttons; nothing is minted until Noah taps Approve.
- **deny**: refused with 403; nothing is stored, so there is nothing to approve.

The config refuses a matrix in which tier 3 is auto or auto-fyi for anyone.

### Requester keys

Each requester has its own random key. The broker holds only SHA-256 hashes (`JIT_REQUESTER_KEYS`, from Vault `homelab/docker/jit-approval-svc` field `requester_keys`); the keys are in `homelab/agents/openclaw-<name>` and each is readable only by its own agent's process. Those paths, and the old shared one, are broker-only for the `vault` resource, so no grant can hand one agent another's key.

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
| `JIT_REQUESTER_KEYS` | Yes | — | `name:<sha256 hex of key>,...`, one per requester. The old shared `JIT_API_KEY` is refused at startup |
| `JIT_APPROVAL_MATRIX` | No | see [Approval matrix](#approval-matrix) | `name=<t1>,<t2>,<t3>;...` with `auto`, `auto-fyi`, `approve` or `deny` |
| `TELEGRAM_WEBHOOK_URL` | No | — | If set, the webhook is registered with Telegram at startup |
| `LISTEN_ADDR` | No | `:8080` | HTTP listen address |
| `REQUEST_TIMEOUT` | No | `1800` | Seconds before pending requests auto-timeout (deployed with `300`) |
| `JIT_RATE_LIMIT_MAX` | No | `50` | Requests per resource per requester per window (deployment: 72) |
| `JIT_RATE_LIMIT_WINDOW_MIN` | No | `15` | Rate-limit window in minutes |
| `ACTIONS_SOCKET` | No | — | Unix socket path for the action-prompt API |
| `HA_URL` | No | `https://homeassistant.lab.nkontur.com` | Home Assistant URL for dynamic backend |
| `GRAFANA_URL` | No | `https://grafana.lab.nkontur.com` | Grafana URL for dynamic backend |
| `INFLUXDB_URL` | No | `https://influxdb.lab.nkontur.com` | InfluxDB URL for dynamic backend |
| `GITLAB_URL` | No | `https://gitlab.lab.nkontur.com` | GitLab URL for the `gitlab` and `gitlab-moltbot` backends |
| `GITLAB_ADMIN_TOKEN` | No | — | Maintainer token for the `gitlab` backend (project access tokens) |
| `GITLAB_PROJECT_ID` | No | `4` | Default project for `gitlab` tokens |
| `GITLAB_AGENT_USER` | No | `moltbot` | User the `gitlab-<user>` backend impersonates. Only `gitlab-moltbot` has a tier-1 entry; any other `gitlab-<user>` needs tier 2 (approval) |
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

`go test` also runs every `Fuzz*` test over its seed corpus (the `f.Add`
seeds plus any `testdata/fuzz/<FuzzName>/` regression inputs), which is
what CI does. The fuzz and property tests (`*/fuzz_test.go`) cover the
security boundaries:

| Package | Fuzz/property tests | Property |
|---|---|---|
| handler | `FuzzHandleRequest`, `FuzzHandleRequestFields`, `TestAutoApprovalMatrix`, `TestRequestedTTLNeverExceedsMax` | no panic/500 on any body or key; for each requester nothing is minted without approval unless its matrix cell at the resource's tier is auto/auto-fyi (one FYI line each for auto-fyi), never at tier 3; minted TTL <= resource/tier max |
| handler | `TestOpsNeverGetsTier3`, `TestUnknownRequesterAlwaysDenied`, `TestRequesterFieldCannotImpersonate`, `TestStatusOnlyForOwnRequests` | ops is refused every tier-3 resource with nothing left to approve; unknown keys/requesters get nothing; the body cannot change the requester; one requester cannot poll or claim another's request |
| config | `FuzzApprovalMatrix`, `TestAuthenticate` | no valid matrix (and no `DecisionFor` result) mints tier 3; unknown requesters and tiers are denied; a key maps to exactly one requester |
| handler | `FuzzTelegramWebhook` | only `jit:approve:<id>` from the configured chat with the webhook secret approves |
| backend | `FuzzRegistryFor` | the agent backend serves exactly `gitlab-<configured user>` (no case/unicode/whitespace variants) |
| backend | `FuzzGitLabAgentScopes`, `FuzzGitLabAgentIdentity`, `FuzzGitLabAgentTTL`, `FuzzExpiryFromName`, `TestGitLabAgentSweepOnlyRevokesExpiredJITTokens` | scopes within the allowlist, never widened; impersonates only the configured username; TTL exact and recoverable by the sweeper |
| backend | `FuzzValidateVaultPaths`, `TestInfluxDBTierPermissions` | dynamic Vault policies stay under `homelab/data/` with allowed capabilities and no HCL injection; tier 1 InfluxDB tokens are read-only |
| vault, config | `FuzzPoliciesForResource`, `TestPrivilegedResourcesNeverTier1`, `TestTier3Resources`, `FuzzTTLFor` | static fallback fails closed for unknown resources/tiers; infra resources never tier 1; router shell, elevated SSH and IPMI are tier 3 |
| actions | `FuzzHandleCallback`, `FuzzPromptAPI` | only exact `act:<action>:<id>` decides a prompt; socket API validates input |

To fuzz for real (one target per run; a crasher is written to
`testdata/fuzz/<FuzzName>/` and should be committed as a regression seed):

```bash
go test ./internal/handler -run '^$' -fuzz '^FuzzHandleRequest$' -fuzztime 3m
```

## Docker

```bash
docker build -t jit-approval-svc .
docker run -e VAULT_ROLE_ID=... -e VAULT_SECRET_ID=... -e TELEGRAM_BOT_TOKEN=... -e TELEGRAM_WEBHOOK_SECRET=... -e JIT_REQUESTER_KEYS=<name>:<sha256 of key> jit-approval-svc
```

See the `jit-approval-svc` service in `docker/docker-compose.yml` for the homelab deployment.

## Logging

All output is structured JSON to stdout, designed for the Docker Loki log driver.

```json
{"ts":"2026-02-06T14:30:00Z","level":"info","event":"backend_credential_minted","backend":"grafana","resource":"grafana","tier":0,"ttl":"5m0s"}
```

Events logged include: `request_received`, `request_tier_raised`, `request_denied_by_policy`, `request_rejected_requester_mismatch`, `approval_sent`, `auto_approve`, `fyi_sent`, `approved`, `denied`, `timeout`, `token_issued`, `backend_credential_minted`, `credential_claimed`, `dynamic_backend_failed`, `request_rate_limited`, `backend_registered`, `influxdb_cleanup_start`, `influxdb_cleanup_success`, `http_request`.

## Security

- Webhook endpoint validates Telegram secret token (required)
- `/request` and `/status/:id` endpoints require `X-JIT-API-Key` header authentication; the key identifies the requester
- Unknown requesters are denied everything; a requester sees only its own requests
- Only callbacks from configured Telegram chat ID are processed
- Credentials returned exactly once (claim-on-first-poll)
- No credential data in logs (request_id and resource only, never tokens)
- Request auto-timeout (`REQUEST_TIMEOUT`, 5 min as deployed)
- Rate limit: `JIT_RATE_LIMIT_MAX` requests per resource per requester per 15 minutes (72 in the deployment)
- All HTTP clients have 10-second timeouts
- InfluxDB tokens are auto-cleaned via goroutine after TTL expiry

## Design Doc

See `/docs/jit-access-design.md` for the full architecture and security analysis.
