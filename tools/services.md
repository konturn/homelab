# Service Access Patterns

## What's in env vs what needs JIT

### Direct env vars (always available)
| Var | Purpose |
|-----|---------|
| `GITLAB_TOKEN` | GitLab PAT (full api scope — needs scoping down) |
| `VAULT_APPROLE_ROLE_ID` | Vault AppRole auth |
| `VAULT_APPROLE_SECRET_ID` | Vault AppRole auth |
| `VAULT_ADDR` | Vault URL |
| `OPENAI_API_KEY` | LLM calls (used by OpenClaw) |
| `TELEGRAM_BOT_TOKEN` | Messaging (used by OpenClaw) |
| `BRAVE_API_KEY` | Web search |
| `ACLAWDEMY_API_KEY` | Aclawdemy API |
| `CLAWDBOT_GATEWAY_TOKEN` | OpenClaw gateway |

### Removed (need JIT now)
| Former var | JIT resource | Tier |
|-----------|-------------|------|
| `GMAIL_EMAIL` / `GMAIL_APP_PASSWORD` | `gmail` | T1 |
| `HASS_TOKEN` / `HASS_URL` | `homeassistant` | T2 |
| `IPMI_USER` / `IPMI_PASSWORD` | `ipmi` | T2 |

### JIT Resources Quick Reference

**T1 (auto-approve, 15min TTL):**

| Resource | Backend | What you get |
|----------|---------|-------------|
| grafana | Dynamic | SA token → use as `Authorization: Bearer $TOKEN` |
| influxdb | Dynamic | Auth token → use as `Authorization: Token $TOKEN` |
| plex | Static Vault | Vault token → read `docker/plex` → `api_key` |
| radarr | Static Vault | Vault token → read `docker/radarr` → `api_key` |
| sonarr | Static Vault | Vault token → read `docker/sonarr` → `api_key` |
| ombi | Static Vault | Vault token → read `docker/ombi` → `api_key` |
| nzbget | Static Vault | Vault token → read `docker/nzbget` → `api_key` |
| deluge | Static Vault | Vault token → read `docker/deluge` → `api_key` |
| paperless | Static Vault | Vault token → read `docker/paperless` → `api_key` |
| prowlarr | Static Vault | Vault token → read `docker/prowlarr` → `api_key` |
| mqtt | Static Vault | Vault token → read `mqtt` → credentials |
| gmail | Static Vault | Vault token → read `docker/gmail` or similar |

**T2 (Telegram approval, 30min TTL):**

| Resource | Backend | What you get |
|----------|---------|-------------|
| homeassistant | Dynamic OAuth | HA access token |
| tailscale | Dynamic OAuth | Short-lived API token |
| gitlab | Dynamic | Project access token |
| ssh | Dynamic cert | Signed SSH certificate (see SSH section) |
| vault | Dynamic | Scoped Vault token (specify paths in request) |
| pihole | Static Vault | DNS management creds |
| ipmi | Static Vault | IPMI credentials |

### Usage with jit-lib.sh

```bash
source /home/node/.openclaw/workspace/tools/jit-lib.sh

# T1 dynamic (instant)
TOKEN=$(jit_grafana_token)
curl -s -H "Authorization: Bearer $TOKEN" "https://grafana.lab.nkontur.com/api/dashboards/home"

# T1 static Vault (instant)
API_KEY=$(jit_service_key radarr)
curl -s -H "X-Api-Key: $API_KEY" "https://radarr.lab.nkontur.com/api/v3/movie"

# T2 (polls until Noah approves)
TOKEN=$(jit_get homeassistant 2 "Check sensor data")
curl -s -H "Authorization: Bearer $TOKEN" "https://homeassistant.lab.nkontur.com/api/states"
```

## SSH via JIT (T2)

```bash
# 1. Generate ephemeral keypair
ssh-keygen -t ed25519 -f /tmp/jit-ssh-key -N "" -q

# 2. Request JIT SSH cert
source /home/node/.openclaw/workspace/tools/jit-lib.sh
PUB_KEY=$(cat /tmp/jit-ssh-key.pub)
RESP=$(jit_request ssh 2 "SSH to router" "{\"public_key\": \"$PUB_KEY\"}")
REQ_ID=$(echo "$RESP" | jq -r '.request_id')

# 3. Poll until approved, then extract cert (use python3 — jq breaks on embedded newlines)
RESP_JSON=$(jit_status "$REQ_ID")
python3 -c "
import json, sys
d = json.loads(sys.argv[1])
with open('/tmp/jit-ssh-key', 'w') as f:
    f.write(d['credential']['token'])
with open('/tmp/jit-ssh-key-cert.pub', 'w') as f:
    f.write(d['credential']['metadata']['certificate'])
" "$RESP_JSON"
chmod 600 /tmp/jit-ssh-key /tmp/jit-ssh-key-cert.pub

# 4. Connect
ssh -o StrictHostKeyChecking=no -i /tmp/jit-ssh-key claude@10.4.0.1
```

**SSH credential structure is DIFFERENT from other JIT resources:**
- `credential.token` = ephemeral private key (NOT the cert)
- `credential.metadata.certificate` = signed SSH certificate

## GitLab

| Setting | Value |
|---------|-------|
| Instance | https://gitlab.lab.nkontur.com |
| User | moltbot |
| Token | `$GITLAB_TOKEN` (env) |
| Homelab project ID | 4 |
| Memory project ID | 9 |
| Permissions | Developer + admin_terraform_state on homelab |

**Shared lib:** `source /home/node/.openclaw/workspace/skills/gitlab/lib.sh`

## Loki (Log Queries)

**Direct internal access (preferred — no auth needed, no JIT):**
Moltbot is on the same Docker network as Loki. Use internal DNS:
```bash
# Query logs by container name
START=$(($(date +%s) - 3600))000000000
END=$(date +%s)000000000
curl -s -G "http://loki:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={container_name="CONTAINER_NAME"}' \
  --data-urlencode "limit=50" \
  --data-urlencode "start=$START" \
  --data-urlencode "end=$END" | jq -r '.data.result[].values[][1]'

# Filter with pattern
curl -s -G "http://loki:3100/loki/api/v1/query_range" \
  --data-urlencode 'query={container_name="CONTAINER_NAME"} |~ "error|ERROR"' \
  --data-urlencode "limit=50" \
  --data-urlencode "start=$START" \
  --data-urlencode "end=$END" | jq -r '.data.result[].values[][1]'

# Check readiness
curl -s "http://loki:3100/ready"

# List all label values (find container names)
curl -s "http://loki:3100/loki/api/v1/label/container_name/values" | jq -r '.data[]'
```

**⚠️ DO NOT use `https://loki.lab.nkontur.com`** — nginx has a 301 redirect loop bug. Always use `http://loki:3100` directly.

**Via Grafana (alternative, needs JIT):**
```bash
TOKEN=$(jit_grafana_token)
curl -s "https://grafana.lab.nkontur.com/api/ds/query" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [{"refId":"A","datasource":{"uid":"P8E80F9AEF21F6940"},"expr":"{container_name=\"CONTAINER\"} |~ \"PATTERN\"","queryType":"range","maxLines":50}],
    "from": "now-1h", "to": "now"
  }'
```

## Home Assistant

```bash
TOKEN=$(jit_get homeassistant 2 "Control devices")
curl -s -H "Authorization: Bearer $TOKEN" "https://homeassistant.lab.nkontur.com/api/states"
```

## Vault

**My policy (`moltbot-ops`):** Only `agents/*` and `moltbot/*` paths.
**For anything else:** Use JIT vault resource with specific paths.

```bash
TOKEN=$(jit_get vault 2 "Read infrastructure/tailscale" '{"vault_paths": [{"path": "homelab/data/infrastructure/tailscale", "capabilities": ["read"]}]}')
curl -s -H "X-Vault-Token: $TOKEN" "$VAULT_ADDR/v1/homelab/data/infrastructure/tailscale"
```
