# Docker Socket Proxy + mTLS Architecture

## Overview

CI/CD jobs need Docker access on the lab host, but exposing the Docker socket directly is a security risk. This architecture uses a layered proxy chain with mTLS authentication to provide controlled, authenticated Docker API access.

## Request Flow

```
CI Runner (host network)
  │
  │  mTLS :2376
  ▼
lab_nginx (internal network)
  │
  │  HTTP :2375
  ▼
docker-socket-proxy (proxy-internal network)
  │
  │  unix socket
  ▼
/var/run/docker.sock
```

## Components

### CI Runner

- Runs with `network_mode: host` (single runner instance)
- Connects to `lab.nkontur.com:2376` using mTLS client certificates
- Environment: `DOCKER_HOST=tcp://lab.nkontur.com:2376`, `DOCKER_TLS_VERIFY=1`

### lab_nginx (mTLS Termination)

- Sits on the `internal` Docker network
- Terminates mTLS on port 2376, validates client certificates against the CA
- Proxies authenticated requests as plain HTTP to `docker-socket-proxy:2375`

### docker-socket-proxy (Tecnativa)

- Runs on a dedicated `docker-proxy` bridge network
- Bind-mounts `/var/run/docker.sock` read-only
- Whitelists specific Docker API endpoints (containers, images, networks, etc.)
- Provides the security boundary: even with valid mTLS certs, only allowed API calls pass through

## Certificate Generation

Certificates **must be generated before `docker compose up`**. If cert files are missing when Docker encounters bind mount sources, it creates them as **directories** instead of files, which breaks TLS configuration.

### CA Certificate

The CA certificate requires an explicit basic constraints extension:

```
basicConstraints = critical, CA:TRUE
```

Without this, some Docker/TLS clients reject the CA as invalid. Generate with:

```bash
openssl req -x509 -new -nodes \
  -key ca-key.pem \
  -days 3650 \
  -out ca.pem \
  -subj "/CN=Docker Proxy CA" \
  -addext "basicConstraints=critical,CA:TRUE"
```

### Server and Client Certs

Standard TLS cert generation signed by the CA above. Server cert gets SANs for `lab.nkontur.com`. Client cert is used by the CI runner.

## BuildKit Compatibility

**`DOCKER_BUILDKIT=0` is required** for CI jobs using this proxy.

BuildKit uses gRPC streaming and session-based APIs that are not part of the standard Docker Engine API. The Tecnativa docker-socket-proxy only exposes REST-style Docker API endpoints, so BuildKit operations fail with connection/protocol errors. Disabling BuildKit forces the classic build path which uses only standard API calls.

```yaml
# In CI job environment
variables:
  DOCKER_BUILDKIT: "0"
```

---

# Chromium Browser Sidecar

## Overview

A headless Chromium browser runs as a sidecar service for browser automation (CDP) and visual debugging (noVNC).

## Network & Access

| Interface | Address | Notes |
|-----------|---------|-------|
| CDP (Chrome DevTools Protocol) | `10.3.32.9:9222` | **IP only** — Chromium 144+ rejects Host headers containing hostnames |
| noVNC (visual access) | `browser.lab.nkontur.com` | Auth-gated with VNC password from Vault |

- Static IP `10.3.32.9` assigned via Ansible inventory variable `{{ chromium_browser_ip }}`
- CDP connections must use the IP directly, not a hostname, due to Chromium 144's Host header validation

## Container Configuration

- Runs as `root` and uses `gosu` to drop privileges to the `chrome` user
- Daily restart via cron at 4:00 AM to clear accumulated state and memory
- Resume/upload bind mount: `/persistent_data/application/chromium-browser/uploads:/uploads:ro`

## noVNC Authentication

VNC password is stored in HashiCorp Vault and injected at container startup. This gates visual access through the `browser.lab.nkontur.com` reverse proxy.
