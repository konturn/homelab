# Moltbot API Services

This document lists the external services that Moltbot can access via their APIs.

## How Moltbot gets credentials

Service API keys are **not** in the `openclaw-gateway` environment. They live in
Vault and Moltbot obtains them on demand through the JIT approval service
(`docker/jit-approval-svc`), using the helpers in the workspace's
`tools/jit-lib.sh`:

- `jitcurl <service> <template> <METHOD> <path>`: calls a media or download
  service through the JIT credential proxy with a short-lived grant; the
  service's key is never handed out (see "Services behind the credential
  proxy" below).
- `jit_service_key <service>`: only for the remaining static resources (MQTT,
  IPMI); it refuses the proxied services.
- `jit_grafana_token`, `jit_influxdb_token`: tier 1 dynamic tokens.
- `gitlab_token`: tier 1 short-lived GitLab token for the moltbot user
  (resource `gitlab-moltbot`); there is no static GitLab token.

The only secrets in the container environment are the gateway's own (OpenAI,
Telegram, Brave, Claude OAuth, gateway token) and the Vault AppRole used to log
in for JIT. Ansible's `fetch-vault-secrets` role reads those from Vault and
renders them into `docker-compose.yml`.

### Two agents, two JIT identities

OpenClaw runs two agents (`docs/design/openclaw-identities.md`): `noah`,
bound to Noah's Telegram DMs, and `ops`, which runs the crons and the
event-router hook. Each has its own JIT key and the broker identifies the
requester from it:

| Agent | Runs as | Key | Requester | T1 | T2 | T3 |
|---|---|---|---|---|---|---|
| `noah` | `node` | Vault `homelab/agents/openclaw-noah`, read with the AppRole | `openclaw-noah` | auto | auto + FYI line | tap |
| `ops` | `ops` (uid 2480) | `/home/ops/.config/openclaw/jit-api-key` (0400) | `openclaw-ops` | auto | tap | refused |

`tools/jit-lib.sh` and `jit-gitlab-token` pick the key from
`JIT_API_KEY_FILE` (set for ops by `ops-launch`) or from
`JIT_API_KEY_VAULT_PATH` (set for node in compose). The ops agent's
environment is rebuilt by `openclaw-claude`, so the AppRole, gateway and hook
tokens never reach it.

### Services behind the credential proxy

Radarr, Sonarr, Prowlarr, Plex, Ombi, NZBGet, Deluge, Tautulli and Pi-hole
are only reachable through the JIT credential proxy (`docker/jit-proxy`,
`docs/design/jit-credential-proxy.md`). Neither agent ever receives their
keys:

```bash
jitcurl <resource> <template[,template]> <METHOD> <path> [curl args]
jitcurl radarr read GET '/api/v3/movie/lookup?term=inception'
```

`jitcurl` gets a short-lived grant from the broker for the named templates.
Approval follows the tier, as for any JIT request, and the proxy adds the
real credential. Templates and what they allow are listed in
`docker/jit-approval-svc/README.md` ("Credential proxy") and in each skill.

### To add a new HTTP service

1. Store its key in Vault under `homelab/data/docker/<service>`.
2. Add a template file `docker/jit-proxy/templates/<service>.yml` (upstream,
   auth, deny list, templates) and the path to `local.jit_proxy_paths` in
   `terraform/vault/policies.tf`. A Go test keeps the two in sync.
3. Add the resource to `resourceTier` in
   `docker/jit-approval-svc/internal/vault/vault.go` (its minimum tier). Do
   not give it a static path.

## Services

### Media and download clients (credential proxy)

| Service | JIT resource (tier) | Templates |
|---------|---------------------|-----------|
| **Radarr**, **Sonarr** | `radarr`, `sonarr` (1) | `read`, `health`, `write`, `replace-file`*, `queue-remove`*, `remove-title`* (2) |
| **Prowlarr** | `prowlarr` (1) | `read`, `test-indexers` |
| **Plex** | `plex` (1) | `read`, `refresh` |
| **Ombi** | `ombi` (1) | `read`, `requests` |
| **Tautulli** | `tautulli` (1) | `read` |
| **NZBGet** | `nzbget` (1) | `read`, `control`, `remove`* |
| **Deluge** | `deluge` (1) | `read`, `control`, `remove`*, `remove-data`* (2) |
| **Pi-hole** | `pihole` (2) | `read`, `block-toggle`, `lists` |

\* single use: one request per grant.

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

### Radarr, Sonarr, Plex — through the proxy
```bash
jitcurl radarr read GET '/api/v3/movie/lookup?term=inception'
jitcurl sonarr read GET '/api/v3/series/lookup?term=breaking%20bad'
jitcurl plex read GET /library/sections -H 'Accept: application/json'
```

### Paperless — List Documents
```bash
TOKEN=$(jit_get paperless 2 "List documents")   # waits for Noah's approval
curl -H "Authorization: Token $TOKEN" "http://paperless-ngx:8000/api/documents/"
```

## Network Notes

All services run on the `internal` network (10.3.x.x). The container names resolve via Docker DNS within the same network.
