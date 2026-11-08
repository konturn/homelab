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
- `inventory/hardware.yml`, `inventory/dependencies.yml` - Disk slots, USB audio ports, service dependency chain. `base/udev/85-my-usb-audio.rules`, `base/disk/crypttab` and OpenClaw's `docker/openclaw/workspace/infra/TOOLS.md` (from `TOOLS.md.in` + repo data) are generated (`python3 ci/facts/render.py`); `facts:drift` checks the live router against them. See `inventory/README.md`

## Config Deployment Pattern

Config files in `docker/<service>/` get deployed to `/persistent_data/application/<service>/` via entries in `ansible/inventory.yml` under `docker_config`:

```yaml
docker_config:
  - src: "docker/myservice/config.yaml"
    dest: "{{ docker_persistent_data_path }}/myservice/config.yaml"
    name: "myservice"
```

When a deploy changes any of a container's config files, the container is restarted **once**, however many of its files changed. The nginx containers (`nginx_config` in inventory: `nginx`, `lab_nginx`, `iot_nginx`) are not restarted at all. They get `nginx -t` and then `nginx -s reload`, and a config that fails `nginx -t` fails the deploy while the old config keeps serving. Grafana reaches Loki through `lab_nginx`, so restarting it makes every Loki-backed alert rule's query fail.

## CPU priority

The router runs core services next to batch work (CI jobs, restic/B2 uploads,
ISO conversion, ZFS resilver/scrub). On cgroup v2 the scheduler splits CPU between
sibling cgroups by `cpu.weight`, so priority is set per cgroup, not with `nice`:

- Docker (cgroupfs driver) puts every container, CI job containers included,
  under `/sys/fs/cgroup/docker/`. runc maps compose `cpu_shares` to
  `cpu.weight` non-linearly: 4096 -> 303, 1024 (default) -> 100, 512 -> 59,
  256 -> 35.
- Core services, `cpu_shares: 4096`: pihole, vault, nginx, lab_nginx,
  iot_nginx, homeassistant, mosquitto, zigbee2mqtt, jit-approval-svc.
- Default (1024): everything else, including gitlab (heavy, would crowd out the
  others) and plex (GPU-bound).
- Batch, `cpu_shares: 256` (paperless-ngx 512, its web UI is interactive):
  nzbget, deluge, paperless-ai. CI jobs (`base/gitlab-runner/config.toml`):
  256, protected/deploy runner 512.
- Host batch units (restic-*, frigate-backup-*, ISO conversion scope):
  `CPUWeight=20` plus `Nice`/`IOSchedulingClass`.

IO weights (`blkio_config.weight`, `IOWeight=`) are not set: every disk uses
mq-deadline, which ignores io.weight, and pool IO is issued by ZFS threads, so
it carries no per-cgroup attribution anyway.

Weights only matter under contention; idle CPU is still used by anyone. Changing
`cpu_shares` in compose recreates that container on the next deploy.

## Docker Image Pinning

All third-party Docker images are pinned to SHA256 digests for reproducible deploys. Format: `image:tag@sha256:xxxx`.

**Exceptions (NOT pinned):**
- `gitlab-registry.lab.nkontur.com` images (nginx, snapcast, snapclient, amcrest2mqtt, jit-approval-svc, anomaly-detector, event-router, novnc, chromium-browser, openclaw, petlibro-camera) — built in CI, pinning would break deploys
- Images using Jinja2 template variables (e.g., `{{ openclaw_image_tag }}`)

Pin the **manifest-list (index) digest**, never a platform-specific one. An index
digest is still immutable and reproducible, but it lets each host resolve its own
architecture. A platform-specific digest hard-codes one architecture: the router is
amd64 while the satellite and zwave Pis are ARM (zwave reports `armhf`), so an amd64
digest deployed to them pulls fine — a digest pull skips platform selection entirely —
and then dies with an exec format error. That is exactly how `d15c1c4` broke promtail
on both Pis (see MR fixing it, 2026-09-01).

**When adding a new service:**
1. Find the index digest — the `Docker-Content-Digest` header for the *tag*:
   ```bash
   docker buildx imagetools inspect <image>:<tag> | head -2   # "Digest:" line
   ```
   Or without docker, straight from the registry:
   ```bash
   TOKEN=$(curl -s "https://auth.docker.io/token?service=registry.docker.io&scope=repository:<repo>:pull" | jq -r .token)
   curl -sI -H "Authorization: Bearer $TOKEN" \
     -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
     "https://registry-1.docker.io/v2/<repo>/manifests/<tag>" | grep -i docker-content-digest
   ```
2. Pin in compose: `image: name:tag@sha256:xxxxx`

**When updating an image version:**
1. Change the tag
2. Fetch the new index digest for the new tag
3. Update the `@sha256:` portion

**Verifying a pin is not architecture-locked:** fetch the manifest by digest and check
`.mediaType`. It must be an `...index.v1+json` or `...manifest.list.v2+json`. A bare
`...manifest.v2+json` / `...image.manifest.v1+json` is a single-platform pin and will
break any non-amd64 host. Renovate (docs/RENOVATE.md) already pins index
digests; this rule is for manual pins.

## Secrets

- Never commit cleartext secrets
- Secrets live in Vault: add the path and field to `vault_secret_paths` in
  `ansible/roles/fetch-vault-secrets/defaults/main.yml`, which turns it into an
  Ansible variable at deploy time; templates use `{{ variable_name }}`

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
