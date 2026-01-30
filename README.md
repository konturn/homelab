# Homelab Infrastructure

Infrastructure-as-Code for home lab environment using Ansible and Docker Compose.

## Quick Start

```bash
# Deploy to router (main host)
ansible-playbook -i ansible/inventory.yml ansible/router.yml

# Deploy to satellites (Raspberry Pis)
ansible-playbook -i ansible/inventory.yml ansible/zwave.yml
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
| 4    | 10.4.x.x   | Management - Registry, switches, IPMI        |
| 5    | 10.5.x.x   | Guest network                                |
| 6    | 10.6.x.x   | IoT - Home Assistant, Zigbee, Snapcast       |

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
- **Snapcast** - Multi-room audio

## Directory Structure

```
.
├── ansible/
│   ├── inventory.yml      # Host variables and config mappings
│   ├── router.yml         # Main host playbook
│   ├── zwave.yml          # Satellite playbook
│   └── roles/             # Ansible roles
├── docker/
│   ├── docker-compose.yml # Main compose file (Jinja2 templated)
│   └── <service>/         # Service-specific configs
├── networking/
│   ├── wireguard/         # VPN configs
│   ├── iptables/          # Firewall rules
│   └── dhcp/              # DHCP server configs
└── base/                  # System-level configs
```

## CI/CD Pipeline

- **MR Pipeline**: Runs `--check --diff` (dry-run validation)
- **Main Pipeline**: Actual deployment to infrastructure

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
- Use Jinja2 environment lookups: `{{ lookup('env', 'SECRET_NAME') }}`
- GitLab CI provides secrets as masked variables

## Maintenance

```bash
# Update all containers (via Watchtower, runs hourly)
# Manual: docker compose pull && docker compose up -d

# Check service status
docker ps

# View logs
docker logs <container_name>
```

## Contributing

This is a personal homelab, but the patterns here may be useful for others.
Feel free to fork and adapt for your own infrastructure.
