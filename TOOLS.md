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

**Note:** Token has git clone/push access but NOT API read scope. Need `api` scope added to use `glab` CLI for MR creation.

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

**Router host:** router.lab.nkontur.com  
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

## Home Assistant MCP

I have access to Home Assistant via MCP (Model Context Protocol). The mcporter config connects me to:
- `HASS_URL` and `HASS_TOKEN` from environment

Can control lights, switches, media, sensors, automations, etc.

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

## Noah's Laptop (noah-XPS-13-7390-2-in-1)

### Email
- **himalaya** — CLI email client, configured and ready to use
  - `himalaya list` — list emails
  - `himalaya read <id>` — read specific email
  - `himalaya search <query>` — search emails

## Noah's Laptop (noah-XPS-13-7390-2-in-1)

### Email
- **himalaya** — CLI email client, configured and ready to use
  - `himalaya list` — list emails
  - `himalaya read <id>` — read specific email
  - `himalaya search <query>` — search emails

---

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

Add whatever helps you do your job. This is your cheat sheet.
