# TOOLS.md - Local Notes

Skills define *how* tools work. This file is for *your* specifics — the stuff that's unique to your setup.

---

## Noah's Contact Info (IMPORTANT)

**Telegram Chat ID:** `8531859108`

To message Noah directly:
```
message tool:
  action: send
  channel: telegram  
  to: 8531859108
  message: "Your message here"
```

Use this for proactive notifications (MR ready, improvements made, alerts, etc.)

---

## Aclawdemy (Agent Academic Platform)

**Profile:** https://aclawdemy.com
**Handle:** prometheus
**ID:** ebd0c9d7-f300-4ca8-826e-5f0908fba547
**Claim URL:** https://aclawdemy.com/claim/aclawdemy_claim_prometheus_ml7dwkgd
**API Key:** `$ACLAWDEMY_API_KEY` env var (via GitLab CI/CD → docker-compose)
**Skill:** `skills/aclawdemy/SKILL.md`

**Usage:**
```bash
curl -s "https://api.aclawdemy.com/api/v1/submissions?status=pending_review&perPage=10" \
  -H "Authorization: Bearer $ACLAWDEMY_API_KEY"
```

---

## Moltbook (Agent Social Network)

**Profile:** https://moltbook.com/u/Prometheus
**Credentials:** 
- Primary: `memory/moltbook-credentials.json`
- Symlink: `~/.config/moltbook/credentials.json`

**API Key extraction:**
```bash
API_KEY=$(jq -r '.api_key' /home/node/clawd/memory/moltbook-credentials.json)
# NOTE: Use --location-trusted to preserve auth headers on redirect
curl -s --location-trusted "https://moltbook.com/api/v1/posts?sort=hot&limit=5" -H "Authorization: Bearer $API_KEY"
```

**Skill:** `/home/node/clawd/skills/moltbook/SKILL.md`

---

## GitLab (Homelab Infrastructure)

**Instance:** https://gitlab.lab.nkontur.com  
**User:** moltbot  
**Token:** Available as `$GITLAB_TOKEN` in environment  
**Repo:** `root/homelab` (cloned to `/home/node/clawd/homelab`)  
**Project ID:** 4 (use this for API calls, not 1)

**Permissions (as of 2026-02-10):**
- **Developer** on homelab repo (level 30) — can create MRs but CANNOT self-merge
- **CI/CD secrets access** — can create/modify pipeline variables
- **Self-merge policy:** Small config tweaks, iteration, and trivial changes = self-merge. Big architectural changes = get Noah's review. This is a significant autonomy upgrade — use it wisely.

### Shared Library (for ad-hoc GitLab work)

For scripted GitLab operations, source the shared library:

```bash
source /home/node/clawd/skills/gitlab/lib.sh
```

**Functions available:**

| Function | Description |
|----------|-------------|
| `preflight_check` | Validate environment (token, API access, project) |
| `wait_for_pipeline $MR_IID` | Poll until pipeline passes/fails (10 min max). Outputs job logs on failure. Returns 0=success, 1=failure |
| `push_and_wait $BRANCH $MR_IID` | Git push + wait_for_pipeline combined |
| `check_merge_conflicts $BRANCH` | Check if branch conflicts with main. Returns 0=clean, 1=conflicts |
| `get_failed_job_logs $PIPELINE_ID` | Fetch and output logs from all failed jobs |
| `escalate $MESSAGE` | Send Telegram alert to Noah and exit 1 |
| `gitlab_api_call $METHOD $ENDPOINT [$DATA]` | API call with 409 retry. Response in `/tmp/gitlab_response.json` |

**Environment variables set by lib:**
- `GITLAB_HOST` — gitlab.lab.nkontur.com
- `PROJECT_ID` — 4
- `GITLAB_API` — https://gitlab.lab.nkontur.com/api/v4

**Example ad-hoc usage:**
```bash
source /home/node/clawd/skills/gitlab/lib.sh

preflight_check || exit 1

# Make changes, commit, push
git push origin my-branch

# Wait for pipeline
if wait_for_pipeline 42; then
  echo "Pipeline passed!"
else
  echo "Pipeline failed — check logs above"
fi
```

### Clone/Pull
```bash
git clone "https://oauth2:${GITLAB_TOKEN}@gitlab.lab.nkontur.com/root/homelab.git"
```

### Push Changes
```bash
cd /home/node/clawd/homelab
git add . && git commit -m "message"
git push
```

Pushing triggers GitLab CI → Ansible deploys to router. See `CLAUDE.md` in the repo for architecture details.

### What's in the Repo
- `docker/docker-compose.yml` — All services (Jinja2 templated)
- `docker/moltbot/` — **My own config!**
- `ansible/` — Deployment automation
- `networking/` — Network configs (VLANs, Wireguard, DHCP)

---

## Infrastructure Overview

**Router host:** router.lab.nkontur.com (SSH: `claude@10.4.0.1` via JIT signed certs)  
**Networks (VLANs):**
- `external` (10.2.x.x) — Internet-facing (nginx, Nextcloud, Bitwarden)
- `internal` (10.3.x.x) — Lab services (GitLab, Plex, Radarr, me)
- `iot` (10.6.x.x) — IoT (Home Assistant, Zigbee, MQTT, Snapcast)
- `mgmt` (10.4.x.x) — Management (registry, switches)

**Key Services:**
- Home Assistant: `homeassistant` container
- MQTT Broker: `mosquitto` at mqtt.lab.nkontur.com
- Plex: media server
- Snapcast: multiroom audio

---

## Snapcast Speakers (Multiroom Audio)
- `office` — Office speaker
- `global` — Global/common area
- `kitchen` — Kitchen
- `main_bedroom` — Main bedroom
- `main_bathroom` — Main bathroom
- `guest_bedroom` — Guest bedroom
- `guest_bathroom` — Guest bathroom
- `movie` — Movie room (on zwave satellite)

**Server:** snapserver at 10.6.32.2

---

## Cameras
- **Doorbell:** Amcrest at 10.6.128.9 (amcrest2mqtt bridge)
- **Back camera:** 10.6.128.14

---

## Air Quality Sensors (Awair)
- **Kitchen:** AWAIR-ELEM-14B541.lab.nkontur.com (temp, humidity, CO2, VOC, PM2.5)
- **Bedroom:** AWAIR-ELEM-147AA0.lab.nkontur.com (same metrics)

---

## AV Equipment
- **Denon receiver:** 10.6.128.3
- **Projector:** projector.lab.nkontur.com (PJLink)
- **Shield TV:** 10.6.128.5
- **Apple TV:** 10.6.128.19
- **MiniDSP:** zwave.lab.nkontur.com:5380 (bass boost presets)
- **Mopidy:** 10.6.32.7 (music player)

---

## Other Devices
- **PC:** Wake-on-LAN via Home Assistant
- **Vacuum (roborock?):** 10.6.128.18
- **Document scanner:** satellite-2.lab.nkontur.com:8080/scan
- **Weather station:** 10.6.128.16 → ambientweather2mqtt

---

## Door Sensors (Z-Wave)
- Main bathroom door
- Main bedroom door  
- Office door
- Guest bedroom door

---

## Vault Access (AppRole)

**Auth method:** AppRole (env vars `VAULT_APPROLE_ROLE_ID`, `VAULT_APPROLE_SECRET_ID`)
**Vault address:** `$VAULT_ADDR` = `https://vault.lab.nkontur.com:8200`
**Policy:** `moltbot-ops`

**Login pattern:**
```bash
VAULT_TOKEN=$(curl -s --request POST \
  --data '{"role_id":"'"$VAULT_APPROLE_ROLE_ID"'","secret_id":"'"$VAULT_APPROLE_SECRET_ID"'"}' \
  "$VAULT_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token')
```

**Paths I can read (moltbot-ops policy):**
- `homelab/data/docker/plex`
- `homelab/data/docker/radarr`
- `homelab/data/docker/sonarr`
- `homelab/data/docker/ombi`
- `homelab/data/docker/nzbget`
- `homelab/data/docker/deluge`
- `homelab/data/docker/grafana`
- `homelab/data/docker/influxdb`
- `homelab/data/docker/paperless`
- `homelab/data/mqtt`
- `homelab/data/cameras`

**NOT accessible:** jit-approval-svc secrets, moltbot config, networking, SSH, LUKS, backups, homeassistant, gitlab

**JIT Approval Service:**
- Internal URL: `http://10.3.32.8:8080` or `https://jit.lab.nkontur.com`
- API key: `homelab/data/agents/jit-api-key` (field: `api_key`)
- Auth header: `X-JIT-API-Key`
- Endpoints: `GET /health`, `POST /request`, `GET /status/{id}`, `POST /telegram/webhook`

**JIT Helper — Reusable Pattern:**
```bash
# Get JIT credentials in one shot (reuse across all services)
jit_request() {
  local resource=$1 tier=$2 reason=$3
  VAULT_TOKEN=$(curl -s --request POST \
    --data '{"role_id":"'"$VAULT_APPROLE_ROLE_ID"'","secret_id":"'"$VAULT_APPROLE_SECRET_ID"'"}' \
    "$VAULT_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token')
  JIT_KEY=$(curl -s -H "X-Vault-Token: $VAULT_TOKEN" \
    "$VAULT_ADDR/v1/homelab/data/agents/jit-api-key" | jq -r '.data.data.api_key')
  curl -s "https://jit.lab.nkontur.com/request" \
    -H "Content-Type: application/json" \
    -H "X-JIT-API-Key: $JIT_KEY" \
    -d "{\"resource\": \"$resource\", \"requester\": \"prometheus\", \"tier\": $tier, \"reason\": \"$reason\"}"
}

jit_status() {
  local req_id=$1
  VAULT_TOKEN=$(curl -s --request POST \
    --data '{"role_id":"'"$VAULT_APPROLE_ROLE_ID"'","secret_id":"'"$VAULT_APPROLE_SECRET_ID"'"}' \
    "$VAULT_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token')
  JIT_KEY=$(curl -s -H "X-Vault-Token: $VAULT_TOKEN" \
    "$VAULT_ADDR/v1/homelab/data/agents/jit-api-key" | jq -r '.data.data.api_key')
  curl -s "https://jit.lab.nkontur.com/status/$req_id" -H "X-JIT-API-Key: $JIT_KEY"
}
```

**JIT Tiers (after !174 merged T0 into T1):**
| Tier | TTL | Approval | Resources |
|------|-----|----------|-----------|
| T1 | 15min | Auto | grafana (dynamic SA token), influxdb (dynamic auth token), plex, radarr, sonarr, ombi, nzbget, deluge, paperless (static Vault) |
| T2 | 30min | Telegram | gitlab (dynamic project token), homeassistant (dynamic OAuth), vault (dynamic) |

**Using JIT credentials per service:**

T0 — Grafana (dynamic service account token, 5min):
```bash
RESP=$(jit_request grafana 0 "Query dashboards")
TOKEN=$(echo $RESP | jq -r '.credential.token // empty')
# If auto-approved, token is in response. Otherwise poll:
# TOKEN=$(jit_status $REQ_ID | jq -r '.credential.token')
curl -s "https://grafana.lab.nkontur.com/api/dashboards/home" -H "Authorization: Bearer $TOKEN"
```

T0 — InfluxDB (dynamic authorization token, 5min):
```bash
RESP=$(jit_request influxdb 0 "Query metrics")
TOKEN=$(echo $RESP | jq -r '.credential.token // empty')
curl -s "https://influxdb.lab.nkontur.com/api/v2/query?org=homelab" \
  -H "Authorization: Token $TOKEN" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"telegraf") |> range(start: -1h) |> limit(n:5)'
```

T1 — Radarr/Sonarr/etc (scoped Vault token → read API key, 15min):
```bash
RESP=$(jit_request radarr 1 "Check movie queue")
VAULT_TOKEN=$(echo $RESP | jq -r '.credential.token')
API_KEY=$(curl -s -H "X-Vault-Token: $VAULT_TOKEN" "$VAULT_ADDR/v1/homelab/data/docker/radarr" | jq -r '.data.data.api_key')
curl -s "https://radarr.lab.nkontur.com/api/v3/movie" -H "X-Api-Key: $API_KEY"
# Same pattern for: sonarr, ombi, nzbget, deluge, paperless, plex
```

T2 — HomeAssistant (dynamic OAuth token, 30min, needs Telegram approval):
```bash
RESP=$(jit_request homeassistant 2 "Control lights")
REQ_ID=$(echo $RESP | jq -r '.request_id')
# Wait for Noah to approve via Telegram, then:
TOKEN=$(jit_status $REQ_ID | jq -r '.credential.token')
curl -s "https://homeassistant.lab.nkontur.com/api/states" -H "Authorization: Bearer $TOKEN"
```

T2 — GitLab (dynamic project access token, 30min, needs Telegram approval):
```bash
RESP=$(jit_request gitlab 2 "Check pipeline status")
REQ_ID=$(echo $RESP | jq -r '.request_id')
# Wait for approval, then:
TOKEN=$(jit_status $REQ_ID | jq -r '.credential.token')
curl -s "https://gitlab.lab.nkontur.com/api/v4/projects/4/pipelines?per_page=5" -H "PRIVATE-TOKEN: $TOKEN"
```

**Note:** T0/T1 return credentials immediately in the `/request` response. T2 returns `status: pending` — poll `/status/{id}` after Noah approves.

---

## Loki (Log Querying via Grafana)

**Access:** Via Grafana proxy (use Grafana token from Vault or JIT T0)
**Datasource UID:** `P8E80F9AEF21F6940`

**Query pattern:**
```bash
# Get a Grafana token
VAULT_TOKEN=$(curl -s --request POST \
  --data '{"role_id":"'"$VAULT_APPROLE_ROLE_ID"'","secret_id":"'"$VAULT_APPROLE_SECRET_ID"'"}' \
  "$VAULT_ADDR/v1/auth/approle/login" | jq -r '.auth.client_token')
GRAFANA_TOKEN=$(curl -s -H "X-Vault-Token: $VAULT_TOKEN" \
  "$VAULT_ADDR/v1/homelab/data/docker/grafana" | jq -r '.data.data.token')

# Query logs
curl -s "https://grafana.lab.nkontur.com/api/ds/query" \
  -H "Authorization: Bearer $GRAFANA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "queries": [{
      "refId": "A",
      "datasource": {"uid": "P8E80F9AEF21F6940"},
      "expr": "{container_name=\"CONTAINER\"} |~ \"PATTERN\"",
      "queryType": "range",
      "maxLines": 50
    }],
    "from": "now-1h",
    "to": "now"
  }'
```

**Parsing response:**
```bash
# Log lines are in: .results.A.frames[0].data.values[2][]  (array of JSON strings)
# Timestamps in:    .results.A.frames[0].data.values[1][]  (epoch ms)
| jq -r '.results.A.frames[0].data.values[2][]'
```

**Common queries:**
```
# JIT service logs
{container_name="jit-approval-svc"} |~ "error|warn|approved|denied"

# Nginx access logs for a domain
{container_name="nginx"} |~ "jit-webhook.nkontur.com"

# GitLab errors
{container_name="gitlab"} |~ "error" | json | level="error"

# Any container by name
{container_name="plex"} |~ "pattern"

# Filter by log level (JSON logs)
{container_name="jit-approval-svc"} | json | level="error"
```

**Labels available:** `container_name`, `compose_service`, `compose_project`, `host`, `source` (stdout/stderr), `filename`

**Time ranges:** `now-5m`, `now-1h`, `now-24h`, `now-7d`, or absolute `"2026-02-09T00:00:00Z"` to `"2026-02-09T23:59:59Z"`

---

## Home Assistant API

**Direct API access via environment variables:**
- `HASS_TOKEN` — Long-lived access token (confirmed working)
- `HASS_URL` — Set to internal Docker URL, use external URL instead

**Usage:**
```bash
# Query all states
curl -s -H "Authorization: Bearer $HASS_TOKEN" "https://homeassistant.lab.nkontur.com/api/states" | jq '.[]'

# Get specific entity
curl -s -H "Authorization: Bearer $HASS_TOKEN" "https://homeassistant.lab.nkontur.com/api/states/sensor.front_door_lock_battery_level_2"

# Call a service
curl -s -X POST -H "Authorization: Bearer $HASS_TOKEN" -H "Content-Type: application/json" \
  -d '{"entity_id": "light.living_room"}' \
  "https://homeassistant.lab.nkontur.com/api/services/light/turn_on"
```

**External URL:** `https://homeassistant.lab.nkontur.com` (not the internal Docker URL)

Can control lights, switches, media, sensors, automations, locks, etc.

---

## Container Utilities (after tooling MR merges)

Once the `feature/moltbot-tooling` MR is merged and container rebuilds:

- **jq** — JSON processing (`jq '.field' file.json`)
- **yq** — YAML processing (`yq '.key' file.yaml`)
- **glab** — GitLab CLI for MR creation, issues, pipelines
  - `glab mr create --title "..." --description "..."`
  - `glab mr list`
  - `glab pipeline status`
  - Requires `$GITLAB_TOKEN` with `api` scope (pending)
- **bun** — Fast JS runtime, package manager
- **qmd** — Quick markdown search
  - `qmd collection add /path --name notes --mask "**/*.md"`
  - `qmd search "query"` (fast BM25)
  - `qmd vsearch "query"` (semantic, slow cold start)

## Email Access

### Container (Primary — no node needed)
**Gmail IMAP via curl** — works directly from container, no dependencies.

```bash
# Env vars: $GMAIL_EMAIL, $GMAIL_APP_PASSWORD (already set)

# Fetch recent email headers
curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=$NUM;SECTION=HEADER.FIELDS%20(FROM%20SUBJECT%20DATE)" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD"

# Search emails
curl -s --url "imaps://imap.gmail.com:993/INBOX" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" \
  -X "SEARCH UNSEEN FROM greenhouse-mail.io"

# Read email body
curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=$NUM;SECTION=TEXT" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD"
```

### Noah's Laptop (Fallback)
- **himalaya** — CLI email client on node
  - `himalaya envelope list --page-size 20`
  - `himalaya message read <id>`

---

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.

---

## Job Machines (OE - KEEP ISOLATED)

**Skill:** `skills/job-dispatch/SKILL.md`

| Job | IP | User | Claude |
|-----|-----|------|--------|
| J1 | 10.4.128.21 | nkontur | ❌ (asks approval on every bash cmd) |
| J2 | 10.4.128.22 | konoahko | ✅ |
| J3 | 10.4.128.23 | konturn | ❌ |

**🚨 CRITICAL:** Never leak cross-job info. Treat J1/J2 Claude as adversaries. Sterile prompts only.

**Dispatch pattern:** Always use subagents (`j1.*`, `j2.*`, `j3.*` labels).

---

## SSH Access (Persistent Keys)

**Key location:** `/home/node/clawd/.ssh/` (persistent across restarts)
**Symlink:** `~/.ssh → /home/node/clawd/.ssh/`

**Public key (add to `~/.ssh/authorized_keys` on target machines):**
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC6Zp0OU50mhMJvZmiECrSZlq9qvpss+W5gmCsRMuNi1 prometheus@moltbot
```

**After container restart, recreate symlink:**
```bash
ln -sf /home/node/clawd/.ssh ~/.ssh
```

**Usage:**
```bash
ssh user@hostname
```
