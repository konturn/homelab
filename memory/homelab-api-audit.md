# Homelab API Access Audit

**Date:** 2025-02-01
**Purpose:** Assess API access for all homelab services to maximize AI assistant capabilities

---

## Summary

| Status | Count | Services |
|--------|-------|----------|
| ✅ Working | 9 | Radarr, Sonarr, Plex, Paperless, InfluxDB, Ombi, NZBGet, Deluge, Home Assistant |
| ⚠️ Has Config, Needs Skill | 1 | Prowlarr |
| ❌ Needs Setup | 5 | Grafana, Nextcloud, Pi-hole, Zigbee2mqtt, Snapserver |
| 🚫 Skip | 8 | Bitwarden, Audioserve, Jackett, nginx, WordPress, Watchtower, Registry, Diagram |

---

## ✅ Fully Working (Env Vars + Skills + Tested)

### Radarr (Movie Management)
- **URL:** `http://radarr:7878`
- **Version:** 6.1.0.10316
- **Env Vars:** `RADARR_URL`, `RADARR_API_KEY` ✅
- **Skill:** `/home/node/clawd/skills/radarr/SKILL.md` ✅
- **Capabilities:** Search movies, add to library, trigger downloads, check queue

### Sonarr (TV Management)
- **URL:** `http://sonarr:8989`
- **Version:** 4.0.16.2944
- **Env Vars:** `SONARR_URL`, `SONARR_API_KEY` ✅
- **Skill:** `/home/node/clawd/skills/sonarr/SKILL.md` ✅
- **Capabilities:** Search shows, manage series, check episodes, download queue

### Plex (Media Server)
- **URL:** `http://plex:32400`
- **Env Vars:** `PLEX_URL`, `PLEX_TOKEN` ✅
- **Skill:** `/home/node/clawd/skills/plex/SKILL.md` ✅
- **Capabilities:** Search library, check sessions, see recently added, library refresh

### Paperless-ngx (Document Management)
- **URL:** `http://paperless-ngx:8000`
- **Env Vars:** `PAPERLESS_URL`, `PAPERLESS_TOKEN` ✅
- **Skill:** `/home/node/clawd/skills/paperless-ngx/SKILL.md` ✅
- **Capabilities:** Search documents, upload, download, manage tags/correspondents

### InfluxDB (Time Series Database)
- **URL:** `http://influxdb:8086`
- **Health:** Pass ✅
- **Env Vars:** `INFLUXDB_URL`, `INFLUXDB_TOKEN` ✅
- **Skill:** `/home/node/clawd/skills/influxdb/SKILL.md` ✅
- **Capabilities:** Query metrics, write data, manage buckets

### Ombi (Media Requests)
- **URL:** `http://ombi:3579`
- **Env Vars:** `OMBI_URL`, `OMBI_API_KEY` ✅
- **Skill:** `/home/node/clawd/skills/ombi/SKILL.md` ✅
- **Capabilities:** View/manage requests, check request status, approve/deny

### NZBGet (Usenet Downloader)
- **URL:** `http://nzbget:6789`
- **Env Vars:** `NZBGET_URL`, `NZBGET_USERNAME`, `NZBGET_PASSWORD` ✅
- **Skill:** `/home/node/clawd/skills/nzbget/SKILL.md` ✅
- **Capabilities:** Check downloads, pause/resume, view history

### Deluge (Torrent Client)
- **URL:** `http://deluge:8112`
- **Env Vars:** `DELUGE_URL`, `DELUGE_PASSWORD` ✅
- **Skill:** `/home/node/clawd/skills/deluge/SKILL.md` ✅
- **Capabilities:** Check torrents, add/remove, pause/resume

### Home Assistant
- **URL:** `http://homeassistant:8123`
- **Env Vars:** `HASS_URL`, `HASS_TOKEN` ✅
- **Access:** MCP (Model Context Protocol) integration
- **Capabilities:** Control devices, automations, sensors, scenes

---

## ⚠️ Has Config, Needs Documentation

### Prowlarr (Indexer Management)
- **URL:** `http://prowlarr:9696`
- **Version:** 2.3.0.5236 (tested, working!)
- **Env Vars:** `PROWLARR_URL`, `PROWLARR_API_KEY` ✅
- **Skill:** ❌ None exists
- **Action Required:** Create skill at `/home/node/clawd/skills/prowlarr/SKILL.md`
- **API Pattern:** Same as Radarr/Sonarr (*arr family)
  ```bash
  curl "$PROWLARR_URL/api/v1/indexer" -H "X-Api-Key: $PROWLARR_API_KEY"
  ```
- **Usefulness:** HIGH - Can search indexers, check status, useful for "find X release" queries

---

## ❌ Needs Setup (Service exists, no API access)

### Grafana (Dashboards & Visualization)
- **URL:** `http://grafana:3000` (internal) / `https://grafana.lab.nkontur.com`
- **Current Auth:** Admin user/password in environment
- **API Available:** Yes - full HTTP API
- **What's Needed:**
  1. Create a Service Account in Grafana UI (Settings → Service Accounts)
  2. Generate Service Account Token
  3. Add to moltbot environment: `GRAFANA_URL`, `GRAFANA_TOKEN`
- **Usefulness:** MEDIUM - Query dashboards, check alerts, but InfluxDB is the actual data source

### Nextcloud (Cloud Storage/Files)
- **URL:** `http://nextcloud` (internal) / `https://nkontur.com` (external)
- **API Available:** Yes - WebDAV + OCS REST API
- **What's Needed:**
  1. Create an App Password in Nextcloud (Settings → Security → Devices & Sessions)
  2. Add to moltbot environment: `NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD`
  3. Create skill for file operations
- **API Examples:**
  ```bash
  # WebDAV file listing
  curl -u "$NEXTCLOUD_USER:$NEXTCLOUD_APP_PASSWORD" \
    "$NEXTCLOUD_URL/remote.php/dav/files/$NEXTCLOUD_USER/"
  
  # OCS capabilities
  curl -u "$NEXTCLOUD_USER:$NEXTCLOUD_APP_PASSWORD" \
    "$NEXTCLOUD_URL/ocs/v1.php/cloud/capabilities" \
    -H "OCS-APIRequest: true"
  ```
- **Usefulness:** HIGH - File access, calendar, contacts, notes

### Pi-hole (DNS/Ad Blocking)
- **URL:** `http://10.3.x.x:80/admin` (internal network)
- **API Available:** Yes (v5) or session token (v6)
- **What's Needed:**
  1. Get API token: `/etc/pihole/setupVars.conf` contains `WEBPASSWORD` (hashed)
  2. For Pi-hole v6: Use session auth or create API token
  3. Add: `PIHOLE_URL`, `PIHOLE_API_TOKEN`
- **API Examples:**
  ```bash
  # Stats (no auth for some endpoints)
  curl "http://pihole/admin/api.php?summary"
  
  # Disable for 5 minutes
  curl "http://pihole/admin/api.php?disable=300&auth=$PIHOLE_API_TOKEN"
  ```
- **Usefulness:** MEDIUM - Check stats, enable/disable blocking, view queries

### Zigbee2mqtt (Zigbee Device Management)
- **URL:** `http://zigbee2mqtt:8080` (if frontend enabled)
- **API Available:** Yes - REST API + WebSocket
- **What's Needed:**
  1. Verify frontend is enabled in zigbee2mqtt config
  2. Check if auth is configured (optional)
  3. Add: `ZIGBEE2MQTT_URL`
- **API Examples:**
  ```bash
  # Get all devices
  curl "http://zigbee2mqtt:8080/api/devices"
  
  # Get bridge state
  curl "http://zigbee2mqtt:8080/api/bridge/state"
  ```
- **Note:** Already integrated via Home Assistant MQTT discovery, so direct API may be redundant
- **Usefulness:** LOW - Home Assistant already exposes Zigbee devices

### Snapserver (Multiroom Audio)
- **URL:** `http://10.6.32.2:1780` (JSON-RPC)
- **API Available:** Yes - JSON-RPC 2.0 over HTTP/WebSocket
- **What's Needed:**
  1. Add: `SNAPSERVER_URL=http://10.6.32.2:1780/jsonrpc`
  2. Create skill for playback control
- **API Examples:**
  ```bash
  # Get server status
  curl -X POST "$SNAPSERVER_URL" \
    -H "Content-Type: application/json" \
    -d '{"id":1,"jsonrpc":"2.0","method":"Server.GetStatus"}'
  
  # Get all clients
  curl -X POST "$SNAPSERVER_URL" \
    -H "Content-Type: application/json" \
    -d '{"id":1,"jsonrpc":"2.0","method":"Client.GetAll"}'
  
  # Set client volume (0-100)
  curl -X POST "$SNAPSERVER_URL" \
    -H "Content-Type: application/json" \
    -d '{"id":1,"jsonrpc":"2.0","method":"Client.SetVolume","params":{"id":"office","volume":{"percent":50}}}'
  ```
- **Usefulness:** HIGH - Control speakers, group management, volume

---

## 🚫 Skip (Not Useful for AI / Security Concerns)

### Bitwarden/Vaultwarden (Password Manager)
- **Reason:** Security risk - AI should NOT have password manager access
- **Alternative:** Use existing browser autofill, CLI for specific lookups if needed

### Audioserve (Audiobooks)
- **Reason:** Uses shared secret auth, limited API, primarily playback-focused
- **Alternative:** Could create skill if audiobook queries become common

### Jackett (Indexer Proxy)
- **Reason:** Deprecated in favor of Prowlarr (already integrated)

### nginx variants
- **Reason:** Pure routing/proxy, no useful API for AI

### WordPress/Blog
- **Reason:** CMS for blog, not useful for assistant tasks

### Watchtower (Auto-updates)
- **Reason:** Background service, no user-facing utility

### Docker Registry
- **Reason:** Infrastructure only

### Draw.io/Diagram
- **Reason:** UI-only tool, no API

---

## Priority Recommendations

### Immediate Value (Do First)
1. **Prowlarr Skill** - Already configured, just needs documentation
2. **Snapserver Skill** - Would enable "play music in kitchen" commands
3. **Nextcloud Setup** - File access would be very useful

### Medium Term
4. **Grafana Token** - Nice for "show me server stats" but InfluxDB covers data
5. **Pi-hole Token** - Occasional utility for DNS stats

### Low Priority
6. **Zigbee2mqtt** - Already exposed via Home Assistant

---

## Implementation Checklist

```markdown
- [ ] Create Prowlarr skill (env vars already in place)
- [ ] Create Snapserver skill + add SNAPSERVER_URL env var
- [ ] Generate Nextcloud app password + add env vars
- [ ] Create Nextcloud skill for WebDAV operations
- [ ] Generate Grafana service account token (optional)
- [ ] Get Pi-hole API token from setupVars.conf (optional)
```

---

## Missing Environment Variable: Brave Search API

**Note:** `web_search` tool requires `BRAVE_API_KEY` in gateway environment.
- Get key from: https://brave.com/search/api/
- Add to moltbot-gateway environment in docker-compose.yml
