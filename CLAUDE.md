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

## Docker Image Pinning

All third-party Docker images are pinned to SHA256 digests for reproducible deploys. Format: `image:tag@sha256:xxxx`.

**Exceptions (NOT pinned):**
- `gitlab-registry.lab.nkontur.com` images (snapcast, snapclient, amcrest2mqtt, moltbot) — built in CI, pinning would break deploys
- Images using Jinja2 template variables (e.g., `{{ moltbot_image_tag }}`)

**When adding a new service:**
1. Find the amd64 digest: `docker manifest inspect <image> | jq -r '.manifests[] | select(.platform.architecture=="amd64") | .digest'`
2. Pin in compose: `image: name:tag@sha256:xxxxx`

**When updating an image version:**
1. Change the tag
2. Fetch the new digest for the new tag
3. Update the `@sha256:` portion

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
