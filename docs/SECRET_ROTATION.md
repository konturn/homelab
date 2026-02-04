# Secret Rotation Guide

This document lists all secrets used in the homelab infrastructure, how to rotate them, and what depends on each one.

## Overview

Secrets are stored as **GitLab CI/CD variables** and injected into `docker-compose.yml` via Ansible's `lookup('env', ...)` function during deployment. The deployment pipeline reads these variables and templates them into the final configuration.

**GitLab CI Variables Location:**  
Settings → CI/CD → Variables (Project: root/homelab)

---

## Secret Inventory

| Secret | Used By | Rotation Complexity | Recommended Schedule |
|--------|---------|---------------------|---------------------|
| `PIHOLE_PASSWORD` | Pi-hole | 🟢 Easy | Annually |
| `INFLUXDB_PASSWORD` | InfluxDB | 🟡 Medium | Annually |
| `INFLUXDB_ADMIN_TOKEN` | InfluxDB, Grafana | 🔴 Hard | Annually |
| `GRAFANA_ADMIN_PASSWORD` | Grafana | 🟢 Easy | Annually |
| `WORDPRESS_DB_PASSWORD` | WordPress, MySQL | 🔴 Hard | Annually |
| `DOORBELL_PASS` | amcrest2mqtt | 🟡 Medium | When compromised |
| `MQTT_PASS` | mosquitto, amcrest2mqtt, ambientweather | 🔴 Hard | Annually |
| `NEXTCLOUD_DB_PASSWORD` | Nextcloud, MariaDB | 🔴 Hard | Annually |
| `OPENAI_API_KEY` | moltbot-gateway | 🟢 Easy | When compromised |
| `MOLTBOT_GATEWAY_TOKEN` | moltbot-gateway | 🟢 Easy | When compromised |
| `MOLTBOT_TELEGRAM_TOKEN` | moltbot-gateway | 🟡 Medium | When compromised |
| `HASS_TOKEN` | moltbot-gateway | 🟢 Easy | Annually |
| `MOLTBOT_GITLAB_TOKEN` | moltbot-gateway | 🟢 Easy | Annually |
| `ROUTER_PRIVATE_KEY_BASE64` | GitLab CI | 🔴 Hard | Annually |

---

## Detailed Rotation Procedures

### PIHOLE_PASSWORD

**Description:** Web admin password for Pi-hole DNS management interface.

**Used by:**
- `pihole` container (environment variable `WEBPASSWORD`)

**How to rotate:**
1. Update the variable in GitLab CI/CD settings
2. Trigger a deployment (push to main or manually run pipeline)
3. Pi-hole will pick up the new password on container restart

**What breaks if not updated everywhere:** Nothing — only used in one place.

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
4. Update GitLab variable for future fresh deployments

**What breaks if not updated everywhere:** None for running instances (password already in InfluxDB). Fresh deployments would use new password.

**Complexity:** 🟡 Medium

---

### INFLUXDB_ADMIN_TOKEN

**Description:** API token for InfluxDB access. Used for both InfluxDB init and Grafana data source.

**Used by:**
- `influxdb` container (`DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`)
- `grafana` container (`INFLUXDB_TOKEN`)

**How to rotate:**
1. Generate new token in InfluxDB UI (Data → API Tokens)
2. Update `INFLUXDB_ADMIN_TOKEN` in GitLab CI/CD variables
3. Redeploy both `influxdb` and `grafana` containers
4. Verify Grafana dashboards still load data

**What breaks if not updated everywhere:**
- If only InfluxDB updated: Grafana loses access to metrics
- Dashboards show "No data" errors

**Complexity:** 🔴 Hard (multi-service dependency)

---

### GRAFANA_ADMIN_PASSWORD

**Description:** Admin password for Grafana web UI.

**Used by:**
- `grafana` container (`GF_SECURITY_ADMIN_PASSWORD`)

**How to rotate:**
1. Update the variable in GitLab CI/CD settings
2. Trigger deployment
3. Note: Grafana may not pick up new password if admin user already exists in database
4. To force: use Grafana CLI or reset via API

**What breaks if not updated everywhere:** Nothing — only used in one place.

**Complexity:** 🟢 Easy (but may need manual Grafana CLI intervention)

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
3. Update `WORDPRESS_DB_PASSWORD` in GitLab CI/CD variables
4. Redeploy both containers

**What breaks if not updated everywhere:**
- WordPress cannot connect to database
- Site shows "Error establishing database connection"

**Complexity:** 🔴 Hard (requires MySQL command execution + coordinated update)

---

### DOORBELL_PASS

**Description:** Password for Amcrest doorbell camera.

**Used by:**
- `amcrest2mqtt` container (`AMCREST_PASSWORD`)

**How to rotate:**
1. Log into Amcrest camera web UI (10.6.128.9)
2. Change the admin password
3. Update `DOORBELL_PASS` in GitLab CI/CD variables
4. Redeploy amcrest2mqtt container

**What breaks if not updated everywhere:**
- amcrest2mqtt fails to connect to camera
- Doorbell events stop appearing in Home Assistant

**Complexity:** 🟡 Medium (requires camera web UI access)

---

### MQTT_PASS

**Description:** Password for Mosquitto MQTT broker authentication.

**Used by:**
- `amcrest2mqtt` container (`MQTT_PASSWORD`)
- `ambientweather` container (`MQTT_PASSWORD`)
- Mosquitto config (stored in `{{ docker_persistent_data_path }}/mqtt/conf/`)
- Home Assistant (likely configured in HA's configuration)
- Zigbee2MQTT (configured in its data directory)

**How to rotate:**
1. Update Mosquitto password file:
   ```bash
   docker exec mosquitto mosquitto_passwd -b /mosquitto/config/passwd mosquitto newpassword
   ```
2. Update `MQTT_PASS` in GitLab CI/CD variables
3. Redeploy: amcrest2mqtt, ambientweather
4. Update Home Assistant MQTT integration configuration
5. Update Zigbee2MQTT configuration
6. Restart all affected services

**What breaks if not updated everywhere:**
- amcrest2mqtt: Doorbell events stop
- ambientweather: Weather station data stops
- Home Assistant: All MQTT devices go unavailable
- Zigbee2MQTT: Zigbee devices go unavailable

**Complexity:** 🔴 Hard (affects many services, some with external configs)

---

### NEXTCLOUD_DB_PASSWORD

**Description:** MariaDB password for Nextcloud.

**Used by:**
- `nextcloud` container (via internal config)
- `nextcloud_db` (MariaDB) container (`MYSQL_ROOT_PASSWORD`, `MYSQL_PASSWORD`)

**How to rotate:**
1. Stop Nextcloud container
2. Connect to MariaDB and change password:
   ```sql
   ALTER USER 'nextcloud'@'%' IDENTIFIED BY 'newpassword';
   ALTER USER 'root'@'%' IDENTIFIED BY 'newpassword';
   FLUSH PRIVILEGES;
   ```
3. Update Nextcloud's `config/config.php` with new password
4. Update `NEXTCLOUD_DB_PASSWORD` in GitLab CI/CD variables
5. Redeploy both containers

**What breaks if not updated everywhere:**
- Nextcloud shows database connection error
- File sync stops for all users

**Complexity:** 🔴 Hard (requires DB commands + Nextcloud config update)

---

### OPENAI_API_KEY

**Description:** API key for OpenAI services (used by Moltbot).

**Used by:**
- `moltbot-gateway` container

**How to rotate:**
1. Generate new key at https://platform.openai.com/api-keys
2. Update `OPENAI_API_KEY` in GitLab CI/CD variables
3. Redeploy moltbot-gateway container
4. Optionally revoke old key in OpenAI dashboard

**What breaks if not updated everywhere:** Nothing — only used in one place.

**Complexity:** 🟢 Easy

---

### MOLTBOT_GATEWAY_TOKEN

**Description:** Authentication token for Moltbot gateway API.

**Used by:**
- `moltbot-gateway` container (`CLAWDBOT_GATEWAY_TOKEN`)
- Any external clients connecting to the gateway

**How to rotate:**
1. Generate new token (any secure random string)
2. Update `MOLTBOT_GATEWAY_TOKEN` in GitLab CI/CD variables
3. Update any external clients using this token
4. Redeploy moltbot-gateway container

**What breaks if not updated everywhere:**
- External clients lose gateway access

**Complexity:** 🟢 Easy (unless external clients exist)

---

### MOLTBOT_TELEGRAM_TOKEN

**Description:** Telegram Bot API token.

**Used by:**
- `moltbot-gateway` container

**How to rotate:**
1. Talk to @BotFather on Telegram
2. Use `/revoke` to invalidate old token
3. Use `/token` to generate new token
4. Update `MOLTBOT_TELEGRAM_TOKEN` in GitLab CI/CD variables
5. Redeploy moltbot-gateway container

**What breaks if not updated everywhere:** Nothing — only used in one place. Old token stops working immediately when revoked.

**Complexity:** 🟡 Medium (requires Telegram BotFather interaction)

---

### HASS_TOKEN

**Description:** Home Assistant long-lived access token.

**Used by:**
- `moltbot-gateway` container

**How to rotate:**
1. Log into Home Assistant
2. Go to Profile → Long-Lived Access Tokens
3. Create new token, copy it
4. Update `HASS_TOKEN` in GitLab CI/CD variables
5. Redeploy moltbot-gateway container
6. Delete old token in Home Assistant

**What breaks if not updated everywhere:** Nothing — only used in one place.

**Complexity:** 🟢 Easy

---

### MOLTBOT_GITLAB_TOKEN

**Description:** GitLab personal access token for Moltbot to create MRs.

**Used by:**
- `moltbot-gateway` container

**How to rotate:**
1. Go to GitLab → User Settings → Access Tokens
2. Create new token with required scopes (api, read_repository, write_repository)
3. Update `MOLTBOT_GITLAB_TOKEN` in GitLab CI/CD variables
4. Redeploy moltbot-gateway container
5. Revoke old token in GitLab

**What breaks if not updated everywhere:** Nothing — only used in one place.

**Complexity:** 🟢 Easy

---

### ROUTER_PRIVATE_KEY_BASE64

**Description:** Base64-encoded SSH private key for Ansible deployments.

**Used by:**
- GitLab CI pipeline (all ansible playbook runs)

**How to rotate:**
1. Generate new SSH keypair:
   ```bash
   ssh-keygen -t ed25519 -f new_deploy_key -N ""
   ```
2. Add public key to authorized_keys on:
   - router.lab.nkontur.com
   - zwave.lab.nkontur.com
   - satellite-2.lab.nkontur.com
3. Base64 encode the private key:
   ```bash
   base64 -w0 new_deploy_key > new_deploy_key.b64
   ```
4. Update `ROUTER_PRIVATE_KEY_BASE64` in GitLab CI/CD variables
5. Test deployment with a dummy commit
6. Remove old public key from all hosts

**What breaks if not updated everywhere:**
- All CI/CD deployments fail
- Changes cannot be automatically deployed

**Complexity:** 🔴 Hard (requires access to multiple hosts)

---

## Secrets NOT in GitLab CI

These secrets are stored elsewhere and managed differently:

### Registry htpasswd

**Location:** `{{ docker_persistent_data_path }}/registry/auth/htpasswd`

**How to rotate:**
```bash
docker run --rm -it httpd:2.4 htpasswd -Bn username > htpasswd
# Copy to router and restart registry container
```

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
| **When Compromised** | OPENAI_API_KEY, MOLTBOT_GATEWAY_TOKEN, MOLTBOT_TELEGRAM_TOKEN, DOORBELL_PASS |
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

---

## Emergency Rotation

If a secret is compromised:

1. **Immediately** update the GitLab CI/CD variable
2. **Immediately** redeploy affected containers
3. For ROUTER_PRIVATE_KEY_BASE64: also remove old public key from hosts
4. Check logs for unauthorized access during exposure window
5. Document the incident

---

## Future Improvements

- [ ] Consider HashiCorp Vault for centralized secret management
- [ ] Implement secret scanning in CI pipeline
- [ ] Set up automated rotation for API keys where supported
- [ ] Add expiration alerts for tokens with TTL
