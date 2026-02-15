# Secret Rotation Guide

This document lists all secrets used in the homelab infrastructure, how to rotate them, and what depends on each one.

## Overview

Secrets are managed through a **Vault-first** architecture with CI environment variable fallback:

1. **HashiCorp Vault** at `vault.lab.nkontur.com:8200` — canonical source of truth, organized under the `homelab/` KV v2 mount.
2. **GitLab CI/CD variables** — fallback when Vault is unreachable. These mirror Vault values.

### How Secrets Flow

```
┌─────────────┐     JWT auth      ┌───────────┐
│  GitLab CI  │ ────────────────► │   Vault   │
│  Pipeline   │ ◄──────────────── │  KV v2    │
│             │   secrets (JSON)  │           │
└──────┬──────┘                   └───────────┘
       │                                │
       │ Ansible: fetch-vault-secrets   │ AppRole auth
       │ role runs FIRST                │
       ▼                                ▼
┌─────────────┐                   ┌───────────┐
│  Ansible    │                   │  Moltbot  │
│  Variables  │                   │ (runtime) │
└──────┬──────┘                   └───────────┘
       │
       │ Jinja2 template with
       │ vault_var | default(lookup('env', 'CI_VAR'))
       ▼
┌─────────────┐
│ Templated   │
│ Compose +   │
│ Configs     │
└─────────────┘
```

**If Vault is down:** Every secret reference uses `| default(lookup('env', 'CI_VAR'))`, so CI environment variables serve as automatic fallback. Deploys never break due to Vault downtime.

**Vault UI:** `https://vault.lab.nkontur.com:8200/ui`
**GitLab CI Variables:** Settings → CI/CD → Variables (Project: root/homelab)

### Vault Authentication

| Method | Role | Policy | Used By |
|--------|------|--------|---------|
| JWT (GitLab OIDC) | `vault-admin` | `vault-admin` | `vault:configure`, `vault:rotate-approle` (main only) |
| JWT (GitLab OIDC) | `ci-deploy` | `ci-deploy` | `router:deploy`, `zwave:deploy`, `satellite-2:deploy`, `router:validate` (main/MR) |
| JWT (GitLab OIDC) | `vault-read` | `vault-read` | `vault:validate` (MR only — terraform plan) |
| AppRole | `openclaw` | `openclaw-ops` | OpenClaw container (scoped read-only) |
| AppRole | `jit-approval-svc` | `jit-approval-svc` | JIT approval service (credential broker) |

### Vault Secret Layout

```
homelab/
├── api-keys/        # External API keys (aclawdemy, anthropic, brave, openai)
├── backup/          # Backblaze, borg, restic credentials
├── cameras/         # Doorbell, rear camera passwords
├── docker/          # Per-service secrets (grafana, influxdb, jit-approval-svc, plex, etc.)
├── email/           # Gmail, SMTP, DKIM
├── gitlab/          # Runner tokens
├── infrastructure/  # Aruba, IPMI, LUKS, OMAPI, Pi-hole, router, SNMP, Tailscale, VRRP
├── openclaw/         # Gateway, GitLab, Telegram tokens
├── mqtt/            # Mosquitto credentials
└── networking/      # Cloudflare, Mullvad, Namesilo, Wireguard
```

### Ansible Vault Integration

The `fetch-vault-secrets` role runs as the **first role** in every playbook. It:

1. Authenticates to Vault via JWT (CI pipeline) or AppRole (openclaw)
2. Reads all secret paths from `homelab/` KV v2 mount
3. Sets each secret as an Ansible fact (e.g., `vault_pihole_password`)
4. If Vault is unreachable, secrets remain unset and the `| default(lookup('env', ...))` fallback activates

**Secret variable naming convention:** `vault_<lowercase_ci_var_name>`

Example in docker-compose.yml template:
```yaml
WEBPASSWORD: {{ vault_pihole_password | default(lookup('env', 'PIHOLE_PASSWORD')) }}
```

### Rotation Procedure (Vault-First)

To rotate a secret:

1. **Update in Vault** (UI or CLI) — this is the source of truth
2. **Update GitLab CI variable** — keeps fallback in sync
3. **Trigger deploy** — push to main or run pipeline manually
4. Vault value is used immediately; CI variable is backup

To remove CI variable fallback (after Vault is proven stable):
1. Remove the `| default(lookup('env', ...))` from templates
2. Delete the CI variable
3. Update this document

---

## Secret Inventory

| Secret | Vault Path | Field | Used By |
|--------|-----------|-------|---------|
| `PIHOLE_PASSWORD` | `infrastructure/pihole` | `password` | Pi-hole |
| `INFLUXDB_PASSWORD` | `docker/influxdb` | `password` | InfluxDB |
| `INFLUXDB_ADMIN_TOKEN` | `docker/influxdb` | `admin_token` | InfluxDB, Grafana |
| `INFLUXDB_TOKEN` | `docker/influxdb` | `token` | openclaw-gateway |
| `INFLUXDB_TELEGRAF_TOKEN` | `docker/influxdb` | `telegraf_token` | Telegraf |
| `GRAFANA_ADMIN_PASSWORD` | `docker/grafana` | `admin_password` | Grafana |
| `GRAFANA_SMTP_PASSWORD` | `docker/grafana` | `smtp_password` | Grafana |
| `WORDPRESS_DB_PASSWORD` | `docker/wordpress` | `db_password` | WordPress, MySQL |
| `NEXTCLOUD_DB_PASSWORD` | `docker/nextcloud` | `db_password` | Nextcloud, MariaDB |
| `DOORBELL_PASS` | `cameras/doorbell` | `password` | amcrest2mqtt |
| `MQTT_PASS` | `mqtt/mosquitto` | `password` | amcrest2mqtt, ambientweather |
| `AUDIOSERVE_SECRET` | `docker/audioserve` | `secret` | audioserve |
| `OPENAI_API_KEY` | `api-keys/openai` | `api_key` | openclaw-gateway |
| `ANTHROPIC_API_KEY` | `api-keys/anthropic` | `api_key` | openclaw (future) |
| `BRAVE_API_KEY` | `api-keys/brave` | `api_key` | openclaw-gateway |
| `ACLAWDEMY_API_KEY` | `api-keys/aclawdemy` | `api_key` | openclaw-gateway |
| `OPENCLAW_GATEWAY_TOKEN` | `openclaw/tokens` | `gateway_token` | openclaw-gateway |
| `OPENCLAW_TELEGRAM_TOKEN` | `openclaw/tokens` | `telegram_token` | openclaw-gateway |
| `OPENCLAW_GITLAB_TOKEN` | `openclaw/tokens` | `gitlab_token` | openclaw-gateway |
| `HASS_TOKEN` | `docker/homeassistant` | `token` | openclaw-gateway |
| `PLEX_TOKEN` | `docker/plex` | `token` | openclaw-gateway |
| `RADARR_API_KEY` | `docker/radarr` | `api_key` | openclaw-gateway |
| `SONARR_API_KEY` | `docker/sonarr` | `api_key` | openclaw-gateway |
| `PROWLARR_API_KEY` | `docker/prowlarr` | `api_key` | openclaw-gateway |
| `OMBI_API_KEY` | `docker/ombi` | `api_key` | openclaw-gateway |
| `NZBGET_USERNAME` | `docker/nzbget` | `username` | openclaw-gateway |
| `NZBGET_PASSWORD` | `docker/nzbget` | `password` | openclaw-gateway |
| `DELUGE_PASSWORD` | `docker/deluge` | `password` | openclaw-gateway |
| `PAPERLESS_TOKEN` | `docker/paperless` | `token` | openclaw-gateway |
| `TAILSCALE_API_TOKEN` | `infrastructure/tailscale` | `api_token` | openclaw-gateway |
| `TAILSCALE_AUTH_KEY` | `infrastructure/tailscale` | `auth_key` | configure-tailscale role |
| `IPMI_USER` | `infrastructure/ipmi` | `user` | openclaw-gateway |
| `IPMI_PASSWORD` | `infrastructure/ipmi` | `password` | openclaw-gateway |
| `GMAIL_EMAIL` | `email/gmail` | `email` | openclaw-gateway |
| `GMAIL_APP_PASSWORD` | `email/gmail` | `app_password` | openclaw-gateway |
| `ROUTER_PRIVATE_KEY_BASE64` | `infrastructure/router` | `private_key_base64` | GitLab CI pipeline |
| `SNMP_PASSWORD` | `infrastructure/snmp` | `password` | Telegraf |
| `LUKS_PASSWORD_BASE64` | `infrastructure/luks` | `password_base64` | configure-base role |
| `OMAPI_SECRET` | `infrastructure/omapi` | `secret` | DHCP OMAPI |
| `CLOUDFLARE_API_KEY` | `networking/cloudflare` | `api_key` | DDNS cron |
| `CLOUDFLARE_ZONE_ID` | `networking/cloudflare` | `zone_id` | DDNS cron |
| `NAMESILO_API_KEY` | `networking/namesilo` | `api_key` | SSL renewal cron |
| `GRAFANA_TOKEN` | `docker/grafana` | `token` | openclaw-gateway |
| `SPOTIFY_DC` | `infrastructure/spotify` | `sp_dc` | Home Assistant spotcast |
| `SPOTIFY_KEY` | `infrastructure/spotify` | `sp_key` | Home Assistant spotcast |
| `JIT_APPROLE_ROLE_ID` | `docker/jit-approval-svc` | `approle_role_id` | jit-approval-svc |
| `JIT_APPROLE_SECRET_ID` | `docker/jit-approval-svc` | `approle_secret_id` | jit-approval-svc |
| `JIT_TELEGRAM_BOT_TOKEN` | `docker/jit-approval-svc` | `telegram_bot_token` | jit-approval-svc |
| `JIT_TELEGRAM_WEBHOOK_SECRET` | `docker/jit-approval-svc` | `telegram_webhook_secret` | jit-approval-svc |
| `BACKBLAZE_ACCESS_KEY_ID` | `backup/backblaze` | `access_key_id` | Restic backups |
| `BACKBLAZE_SECRET_ACCESS_KEY` | `backup/backblaze` | `secret_access_key` | Restic backups |
| `RESTIC_PASSWORD` | `backup/restic` | `password` | Restic backups |

### Secrets NOT in Vault (CI-only)

| Secret | Reason |
|--------|--------|
| `VAULT_APPROLE_ROLE_ID` | Circular dependency — needed to auth to Vault |
| `VAULT_APPROLE_SECRET_ID` | Circular dependency — needed to auth to Vault |
| `VAULT_UNSEAL_KEYS` | Break-glass secret — must not depend on Vault |
| `ROUTER_PRIVATE_KEY_BASE64` | CI `before_script` fallback — Vault JWT fetch attempted first |
| `SMTP_PASSWORD` | Appears unused — candidate for deletion |

---

## Detailed Rotation Procedures

### PIHOLE_PASSWORD

**Description:** Web admin password for Pi-hole DNS management interface.

**Used by:**
- `pihole` container (environment variable `WEBPASSWORD`)

**How to rotate:**
1. Update in Vault: `homelab/infrastructure/pihole` → `password`
2. Update GitLab CI variable `PIHOLE_PASSWORD` (fallback)
3. Trigger a deployment (push to main or manually run pipeline)
4. Pi-hole picks up the new password on container restart

**Complexity:** 🟢 Easy

---

### INFLUXDB_PASSWORD

**Description:** Admin password for InfluxDB initial setup.

**Used by:**
- `influxdb` container (environment variable `DOCKER_INFLUXDB_INIT_PASSWORD`)

**How to rotate:**
1. This password is only used during initial InfluxDB setup
2. After first run, InfluxDB stores credentials internally
3. To change: use InfluxDB admin UI or CLI to update the user password
4. Update in Vault: `homelab/docker/influxdb` → `password`
5. Update GitLab CI variable for fallback

**Complexity:** 🟡 Medium

---

### INFLUXDB_ADMIN_TOKEN

**Description:** API token for InfluxDB access. Used for both InfluxDB init and Grafana data source.

**Used by:**
- `influxdb` container (`DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`)
- `grafana` container (`INFLUXDB_TOKEN`)

**How to rotate:**
1. Generate new token in InfluxDB UI (Data → API Tokens)
2. Update in Vault: `homelab/docker/influxdb` → `admin_token`
3. Update GitLab CI variable `INFLUXDB_ADMIN_TOKEN`
4. Redeploy both `influxdb` and `grafana` containers
5. Verify Grafana dashboards still load data

**Complexity:** 🔴 Hard (multi-service dependency)

---

### WORDPRESS_DB_PASSWORD

**Description:** MySQL database password for WordPress.

**Used by:**
- `blog` (WordPress) container (`WORDPRESS_DB_PASSWORD`)
- `wordpress_db` (MySQL) container (`MYSQL_PASSWORD`, `MYSQL_ROOT_PASSWORD`)

**How to rotate:**
1. Stop WordPress container
2. Connect to MySQL and change the password:
   ```sql
   ALTER USER 'wordpress'@'%' IDENTIFIED BY 'newpassword';
   ALTER USER 'root'@'%' IDENTIFIED BY 'newpassword';
   FLUSH PRIVILEGES;
   ```
3. Update in Vault: `homelab/docker/wordpress` → `db_password`
4. Update GitLab CI variable `WORDPRESS_DB_PASSWORD`
5. Redeploy both containers

**Complexity:** 🔴 Hard (requires MySQL command execution + coordinated update)

---

### DOORBELL_PASS

**Description:** Password for Amcrest doorbell camera.

**Used by:**
- `amcrest2mqtt` container (`AMCREST_PASSWORD`)

**How to rotate:**
1. Log into Amcrest camera web UI (10.6.128.9)
2. Change the admin password
3. Update in Vault: `homelab/cameras/doorbell` → `password`
4. Update GitLab CI variable `DOORBELL_PASS`
5. Redeploy amcrest2mqtt container

**Complexity:** 🟡 Medium (requires camera web UI access)

---

### MQTT_PASS

**Description:** Password for Mosquitto MQTT broker authentication.

**Used by:**
- `amcrest2mqtt` container (`MQTT_PASSWORD`)
- `ambientweather` container (`MQTT_PASSWORD`)
- Mosquitto config (stored in `{{ docker_persistent_data_path }}/mqtt/conf/`)
- Home Assistant (configured in HA's configuration)
- Zigbee2MQTT (configured in its data directory)

**How to rotate:**
1. Update Mosquitto password file:
   ```bash
   docker exec mosquitto mosquitto_passwd -b /mosquitto/config/passwd mosquitto newpassword
   ```
2. Update in Vault: `homelab/mqtt/mosquitto` → `password`
3. Update GitLab CI variable `MQTT_PASS`
4. Redeploy: amcrest2mqtt, ambientweather
5. Update Home Assistant MQTT integration configuration
6. Update Zigbee2MQTT configuration
7. Restart all affected services

**Complexity:** 🔴 Hard (affects many services, some with external configs)

---

### NEXTCLOUD_DB_PASSWORD

**Description:** MariaDB password for Nextcloud.

**Used by:**
- `nextcloud` container (via internal config)
- `nextcloud_db` (MariaDB) container (`MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`)

**How to rotate:**
1. Stop Nextcloud container
2. Connect to MariaDB and change password
3. Update Nextcloud's `config/config.php` with new password
4. Update in Vault: `homelab/docker/nextcloud` → `db_password`
5. Update GitLab CI variable `NEXTCLOUD_DB_PASSWORD`
6. Redeploy both containers

**Complexity:** 🔴 Hard (requires DB commands + Nextcloud config update)

---

### OPENCLAW_GATEWAY_TOKEN / OPENCLAW_TELEGRAM_TOKEN / OPENCLAW_GITLAB_TOKEN

**Description:** Tokens for Moltbot gateway API, Telegram Bot API, and GitLab access.

**Vault path:** `homelab/openclaw/tokens`

**Note:** `OPENCLAW_GITLAB_TOKEN` is also used by the `vault:rotate-approle` CI job. That job now fetches it from Vault via JWT auth, falling back to the CI variable.

**How to rotate:**
1. Generate new token (varies by service)
2. Update in Vault: `homelab/openclaw/tokens` → relevant field
3. Update GitLab CI variable (fallback)
4. Redeploy openclaw-gateway container

**Complexity:** 🟢-🟡 Easy to Medium

---

### ROUTER_PRIVATE_KEY_BASE64

**Description:** Base64-encoded SSH private key for Ansible deployments.

**Vault path:** `infrastructure/router` → `private_key_base64`

**Note:** The CI `.ansible` template's `before_script` now fetches this key from Vault via JWT auth (role `ci-deploy`) before Ansible runs. The CI variable `ROUTER_PRIVATE_KEY_BASE64` is kept as a fallback if Vault is unreachable.

**Used by:**
- GitLab CI pipeline (all ansible playbook runs)

**How to rotate:**
1. Generate new SSH keypair
2. Add public key to authorized_keys on all deploy targets
3. Base64 encode the private key
4. Update in Vault: `homelab/infrastructure/router` → `private_key_base64`
5. Update `ROUTER_PRIVATE_KEY_BASE64` in GitLab CI/CD variables (fallback)
6. Test deployment with a dummy commit
7. Remove old public key from all hosts

**Complexity:** 🔴 Hard (requires access to multiple hosts)

---

## Secrets NOT in GitLab CI

These secrets are stored elsewhere and managed differently:

### Bitwarden Configuration

**Location:** `{{ docker_persistent_data_path }}/bitwarden/global.override.env`

This file contains Bitwarden-specific secrets. Manage directly on the router host.

### Zigbee2MQTT

**Location:** `{{ docker_persistent_data_path }}/zigbee2mqtt/configuration.yaml`

Contains MQTT credentials. Update when rotating MQTT_PASS.

---

## Rotation Schedule

| Frequency | Secrets |
|-----------|---------|
| **When Compromised** | OPENAI_API_KEY, OPENCLAW_GATEWAY_TOKEN, OPENCLAW_TELEGRAM_TOKEN, DOORBELL_PASS |
| **Monthly** | VAULT_APPROLE_SECRET_ID (via `vault:rotate-approle` CI job) |
| **Annually** | All others |
| **After Employee Offboarding** | All secrets they had access to |

---

## Pre-Rotation Checklist

Before rotating any secret:

1. [ ] Identify all services using the secret (check this document)
2. [ ] Plan for brief downtime if multi-service secret
3. [ ] Have rollback plan (keep old secret value temporarily)
4. [ ] Test in off-hours if possible
5. [ ] Verify services after rotation
6. [ ] Update BOTH Vault and GitLab CI variable

---

## Emergency Rotation

If a secret is compromised:

1. **Immediately** update the secret in Vault (UI or CLI)
2. **Immediately** update the GitLab CI/CD variable (fallback)
3. **Immediately** redeploy affected containers
4. For ROUTER_PRIVATE_KEY_BASE64: also remove old public key from hosts
5. Check logs for unauthorized access during exposure window
6. Document the incident

---

## Vault AppRole Rotation (Moltbot)

Moltbot authenticates to Vault using AppRole (`role_id` + `secret_id`).
The `secret_id` should be rotated periodically.

**CI Variables:**
- `VAULT_APPROLE_ROLE_ID` — static, rarely changes
- `VAULT_APPROLE_SECRET_ID` — rotate monthly

**Automated rotation:**

A manual CI job `vault:rotate-approle` is available on main branch pipelines.
It can also be triggered via pipeline schedule for automatic monthly rotation.

The job:
1. Authenticates to Vault via JWT (`vault-admin` role)
2. Generates a new `secret_id` for the openclaw AppRole
3. Verifies the new credentials work (login test)
4. Updates the `VAULT_APPROLE_SECRET_ID` CI variable
5. Next openclaw deploy picks up the new secret automatically

**To trigger manually:**
1. Go to CI/CD → Pipelines → Run pipeline (on main)
2. Find `vault:rotate-approle` in the configure stage
3. Click the play button

**To schedule:**
1. Go to CI/CD → Schedules → New schedule
2. Set cron: `0 4 1 * *` (4 AM on the 1st of each month)
3. Target branch: main

---

## Vault AppRole Rotation (JIT Approval Service)

The JIT approval service authenticates to Vault using AppRole (`role_id` + `secret_id`).
It brokers credential access for the agent by minting short-lived child tokens with
scoped policies (`jit-tier1-services`, `jit-tier2-infra`).

**Policies attached:**
- `jit-approval-svc` — service's own policy (token management + read brokered secrets)
- Child tokens minted with tier-specific policies only

**Rotation:** Same procedure as openclaw AppRole — generate new `secret_id`, update the
service's environment, restart.

---

## Transcript Hygiene

OpenClaw session transcripts (`.jsonl` files) may contain credentials that were pasted or echoed during interactive sessions. A cron job runs every 6 hours on the router to scrub these automatically.

### What it does

1. **Credential redaction** — Scans all transcript files under `{{ docker_persistent_data_path }}/openclaw-gateway/agents/main/sessions/` and replaces known secret patterns with `[REDACTED]` in-place.
2. **Archive purge** — Deletes archived sub-agent transcripts (`*.deleted.*` files) older than 7 days to reclaim disk space.

### Patterns redacted

| Pattern | Example |
|---------|---------|
| Vault tokens | `hvs.CAESILx...` |
| GitLab tokens | `glpat-xxxx...` |
| GitHub tokens | `ghp_`, `gho_`, `ghs_` |
| OpenAI keys | `sk-xxxx...` (20+ chars) |
| Anthropic keys | `sk-ant-xxxx...` |
| Slack tokens | `xoxb-`, `xoxp-` |
| AWS access keys | `AKIA...` |
| Telegram bot tokens | `123456:AAxxx...` |
| JWT tokens | `eyJ...eyJ...xxx` |
| Private key blocks | `-----BEGIN...PRIVATE KEY-----` |
| UUID secrets | After `secret_id`, `secret`, `password` keywords |
| Long hex strings | After `token`, `password`, `secret`, `key` keywords |
| App passwords | Near `app.password` or `GMAIL_APP_PASSWORD` |

### Configuration

- **Script:** `/usr/local/bin/scrub-transcripts.sh` (deployed by Ansible)
- **Source:** `docker/openclaw/scripts/scrub-transcripts.sh` in this repo
- **Schedule:** Every 6 hours at :30 (cron)
- **Logging:** Output goes to syslog via `logger -t scrub-transcripts`
- **Ansible task:** In `configure-docker` role

### Manual run

```bash
/usr/local/bin/scrub-transcripts.sh /persistent_data/application/openclaw-gateway/agents/main/sessions/
```

### Monitoring

Check recent scrub activity:
```bash
journalctl -t scrub-transcripts --since "24 hours ago"
```

---

## Future Improvements

- [ ] Remove CI env var fallbacks once Vault proves stable (~1 month)
- [x] Move CLOUDFLARE_API_KEY to Vault
- [ ] Implement secret scanning in CI pipeline
- [ ] Set up pipeline schedule for automatic AppRole rotation
- [ ] Add Vault token expiry monitoring to Grafana
- [x] Add GRAFANA_TOKEN to Vault
- [ ] Delete unused SMTP_PASSWORD CI variable

---

## SSH CA (Vault SSH Secrets Engine)

**Path:** `ssh-client-signer`
**Type:** Vault SSH secrets engine (not KV)

The SSH CA key is auto-generated by Vault (`generate_signing_key = true`).
Rotation requires regenerating the CA and re-deploying the public key to all hosts.

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CA keypair | `ssh-client-signer/config/ca` | Vault-managed signing key |
| CA public key | `/etc/ssh/trusted-user-ca-keys.pem` on hosts | SSH trust anchor |
| Signing role | `ssh-client-signer/sign/claude` | Issues 15-min certificates |

### Rotation procedure

1. **Warning:** Rotating the CA invalidates ALL existing certificates
2. Delete and recreate the SSH mount in Terraform (or use `vault write ssh-client-signer/config/ca generate_signing_key=true`)
3. Run Ansible on all hosts to deploy the new public key:
   ```bash
   ansible-playbook ansible/router.yml --tags claude-user
   ansible-playbook ansible/satellite-2.yml --tags claude-user
   ```
4. Verify: `ssh -i /tmp/test-key claude@router.lab.nkontur.com` with a freshly signed cert
