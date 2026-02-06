# Reverse Proxy Migration: nginx to Traefik

**Author:** Prometheus (moltbot)
**Date:** 2026-02-05
**Status:** Draft / RFC
**Audience:** Noah (homelab owner/operator)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Current Architecture](#current-architecture)
3. [Proposed Traefik Architecture](#proposed-traefik-architecture)
4. [Migration Strategy](#migration-strategy)
5. [Security Analysis](#security-analysis)
6. [Operational & Management Implications](#operational--management-implications)
7. [Alternatives Considered](#alternatives-considered)
8. [Recommendation](#recommendation)
9. [Appendix: Example Configurations](#appendix-example-configurations)

---

## Executive Summary

This document evaluates migrating the homelab reverse proxy from nginx (with crossplane-compiled JSON drop-in configs) to Traefik (Docker-native label-based routing). It covers architecture, migration strategy, security implications, operational trade-offs, and alternatives.

**Bottom line up front:** The migration introduces real security trade-offs (Docker socket exposure, label injection surface) that must be weighed against operational gains (automatic service discovery, native ACME, unified config-as-code). The recommendation is at the end, after the full analysis.

---

## Current Architecture

### Overview

The homelab runs **three separate nginx instances**, each bound to a different VLAN via macvlan networking:

| Instance | Network | VLAN | IP Range | Purpose |
|----------|---------|------|----------|---------|
| `nginx` | external | bond0.2 | 10.2.x.x | Public-facing: nkontur.com, Plex, Nextcloud, Bitwarden, Audioserve, WordPress |
| `lab_nginx` | internal | bond0.3 | 10.3.x.x | Lab services: GitLab, Grafana, Paperless, Radarr, Sonarr, etc. |
| `iot_nginx` | iot | bond0.6 | 10.6.x.x | IoT services: Mopidy, Zigbee2MQTT, etc. |

### Config Generation Pipeline

nginx configs are **not hand-written**. They're generated via a Python script (`generate-configs.py`) that:

1. Reads `docker-compose.yml` to discover services on each network
2. Reads JSON drop-in configs (`http-external-drop-in.conf`, `http-internal-drop-in.conf`) for services needing custom configuration (Plex, Paperless, GitLab Registry, nkontur.com static site)
3. Generates JSON intermediate configs
4. Compiles them to nginx config files using **crossplane** (nginx JSON-to-config compiler)

This means the system already has a form of auto-discovery: services get a reverse proxy entry automatically if they're on the right network with the right ports. Custom settings are layered on via JSON drop-ins.

### SSL/TLS

- Wildcard cert for `*.nkontur.com`, `*.lab.nkontur.com`, `*.iot.lab.nkontur.com`
- Renewed via **weekly cron** using certbot with NameSilo DNS challenge
- Cert files shared across all three nginx instances via volume mount to `/data/certs`
- Strong TLS config in `ssl_config`: TLSv1.2+, modern cipher suites, HSTS, OCSP stapling, DH params

### Security Headers (from `ssl_config`)

```
Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
X-Frame-Options: SAMEORIGIN
X-XSS-Protection: 1; mode=block
Referrer-Policy: no-referrer
```

### Rate Limiting

Two zones defined in the HTTP master template:
- `auth_strict`: 5 requests/minute per IP (for auth endpoints)
- `general`: 10 requests/second per IP (for everything else, burst=20)

### Notable Custom Configs

**nkontur.com (external drop-in):**
- Static file hosting from `/data/webroot/html` with alias locations for `/publications`, `/poetry`, `/net-map`, `/contact`
- Plex reverse proxy at `/plex/` with extensive custom settings: 240s timeouts, WebSocket upgrade, custom buffer sizes, proxy cache bypass, X-Frame-Options removal
- Ombi (plex-requests) at `/plex-requests/`
- Rewrite rules for Plex media paths

**Internal drop-in:**
- Paperless-NGX: WebSocket support, custom P3P header, 240s timeouts, Docker DNS resolver
- GitLab Registry: chunked transfer, unlimited body size, 900s timeouts, no buffering, Docker Distribution API header

### What Works Well

- Auto-discovery from docker-compose already exists
- Strong TLS configuration
- Per-service customization via drop-ins
- Three isolated nginx instances (good blast radius containment)
- Simple mental model: each VLAN has its own nginx

### What's Painful

- JSON-based config format is verbose and hard to read/edit
- Crossplane adds a build step and dependency
- Adding a new service with custom config requires editing JSON, understanding the template system
- No automatic HTTPS renewal in the hot path (weekly cron, potential for expired certs if cron fails)
- Three separate nginx instances to manage
- No native metrics/monitoring
- No dashboard for debugging routing

---

## Proposed Traefik Architecture

### Core Concepts

Traefik uses a fundamentally different model:

- **Entrypoints**: Network addresses Traefik listens on (ports/IPs)
- **Routers**: Rules that match incoming requests (Host, Path, Headers) and route them to services
- **Services**: The actual backend containers
- **Middleware**: Request/response transformations (rate limiting, headers, auth, etc.)
- **Providers**: Sources of configuration (Docker labels, file, Consul, etc.)

### How Traefik Replaces nginx

Instead of three nginx instances, a single Traefik instance would:

1. Connect to **all three networks** (external, internal, iot)
2. Define **entrypoints** bound to specific IPs on each network
3. Use the **Docker provider** to auto-discover services via labels
4. Apply **middleware chains** for security headers, rate limiting, etc.
5. Handle **ACME certificates** natively via NameSilo DNS challenge

### Two-Network (Three-Network) Entrypoint Setup

```yaml
# traefik static config
entryPoints:
  web-external:
    address: ":80"        # On external network IP
  websecure-external:
    address: ":443"       # On external network IP
  web-internal:
    address: ":80"        # On internal network IP  
  websecure-internal:
    address: ":443"       # On internal network IP
  web-iot:
    address: ":80"        # On iot network IP
  websecure-iot:
    address: ":443"       # On iot network IP
```

**Problem:** Traefik can listen on multiple ports but binding to specific IPs per network is complicated with macvlan. The current setup uses macvlan with static IPs per nginx instance. Traefik would need to either:

**Option A: Single Traefik with multiple IPs** — Traefik gets a static IP on each macvlan network and binds entrypoints to specific IPs. This is supported via `address: "10.2.x.x:443"` but puts all routing logic in one container.

**Option B: Multiple Traefik instances** — One per network, like the current nginx setup. Defeats much of the purpose of migrating.

**Option C: Single Traefik with network-aware routing** — Traefik on all networks, using router rules to restrict which services are reachable from which entrypoint. This is the typical Traefik approach but requires careful labeling.

Option A is the most practical. The entrypoint config would look like:

```yaml
entryPoints:
  websecure-external:
    address: "10.2.x.x:443"
  websecure-internal:
    address: "10.3.x.x:443"
  websecure-iot:
    address: "10.6.x.x:443"
```

### Static File Hosting

Traefik does not serve static files. The nkontur.com static site would need a **separate container**:

```yaml
nkontur-static:
  image: nginx:alpine  # or caddy, or any static file server
  container_name: nkontur-static
  volumes:
    - /path/to/webroot:/usr/share/nginx/html:ro
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.nkontur-static.rule=Host(`nkontur.com`, `www.nkontur.com`)"
    - "traefik.http.routers.nkontur-static.entrypoints=websecure-external"
    - "traefik.http.routers.nkontur-static.tls.certResolver=namesilo"
```

This is actually cleaner: the static site becomes its own service rather than being embedded in the proxy config. But it adds a container.

### Certificate Management: ACME with NameSilo

Traefik uses [lego](https://go-acme.github.io/lego/) internally for ACME. NameSilo is a supported DNS provider.

```yaml
certificatesResolvers:
  namesilo:
    acme:
      email: konoahko@gmail.com
      storage: /data/acme.json
      dnsChallenge:
        provider: namesilo
        resolvers:
          - "1.1.1.1:53"
          - "8.8.8.8:53"
```

Environment variable needed: `NAMESILO_API_KEY`

**Advantages over current cron:**
- Automatic renewal (Traefik handles it continuously)
- No separate certbot dependency
- No cron failure risk
- Wildcard certs supported natively

**Risks:**
- NameSilo's DNS propagation is notoriously slow (up to 15 minutes). Traefik's `delayBeforeChecks` setting may need tuning.
- Single `acme.json` file for all certs. Corruption = all certs gone (mitigated by persistent volume + backups).
- If Traefik restarts during renewal, it picks up where it left off, but there could be brief periods without valid certs.

### Dashboard

Traefik has a built-in dashboard showing routers, services, middleware, and health.

```yaml
api:
  dashboard: true
  insecure: false  # NEVER expose insecurely

# Access via internal entrypoint only, with basic auth
labels:
  - "traefik.http.routers.dashboard.rule=Host(`traefik.lab.nkontur.com`)"
  - "traefik.http.routers.dashboard.entrypoints=websecure-internal"
  - "traefik.http.routers.dashboard.middlewares=dashboard-auth"
  - "traefik.http.middlewares.dashboard-auth.basicauth.users=admin:$$hashed$$password"
```

---

## Migration Strategy

### Phase 0: Preparation (1-2 days)

1. Create Traefik static configuration files (entrypoints, providers, ACME)
2. Create shared middleware definitions (security headers, rate limiting)
3. Set up Docker socket proxy (see Security section)
4. Add Traefik to docker-compose.yml on alternate ports
5. Create static site container for nkontur.com

### Phase 1: Parallel Deployment (1-2 days)

Deploy Traefik alongside nginx, listening on non-standard ports:

```yaml
traefik:
  image: traefik:v3.4
  container_name: traefik
  networks:
    external:
      ipv4_address: 10.2.x.y   # Different IP from nginx
    internal:
      ipv4_address: 10.3.x.y
    iot:
      ipv4_address: 10.6.x.y
    socket_proxy:
  # ... config ...
```

Test each service by curling the Traefik IP directly. Both nginx and Traefik serve traffic simultaneously.

### Phase 2: Service-by-Service Migration (3-5 days)

Migrate services one at a time, starting with internal (lowest risk):

**Migration order:**
1. Internal non-critical: diagram, iperf3
2. Internal important: Grafana, Radarr, Sonarr, Jackett, NZBGet
3. Internal critical: GitLab, Paperless, Deluge
4. IoT: Mopidy, Zigbee2MQTT
5. External non-critical: WordPress/blog, Ombi
6. External critical: Bitwarden, Nextcloud, Plex, nkontur.com

For each service:
1. Add Traefik labels to the service in docker-compose
2. Verify routing works via Traefik IP
3. Remove the service from nginx config / drop-in
4. Verify nginx still serves remaining services

**Rollback:** Remove labels, re-add nginx config. Since both are running, rollback is instant.

### Phase 3: DNS Cutover

Once all services route through Traefik:

1. Update DNS records to point to Traefik IPs (or swap IPs: give Traefik the old nginx IPs)
2. Monitor for 24-48 hours
3. Remove nginx containers
4. Remove crossplane pipeline step from CI

### Phase 4: Cleanup (1 day)

1. Remove nginx configs, generate-configs.py, crossplane dependency
2. Update CI pipeline
3. Update documentation
4. Verify ACME cert renewal works end-to-end

### Timeline Estimate

| Phase | Duration | Risk |
|-------|----------|------|
| Preparation | 1-2 days | Low |
| Parallel deployment | 1-2 days | Low |
| Service migration | 3-5 days | Medium |
| DNS cutover | 1 day + 48h monitoring | High |
| Cleanup | 1 day | Low |
| **Total** | **~2 weeks** | |

### Example docker-compose Labels (Final State)

**Simple internal service (Grafana):**
```yaml
grafana:
  # ... existing config ...
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.grafana.rule=Host(`grafana.lab.nkontur.com`)"
    - "traefik.http.routers.grafana.entrypoints=websecure-internal"
    - "traefik.http.routers.grafana.tls.certResolver=namesilo"
    - "traefik.http.routers.grafana.middlewares=security-headers@file,internal-rate-limit@file"
    - "traefik.http.services.grafana.loadbalancer.server.port=3000"
```

**Complex external service (Plex):**
```yaml
plex:
  # ... existing config ...
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.plex.rule=Host(`nkontur.com`) && PathPrefix(`/plex`)"
    - "traefik.http.routers.plex.entrypoints=websecure-external"
    - "traefik.http.routers.plex.tls.certResolver=namesilo"
    - "traefik.http.routers.plex.middlewares=security-headers@file,plex-strip@file,external-rate-limit@file"
    - "traefik.http.services.plex.loadbalancer.server.port=32400"
    - "traefik.http.services.plex.loadbalancer.responseForwarding.flushInterval=100ms"
    # Custom transport for Plex's long timeouts
    - "traefik.http.serversTransports.plex.forwardingTimeouts.dialTimeout=240s"
    - "traefik.http.serversTransports.plex.forwardingTimeouts.responseHeaderTimeout=240s"
```

**GitLab Registry (complex internal):**
```yaml
gitlab:
  # ... existing config ...
  labels:
    - "traefik.enable=true"
    # Main GitLab
    - "traefik.http.routers.gitlab.rule=Host(`gitlab.lab.nkontur.com`)"
    - "traefik.http.routers.gitlab.entrypoints=websecure-internal"
    - "traefik.http.routers.gitlab.tls.certResolver=namesilo"
    - "traefik.http.services.gitlab.loadbalancer.server.port=80"
    # Registry (separate router, same container)
    - "traefik.http.routers.gitlab-registry.rule=Host(`gitlab-registry.lab.nkontur.com`)"
    - "traefik.http.routers.gitlab-registry.entrypoints=websecure-internal"
    - "traefik.http.routers.gitlab-registry.tls.certResolver=namesilo"
    - "traefik.http.routers.gitlab-registry.middlewares=registry-buffering@file"
    - "traefik.http.services.gitlab-registry.loadbalancer.server.port=5050"
```

**Bitwarden (external, security-critical):**
```yaml
bitwarden:
  # ... existing config ...
  labels:
    - "traefik.enable=true"
    - "traefik.http.routers.bitwarden.rule=Host(`bitwarden.nkontur.com`)"
    - "traefik.http.routers.bitwarden.entrypoints=websecure-external"
    - "traefik.http.routers.bitwarden.tls.certResolver=namesilo"
    - "traefik.http.routers.bitwarden.middlewares=security-headers@file,auth-rate-limit@file"
    - "traefik.http.services.bitwarden.loadbalancer.server.port=80"
```

---

## Security Analysis

This is the most important section of this document.

### 1. Docker Socket Access — The Elephant in the Room

**Context:** We recently removed Docker socket access from moltbot specifically to reduce attack surface. Now we'd be giving it to Traefik, which is internet-facing.

**The threat model:**

If Traefik is compromised (e.g., via an HTTP request exploit, a CVE in Go's HTTP stack, or a vulnerability in Traefik itself), an attacker with Docker socket access can:

- List all containers and their environment variables (including secrets like `GITLAB_TOKEN`, `HASS_TOKEN`, database passwords, API keys)
- Start new containers with host filesystem mounts
- Execute commands in running containers
- Stop/remove containers (DoS)
- Effectively gain root on the host

**This is not theoretical.** Traefik is internet-facing. It processes untrusted HTTP requests. nginx has had fewer CVEs historically than application-level proxies because it's written in C with a simpler processing model.

**Mitigation: Docker Socket Proxy**

Use [Tecnativa docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) (already deployed in this homelab for CI builds):

```yaml
traefik-socket-proxy:
  image: tecnativa/docker-socket-proxy:latest
  container_name: traefik-socket-proxy
  restart: unless-stopped
  networks:
    - socket_proxy  # Isolated network, only Traefik can reach it
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock:ro
  environment:
    # Traefik only needs to READ container info
    - CONTAINERS=1        # Read container list & metadata
    - NETWORKS=1          # Read network info for routing
    - SERVICES=0          # Not using Swarm
    - TASKS=0             # Not using Swarm
    - POST=0              # Block ALL write operations
    - BUILD=0
    - COMMIT=0
    - CONFIGS=0
    - DISTRIBUTION=0
    - EXEC=0              # Critical: no exec into containers
    - IMAGES=0
    - INFO=0
    - PLUGINS=0
    - SECRETS=0
    - VOLUMES=0
    - AUTH=0
    - EVENTS=1            # Traefik needs events to detect changes
```

Then Traefik connects to the proxy instead of the socket directly:

```yaml
traefik:
  environment:
    - DOCKER_HOST=tcp://traefik-socket-proxy:2375
  # NO docker.sock volume mount
```

**Residual risk with socket proxy:**
- Attacker can still **read** all container metadata (labels, env vars, network config)
- Container environment variables often contain secrets (visible via `GET /containers/{id}/json`)
- The socket proxy itself is an additional dependency and attack surface
- Supply chain risk: the socket proxy image could be compromised

**Assessment:** The socket proxy significantly reduces blast radius (no write access = no container exec, no new containers, no host filesystem access). But the **read access alone exposes all secrets in environment variables**. This is a real concern given the homelab docker-compose has dozens of secrets as env vars.

**Additional mitigation:** Consider using Vault for secret injection instead of environment variables, so container metadata doesn't contain plaintext secrets. (This is a larger project but aligns with the Vault deployment already in the homelab.)

### 2. Label Injection

**The attack:** A compromised container can set its own Docker labels. If Traefik reads labels from all containers, a compromised container could add labels to route traffic to itself, e.g., routing `bitwarden.nkontur.com` to a malicious container that harvests credentials.

**Mitigations:**

1. **`exposedByDefault: false`** — Containers must explicitly opt in with `traefik.enable=true`. But a compromised container can set this label on itself.

2. **`constraints` expression** — Traefik can filter which containers it reads:
   ```yaml
   providers:
     docker:
       constraints: "Label(`traefik.managed`, `true`)"
   ```
   A compromised container could still add this label, though.

3. **The real problem:** Docker labels are controlled by whoever creates the container. In a compose-based setup, labels come from the compose file, which is in git. A compromised *running* container cannot change its own labels (labels are set at creation time and are immutable). **This means label injection requires compromising the compose file or the Docker API.**

   With the socket proxy blocking write operations, a compromised Traefik instance cannot modify container labels or create new containers. Label injection via a running container is not possible.

**Assessment:** Low risk in practice, as long as:
- The socket proxy blocks write operations
- The compose file is reviewed before deploy (existing CI/CD flow)
- `exposedByDefault: false` is set

### 3. Network Exposure

**Current state:** Three separate nginx instances, each on one network. Blast radius is isolated: compromised external nginx cannot reach internal services.

**Traefik proposal:** Single container on **all three networks** (external, internal, iot). This means:

- A compromised Traefik can reach **every service on every network**
- Traefik becomes a pivot point between the external internet and internal/iot networks
- This is a significant regression from the current isolation model

**Mitigations:**
- Network policies (Docker doesn't support them natively without plugins)
- Running separate Traefik instances per network (defeats consolidation benefits)
- Using entrypoint binding to specific IPs to limit what's reachable per entrypoint

**Assessment:** This is the **biggest security concern**. The current three-nginx architecture provides genuine network-level isolation. A single Traefik spanning all networks removes this isolation. Even with proper entrypoint configuration, a compromised Traefik process has network-level access to everything.

### 4. TLS Configuration

Traefik supports strong TLS defaults via dynamic configuration:

```yaml
# dynamic/tls.yaml
tls:
  options:
    default:
      minVersion: VersionTLS12
      cipherSuites:
        - TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305
        - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305
      curvePreferences:
        - CurveP521
        - CurveP384
      sniStrict: true
```

**Parity with current setup:** Full parity is achievable. Traefik supports TLSv1.2+, modern cipher suites, HSTS, and OCSP stapling. DH params are not configurable in Traefik (uses Go's built-in ephemeral DH), which is actually fine since ECDHE is preferred anyway.

**One catch:** Traefik's Go TLS implementation handles TLS differently than nginx's OpenSSL. Go's TLS is generally secure by default but has occasionally had different behavior around edge cases (client cert handling, TLS session resumption). In practice, this is not a meaningful risk.

### 5. Rate Limiting: Traefik vs nginx

| Feature | nginx `limit_req` | Traefik `rateLimit` middleware |
|---------|-------------------|-------------------------------|
| Algorithm | Leaky bucket | Token bucket |
| Per-IP limiting | Yes (via `$binary_remote_addr`) | Yes (via `sourceCriterion.requestHeaderName` or IP) |
| Burst handling | `burst=N nodelay` | `burst=N` (built into token bucket) |
| Shared state | Shared memory zones | In-memory (single instance only) |
| Multiple zones | Yes (auth_strict, general) | Yes (multiple middleware definitions) |
| Custom status code | Yes (`limit_req_status 429`) | Yes (HTTP 429 by default) |
| Exemptions | Via map/geo blocks | Via `sourceCriterion.ipStrategy.excludedIPs` |

**Key difference:** nginx's leaky bucket smooths traffic over time. Traefik's token bucket allows bursts up to the bucket size, then enforces the average rate. Both are effective but behave differently under bursty traffic.

**Current config translation:**
```yaml
# nginx: rate=10r/s burst=20 nodelay → Traefik equivalent:
http:
  middlewares:
    general-rate-limit:
      rateLimit:
        average: 10    # requests per second
        burst: 20
    auth-rate-limit:
      rateLimit:
        average: 1     # 5r/m = ~0.083r/s, round to 1
        period: 12s    # 1 request per 12 seconds ≈ 5/minute
        burst: 2
```

**Limitation:** Traefik's rate limiter is in-process memory only. No shared state across multiple Traefik instances. For a single-instance homelab, this is fine.

**Assessment:** Functional parity exists but the behavior is subtly different. The token bucket approach is arguably better for user experience (allows bursts), but the leaky bucket is more predictable for security (steady drain rate).

### 6. Security Headers

Traefik supports security headers via the `headers` middleware:

```yaml
http:
  middlewares:
    security-headers:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
        stsPreload: true
        frameDeny: true              # X-Frame-Options: DENY
        # or: customFrameOptionsValue: "SAMEORIGIN"
        contentTypeNosniff: true     # X-Content-Type-Options: nosniff
        browserXssFilter: true       # X-XSS-Protection: 1; mode=block
        referrerPolicy: "no-referrer"
        # CSP can be added:
        # contentSecurityPolicy: "default-src 'self'"
```

**Parity:** Full parity with current nginx `ssl_config` headers. Traefik actually makes it easier to apply different header policies per service (different middleware chains).

### 7. Access Logs

Traefik supports JSON-structured access logs:

```yaml
accessLog:
  filePath: "/data/log/access.log"
  format: json
  fields:
    headers:
      names:
        User-Agent: keep
        Authorization: drop
        Content-Type: keep
```

**Advantage over nginx:** JSON logs integrate better with Loki/Grafana (already deployed). Nginx's default log format requires parsing.

---

## Operational & Management Implications

### Day-to-Day Complexity

| Aspect | nginx (current) | Traefik (proposed) |
|--------|----------------|-------------------|
| Adding a service | Edit JSON drop-in or rely on auto-discovery | Add labels to docker-compose service |
| Custom proxy settings | Edit JSON drop-in | Add middleware labels or file-based middleware |
| Viewing active routes | Read generated configs or check nginx -T | Dashboard UI or `traefik.lab.nkontur.com` |
| Cert management | Pray the cron works; manually renew if not | Automatic; check dashboard for cert status |
| Debugging routing | `nginx -t`, access logs, error logs | Dashboard, access logs, `--log.level=DEBUG` |
| Config validation | crossplane build step | Traefik validates on startup; labels checked at runtime |

**Verdict:** Day-to-day, Traefik is simpler for common operations (adding services, checking routes). More complex for unusual configurations that don't fit the label model.

### Debugging

**nginx debugging patterns:**
- `nginx -t` for config validation
- Access/error logs with well-known formats
- Decades of Stack Overflow answers
- `curl -v` against the server

**Traefik debugging patterns:**
- Dashboard shows active routers, services, middleware, errors
- `--log.level=DEBUG` for verbose output (very verbose)
- Access logs in JSON format
- `curl -v` still works
- Prometheus metrics for detailed request analysis
- Traefik's error pages show middleware chain issues

**Assessment:** Traefik's dashboard is genuinely useful for debugging routing issues. But when things go wrong, nginx's failure modes are more predictable and well-documented. Traefik's label-based config can be harder to reason about ("why isn't my router matching?").

### Monitoring & Metrics

Traefik exposes Prometheus metrics natively:

```yaml
metrics:
  prometheus:
    entryPoint: metrics
    addEntryPointsLabels: true
    addRoutersLabels: true
    addServicesLabels: true
```

Metrics include:
- Request count, duration, size per router/service/entrypoint
- TLS handshake duration
- Open connections
- Retry count
- Health check status

**Integration with existing stack:** InfluxDB can scrape Prometheus metrics (or use Telegraf as an intermediary). Grafana has built-in Prometheus datasource support and community dashboards for Traefik.

This is a genuine upgrade. Currently there's no easy way to get per-service request metrics from nginx without custom log parsing.

### Config as Code: Labels vs Files

**Labels in docker-compose (Traefik approach):**
- ✅ Routing config lives next to the service definition
- ✅ Single source of truth (docker-compose.yml)
- ✅ GitOps-friendly: PR to change routing = PR to change service
- ❌ docker-compose.yml becomes very long and noisy with labels
- ❌ Complex middleware definitions don't fit well in labels (use file provider instead)
- ❌ Labels are strings; no syntax validation until Traefik reads them

**Separate nginx configs (current approach):**
- ✅ Routing config is separate from service definition (separation of concerns)
- ✅ nginx -t validates syntax before deploy
- ❌ Config split across multiple files/formats (JSON drop-ins, templates, Python generator)
- ❌ Two places to update when adding a service

**Hybrid approach (recommended with Traefik):** Use labels for simple routing (most services) and the Traefik **file provider** for complex middleware chains, TLS options, and shared definitions. Best of both worlds.

### Rollback Plan

**During migration (parallel deployment):** Instant rollback. Remove Traefik labels, nginx is still running.

**After full cutover:**
1. Traefik bug: Check if fixable via config change (fast)
2. Traefik CVE: Pull updated image, restart (minutes)
3. Traefik fundamentally broken: Revert docker-compose to remove Traefik, re-add nginx configs from git history, redeploy via Ansible (~30 minutes)
4. Keep nginx configs in a `legacy/` directory for 3 months after migration

**Assessment:** Rollback is manageable but not instant after full cutover. The 30-minute window for a full revert is acceptable for a homelab.

### Learning Curve

**Traefik concepts that differ from nginx:**
- Routers (≈ nginx server blocks + location blocks)
- Services (≈ nginx upstream blocks)
- Middleware (≈ nginx modules/directives, but composable chains)
- Providers (new concept: where config comes from)
- Entrypoints (≈ nginx listen directives)
- Certificate resolvers (≈ certbot, but integrated)

The label syntax is initially confusing:
```
traefik.http.routers.myapp.middlewares=auth@file,rate-limit@file
```

But once the pattern is understood (`traefik.{protocol}.{type}.{name}.{property}`), it becomes predictable.

**Estimate:** 1-2 days to become comfortable, 1-2 weeks to handle edge cases confidently.

### Community & Ecosystem

| Aspect | nginx | Traefik |
|--------|-------|---------|
| Age | 2004 | 2016 |
| GitHub stars | ~80k (unofficial) | ~53k |
| Docker Hub pulls | Billions | 4B+ |
| CVE count (last 5yr) | ~40 | ~15 |
| Documentation | Extensive, well-organized | Good, but can be confusing (v1/v2/v3 docs coexist) |
| Community support | Massive (StackOverflow, forums) | Large (community forum, Reddit, Discord) |
| Homelab adoption | Still dominant | Very popular, arguably default for Docker homelabs |

**Assessment:** Both are mature. nginx has deeper roots and more community knowledge. Traefik is the de facto standard for Docker-native reverse proxying in homelabs.

---

## Alternatives Considered

### Option 1: Keep nginx (Status Quo)

**Pros:**
- Working, tested, stable
- Three isolated instances = great blast radius containment
- No Docker socket exposure
- Mature, well-understood technology
- Strong TLS defaults already configured

**Cons:**
- JSON drop-in format is painful to edit
- Crossplane build dependency
- Manual cert renewal (cron-based)
- No native metrics
- No dashboard
- Adding services with custom configs requires understanding the template system

**When to choose this:** If security isolation is the top priority and the operational pain is tolerable.

### Option 2: Caddy

**Pros:**
- Automatic HTTPS out of the box (ACME built-in)
- Extremely simple Caddyfile syntax
- No Docker socket needed (file-based config)
- NameSilo DNS challenge supported via [caddy-dns/namesilo](https://github.com/caddy-dns/namesilo) plugin (requires custom build with `xcaddy`)
- Handles static files natively
- Smaller attack surface than Traefik (no Docker provider)

**Cons:**
- No Docker auto-discovery (must maintain Caddyfile manually)
- NameSilo plugin requires building a custom Caddy binary
- Less mature ecosystem than nginx
- Custom plugin had build issues reported in community forums
- No dashboard (though API exists)
- Fewer middleware options than Traefik

**Caddyfile example for the homelab:**
```
nkontur.com, www.nkontur.com {
    root * /data/webroot/html
    file_server

    handle_path /plex/* {
        reverse_proxy plex:32400 {
            transport http {
                dial_timeout 240s
                response_header_timeout 240s
            }
        }
    }

    handle_path /plex-requests/* {
        reverse_proxy ombi:3579
    }

    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

grafana.lab.nkontur.com {
    reverse_proxy grafana:3000
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}
```

**When to choose this:** If you want automatic HTTPS without Docker socket exposure and don't mind maintaining a config file. Good middle ground between nginx and Traefik.

### Option 3: nginx-proxy + acme-companion

[nginx-proxy](https://github.com/nginx-proxy/nginx-proxy) + [acme-companion](https://github.com/nginx-proxy/acme-companion)

**Pros:**
- Docker-native nginx (auto-discovers via Docker socket)
- Familiar nginx under the hood
- ACME companion handles Let's Encrypt automatically
- Well-established project

**Cons:**
- Still requires Docker socket access (same concern as Traefik)
- Less flexible than Traefik (no middleware chains, limited routing rules)
- DNS challenge support is limited (primarily HTTP-01)
- Less active development than Traefik
- Doesn't support the NameSilo DNS challenge natively
- No native metrics

**When to choose this:** If you want Docker auto-discovery with nginx and don't need DNS challenge for cert renewal. Not viable here because we need DNS challenge for internal services.

---

## Recommendation

### The Decision Matrix

| Criteria | Weight | nginx (current) | Traefik | Caddy |
|----------|--------|-----------------|---------|-------|
| Security isolation | High | ★★★★★ | ★★☆☆☆ | ★★★★☆ |
| Docker socket risk | High | ★★★★★ (none) | ★★☆☆☆ | ★★★★★ (none) |
| Operational simplicity | Medium | ★★☆☆☆ | ★★★★☆ | ★★★★★ |
| Auto cert management | Medium | ★★☆☆☆ | ★★★★★ | ★★★★☆ |
| Monitoring/metrics | Medium | ★☆☆☆☆ | ★★★★★ | ★★★☆☆ |
| Config as code | Medium | ★★★☆☆ | ★★★★☆ | ★★★★☆ |
| Rollback safety | Low | ★★★★★ | ★★★☆☆ | ★★★★☆ |
| Dashboard | Low | ☆☆☆☆☆ | ★★★★★ | ★★☆☆☆ |

### Recommendation: Caddy, Not Traefik

After thorough analysis, **Caddy is the better choice for this homelab**, and here's why:

**1. The Docker socket problem is real.**

We removed Docker socket from moltbot for good reason. Giving it to an internet-facing service is a step backward. The socket proxy mitigates but doesn't eliminate the risk. And the read-only socket still exposes every secret stored in container environment variables. This is not a theoretical concern — it's the exact attack vector we were protecting against.

**2. The network isolation regression is real.**

Going from three isolated nginx instances to one Traefik spanning all networks is a meaningful security downgrade. A single Traefik compromise gives an attacker a network pivot point across external, internal, and IoT VLANs. The current architecture correctly isolates these.

**3. Caddy solves the actual pain points without the security trade-offs.**

The real pain points are:
- JSON config format (Caddy: simple Caddyfile syntax)
- Manual cert renewal (Caddy: built-in ACME with DNS challenge)
- No metrics (Caddy: Prometheus metrics available)
- Crossplane dependency (Caddy: no build step)

What Caddy does NOT require:
- Docker socket access (file-based config)
- Spanning all networks (can run separate instances like nginx, or single instance)
- Label-based discovery (explicit config, reviewable in git)

**4. Caddy handles the nkontur.com static site natively.** No need for a separate container.

**5. The auto-discovery is less valuable than it seems.** Services change rarely (maybe once a month). Writing a Caddyfile entry is trivially fast. The crossplane/JSON template system is what's painful, not the lack of auto-discovery.

### Implementation Plan (Caddy)

If Caddy is chosen, a follow-up design doc should be created covering:

1. **Caddyfile structure** — One Caddyfile per network (external, internal, iot) or a single Caddyfile with proper site blocks
2. **Custom Caddy build** — xcaddy with caddy-dns/namesilo plugin, built via GitLab CI, pushed to registry
3. **Migration** — Same parallel deployment strategy as described above
4. **Three instances or one** — Evaluate whether to keep the isolated-per-network model (recommended for security) or consolidate

### If Traefik Is Still Preferred

If the decision is made to proceed with Traefik despite the security concerns, the **minimum requirements** are:

1. **Docker socket proxy** — Non-negotiable. Use Tecnativa proxy with read-only, events-only permissions.
2. **`exposedByDefault: false`** — Non-negotiable.
3. **`security_opt: no-new-privileges:true`** — On the Traefik container.
4. **Dedicated socket proxy network** — Isolated from all other networks.
5. **Secret migration to Vault** — Move secrets out of environment variables so Docker metadata doesn't expose them.
6. **Accept the network isolation regression** — Or run three Traefik instances (which negates most benefits).

---

## Appendix: Example Configurations

### A. Traefik Static Config (if Traefik is chosen)

```yaml
# traefik.yml
global:
  checkNewVersion: false
  sendAnonymousUsage: false

log:
  level: INFO
  format: json

accessLog:
  filePath: /data/log/access.log
  format: json
  bufferingSize: 100

api:
  dashboard: true
  insecure: false

entryPoints:
  websecure-external:
    address: ":443"
    http:
      tls:
        certResolver: namesilo
        options: modern@file
    forwardedHeaders:
      insecure: false
  websecure-internal:
    address: ":8443"
    http:
      tls:
        certResolver: namesilo
        options: modern@file
  metrics:
    address: ":8082"

providers:
  docker:
    endpoint: "tcp://traefik-socket-proxy:2375"
    exposedByDefault: false
    network: proxy
    watch: true
  file:
    directory: /etc/traefik/dynamic
    watch: true

certificatesResolvers:
  namesilo:
    acme:
      email: konoahko@gmail.com
      storage: /data/acme.json
      dnsChallenge:
        provider: namesilo
        resolvers:
          - "1.1.1.1:53"
          - "8.8.8.8:53"
        propagation:
          delayBeforeChecks: 900  # NameSilo is slow, wait 15 minutes

metrics:
  prometheus:
    entryPoint: metrics
    addEntryPointsLabels: true
    addRoutersLabels: true
    addServicesLabels: true
```

### B. Traefik Dynamic Config (shared middleware)

```yaml
# dynamic/middleware.yml
http:
  middlewares:
    security-headers:
      headers:
        stsSeconds: 31536000
        stsIncludeSubdomains: true
        stsPreload: true
        customFrameOptionsValue: "SAMEORIGIN"
        contentTypeNosniff: true
        browserXssFilter: true
        referrerPolicy: "no-referrer"
        forceSTSHeader: true

    external-rate-limit:
      rateLimit:
        average: 10
        burst: 20

    auth-rate-limit:
      rateLimit:
        average: 5
        period: 60s
        burst: 2

    internal-rate-limit:
      rateLimit:
        average: 50
        burst: 100

    registry-buffering:
      buffering:
        maxRequestBodyBytes: 0
        maxResponseBodyBytes: 0
        retryExpression: "IsNetworkError() && Attempts() < 2"

    https-redirect:
      redirectScheme:
        scheme: https
        permanent: true

tls:
  options:
    modern:
      minVersion: VersionTLS12
      cipherSuites:
        - TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        - TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        - TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256
        - TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
      curvePreferences:
        - CurveP521
        - CurveP384
      sniStrict: true
```

### C. Equivalent Caddyfile (if Caddy is chosen)

```caddyfile
# === External Services (10.2.x.x) ===

nkontur.com, www.nkontur.com {
    root * /data/webroot/html
    file_server

    handle_path /plex/* {
        reverse_proxy plex:32400 {
            transport http {
                dial_timeout 240s
                read_timeout 240s
                write_timeout 300s
            }
            header_up Upgrade {http.request.header.Upgrade}
            header_up Connection {http.request.header.Connection}
            header_up X-Real-IP {remote_host}
            header_up X-Forwarded-For {remote_host}
            header_up X-Forwarded-Proto {scheme}
        }
    }

    handle_path /plex-requests/* {
        reverse_proxy ombi:3579
    }

    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
        X-Frame-Options SAMEORIGIN
        X-XSS-Protection "1; mode=block"
        Referrer-Policy no-referrer
    }

    rate_limit {remote.ip} 10r/s burst 20

    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

bitwarden.nkontur.com {
    reverse_proxy bitwarden:80
    header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

# === Internal Services (10.3.x.x) ===

grafana.lab.nkontur.com {
    reverse_proxy grafana:3000
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

gitlab.lab.nkontur.com {
    reverse_proxy gitlab:80
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

gitlab-registry.lab.nkontur.com {
    reverse_proxy gitlab:5050 {
        transport http {
            read_timeout 900s
            write_timeout 900s
        }
    }
    request_body {
        max_size 0
    }
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}

paperless-ngx.lab.nkontur.com {
    reverse_proxy paperless-ngx:8000 {
        transport http {
            read_timeout 240s
            write_timeout 240s
        }
    }
    tls {
        dns namesilo {
            api_key {env.NAMESILO_API_KEY}
        }
    }
}
```

---

## Changelog

| Date | Author | Change |
|------|--------|--------|
| 2026-02-05 | Prometheus | Initial draft |
