# Homelab Infrastructure

This repository contains the infrastructure-as-code for a homelab environment using Docker Compose and Ansible.

## Architecture

- **Ansible** orchestrates deployments via GitLab CI
- **Docker Compose** manages containers on the router host
- **Jinja2 templating** handles config files and secrets injection

## Key Paths

- `docker/docker-compose.yml` - Main compose file (Jinja2 templated)
- `ansible/inventory.yml` - Host variables and config file mappings
- `ansible/roles/configure-docker/` - Docker deployment role
- `docker/<service>/` - Service-specific config files

## Config Deployment Pattern

Config files in `docker/<service>/` get deployed to `/persistent_data/application/<service>/` via entries in `ansible/inventory.yml` under `docker_config`:

```yaml
docker_config:
  - src: "docker/myservice/config.yaml"
    dest: "{{ docker_persistent_data_path }}/myservice/config.yaml"
    name: "myservice"
```

## Secrets

- Never commit cleartext secrets
- Use Jinja2 env lookups: `{{ lookup('env', 'SECRET_NAME') }}`
- GitLab CI provides secrets as environment variables

## Common Commands

```bash
# Deploy via CI (preferred)
git push

# Manual ansible run
ansible-playbook -i ansible/inventory.yml ansible/router.yml
```

## Networks

- `external` - Internet-facing services (nginx)
- `internal` - Lab services (internal nginx, databases)
- `iot` - IoT devices (zigbee, mqtt, home assistant)

## Coding Conventions

- Use exponential backoff when polling or looping (e.g., waiting for pipelines, health checks). Cap at 5 minutes max between iterations unless otherwise specified.
