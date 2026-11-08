# Homelab Infrastructure

Infrastructure-as-Code for home lab environment using Ansible and Docker Compose.

## Quick Start

```bash
# Deploy to router (main host)
ansible-playbook -i ansible/inventory.yml ansible/router.yml

# Deploy to satellites (Raspberry Pis) and the Mac mini
ansible-playbook -i ansible/inventory.yml ansible/zwave.yml
ansible-playbook -i ansible/inventory.yml ansible/satellite-2.yml
ansible-playbook -i ansible/inventory.yml ansible/macmini.yml
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      GitLab CI/CD                           │
│  Push → Validate (MR) or Deploy (main) → Ansible → Router   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Router (Main Host)                       │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │   Docker    │  │   Ansible   │  │  Networking │         │
│  │  Compose    │  │    Roles    │  │   (VLANs)   │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
   ┌─────────┐          ┌─────────┐          ┌─────────┐
   │ VLAN 2  │          │ VLAN 3  │          │ VLAN 6  │
   │External │          │Internal │          │   IoT   │
   │10.2.x.x │          │10.3.x.x │          │10.6.x.x │
   └─────────┘          └─────────┘          └─────────┘
```

## Networks (VLANs)

| VLAN | Subnet     | Purpose                                      |
|------|------------|----------------------------------------------|
| 2    | 10.2.x.x   | External - Internet-facing services          |
| 3    | 10.3.x.x   | Internal - Lab services, databases           |
| 4    | 10.4.x.x   | Management - switches, IPMI                  |
| 5    | 10.5.x.x   | Guest network                                |
| 6    | 10.6.x.x   | IoT - Home Assistant, Zigbee, Snapcast       |

> 📖 **See [docs/NETWORK.md](docs/NETWORK.md)** for detailed network documentation including traffic flows, Tailscale remote access, and Mullvad egress routing.

> 📊 **See [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md)** for the full observability stack — logs (Loki), metrics (Telegraf/InfluxDB), traces (OpenTelemetry/Tempo), and alerting (Grafana).

> 🔌 **See [docs/POWER.md](docs/POWER.md)** for the UPS, what happens in a power outage (shutdown and restart), battery health and the supervised UPS test plan.

## Services

### External (10.2.x.x)
- **nginx** - Reverse proxy
- **Nextcloud** - File sync
- **Bitwarden** - Password manager
- **Plex** - Media server
- **Audioserve** - Audiobook streaming

### Internal (10.3.x.x)
- **GitLab** - Source control, CI/CD
- **Moltbot** - AI assistant gateway
- **Radarr/Sonarr** - Media automation
- **PiHole** - DNS ad-blocking
- **Paperless-ngx** - Document management

### IoT (10.6.x.x)
- **Home Assistant** - Home automation
- **Zigbee2MQTT** - Zigbee device bridge
- **Mosquitto** - MQTT broker
- **Snapcast** - Multi-room audio, with an AirPlay receiver per zone (docs/AUDIO.md)

## Directory Structure

```
.
├── ansible/
│   ├── inventory.yml      # Host variables and config mappings
│   ├── router.yml         # Main host playbook
│   ├── zwave.yml          # Z-Wave Pi playbook
│   ├── satellite-2.yml    # Satellite Pi playbook
│   ├── macmini.yml        # Mac mini playbook
│   └── roles/             # Ansible roles
├── docker/
│   ├── docker-compose.yml # Main compose file (Jinja2 templated)
│   └── <service>/         # Service-specific configs
├── networking/
│   ├── iptables/          # Firewall rules
│   └── dhcp/              # DHCP server configs
└── base/                  # System-level configs
```

## CI/CD Pipeline

- **MR Pipeline**: Validates the playbooks. MR pipelines do not get the
  protected deploy key, so they run `--syntax-check`; `--check --diff` runs
  only when the key is available.
- **Main Pipeline**: Deploys to every host, limited to the roles whose inputs
  changed (see [docs/DEPLOY.md](docs/DEPLOY.md)); a nightly schedule runs a
  full deploy.

Pipeline caches pip and ansible-galaxy dependencies for faster subsequent runs.

## Adding a New Service

1. Add service to `docker/docker-compose.yml`
2. Add any config files to `docker/<service>/`
3. Update `ansible/inventory.yml` with config file mappings:
   ```yaml
   docker_config:
     - src: "docker/myservice/config.yml"
       dest: "{{ docker_persistent_data_path }}/myservice/config.yml"
       name: "myservice"
   ```
4. Push to trigger deployment

## Secrets

- Never commit cleartext secrets
- Secrets live in Vault. The `fetch-vault-secrets` role reads every path listed
  in `ansible/roles/fetch-vault-secrets/defaults/main.yml` into Ansible
  variables at deploy time (CI authenticates with a JWT ID token), so templates
  use `{{ variable_name }}`

## Maintenance

```bash
# Image updates: Renovate opens MRs (hourly schedule, docs/RENOVATE.md); the
# auto-merge sweep merges eligible ones (docs/AUTOMERGE.md)
# Manual: docker compose pull && docker compose up -d

# Check service status
docker ps

# View logs
docker logs <container_name>
```

## Contributing

This is a personal homelab, but the patterns here may be useful for others.
Feel free to fork and adapt for your own infrastructure.
