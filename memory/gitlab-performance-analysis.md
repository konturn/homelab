# GitLab Performance Analysis

**Date:** 2026-02-05
**Instance:** GitLab EE 18.8.3 (Docker container on router.lab.nkontur.com)
**Requested by:** Noah

---

## System Overview

| Resource | Value |
|----------|-------|
| **Host CPUs** | 32 cores (dual CPU, IPMI-managed server) |
| **Host RAM** | 128 GB total |
| **Host RAM Used** | 97.7 GB (72.3%) |
| **Host Swap** | 1 GB total, 99.98% used ⚠️ |
| **Host Disk** | ext4, `/persistent_data` = 983 GB total, 137 GB used |
| **GitLab Container Memory Limit** | 14 GB (docker-compose) — Docker reports 24 GB limit* |
| **GitLab Actual Memory Usage** | 4.9-7.2 GB (avg ~5.6 GB), peaks to 8 GB |
| **GitLab CPU Usage** | 39-55% of allocation (avg ~42%) |
| **Total Containers** | ~42 containers on same host |

*\*Docker reports 25,769,803,776 bytes (~24 GB) as the limit. This discrepancy may be due to Docker engine version behavior or the compose limit not being properly applied. Worth verifying.*

### GitLab Memory Usage (Last 24h)

| Time | Memory (GB) | % of Limit |
|------|-------------|------------|
| Off-peak (3 AM) | 5.0 | ~19.5% |
| Average | 5.6 | ~22% |
| Peak (6 PM) | 7.2 | ~28% |
| Current snapshot | 8.0 | ~31% |

### Host CPU Usage

Average CPU idle: 75-85% → system is using 15-25% CPU on average. Not CPU-constrained.

---

## Current Configuration (gitlab.rb)

```ruby
# Current settings in docker/gitlab/gitlab.rb
external_url 'https://gitlab.lab.nkontur.com'

# Nginx (SSL terminated externally)
nginx['listen_port'] = 80
nginx['listen_https'] = false

# Container Registry - ENABLED
registry_external_url 'https://gitlab-registry.lab.nkontur.com'
gitlab_rails['registry_enabled'] = true
registry['enable'] = true

# Puma
puma['worker_processes'] = 8          # Already reduced from default (~34)
puma['per_worker_max_memory_mb'] = 1200
puma['min_threads'] = 1
puma['max_threads'] = 4

# Sidekiq
sidekiq['concurrency'] = 10           # Already reduced from default (20)

# PostgreSQL
postgresql['shared_buffers'] = "2GB"
postgresql['work_mem'] = "64MB"
postgresql['maintenance_work_mem'] = "256MB"
postgresql['effective_cache_size'] = "4GB"

# Cache
gitlab_rails['rake_cache_clear'] = false
```

### What's NOT configured (defaults in effect):
- All monitoring services: **ENABLED** (Prometheus, exporters, alertmanager)
- GitLab KAS (Kubernetes Agent): **ENABLED**
- GitLab Pages: **ENABLED** (if available)
- Dependency Proxy: **ENABLED**
- Terraform state management: **ENABLED**
- Package registry: **ENABLED**
- jemalloc tuning: **NOT SET**
- Gitaly concurrency limits: **NOT SET**

---

## GitLab Runner Configuration

```toml
# base/gitlab-runner/config.toml
concurrent = 20    # ⚠️ Very high for a shared homelab server

[[runners]]
  name = "main.nkontur.com"
  executor = "docker"
  [runners.docker]
    image = "ubuntu:20.04"
    network_mode = "host"
    volumes = ["/cache/gitlab-runner:/cache"]

[[runners]]
  name = "images"
  executor = "docker"
  [runners.docker]
    image = "docker:24"
    network_mode = "docker_mgmt"
    privileged = false
    volumes = ["/cache/gitlab-runner:/cache"]
```

**Runner is on the same machine**, installed as a system service (not in docker-compose). It spawns Docker containers for each CI job.

---

## Repository Statistics

| Repository | Repo Size | Total Storage |
|-----------|-----------|---------------|
| root/homelab | 5.3 MB | 95.9 MB |
| moltbot/clawd-memory | 12.3 MB | 12.3 MB |
| root/images | 0.0 MB | 2.6 MB |
| gitlab-instance/Monitoring | 0.0 MB | 0.0 MB |

Small repos. Git performance is not a bottleneck.

---

## Critical Finding: Swap Exhaustion ⚠️

The host has only 1 GB of swap, and it's 99.98% full. With 128 GB RAM and 72% usage, the system is under memory pressure. When containers or processes need more memory, the kernel has nowhere to page to. This causes:
- OOM kills
- Performance degradation across ALL containers
- Unpredictable latency spikes

---

## Recommendations (Ranked by Impact)

### 1. 🔴 Reduce Puma Workers from 8 to 4

**Impact: HIGH | Effort: EASY | Risk: LOW**

Each Puma worker is a separate process that forks the entire Rails application. With `per_worker_max_memory_mb = 1200`, 8 workers can consume up to 9.6 GB. For a single-user homelab, 4 workers (or even 2) is more than enough.

**Estimated memory savings: 2-4 GB**

```ruby
# In gitlab.rb
puma['worker_processes'] = 4
puma['per_worker_max_memory_mb'] = 1024
puma['min_threads'] = 1
puma['max_threads'] = 4
```

For absolute minimum (single-user), you could even go to `0` (single-process mode, saves 3-6 GB but reduces concurrency):
```ruby
puma['worker_processes'] = 0  # Single-process mode for minimum memory
```

---

### 2. 🔴 Disable All Built-in Monitoring

**Impact: HIGH | Effort: EASY | Risk: LOW**

GitLab runs Prometheus, node_exporter, postgres_exporter, redis_exporter, alertmanager, and gitlab_exporter by default. You already have external monitoring via Telegraf → InfluxDB → Grafana. The built-in monitoring is pure waste.

**Estimated memory savings: 300-500 MB**

```ruby
# In gitlab.rb
prometheus_monitoring['enable'] = false
prometheus['enable'] = false
alertmanager['enable'] = false
node_exporter['enable'] = false
postgres_exporter['enable'] = false
redis_exporter['enable'] = false
gitlab_exporter['enable'] = false
puma['exporter_enabled'] = false
sidekiq['metrics_enabled'] = false
```

---

### 3. 🔴 Configure jemalloc Memory Decay

**Impact: HIGH | Effort: EASY | Risk: LOW**

By default, jemalloc (GitLab's memory allocator) holds onto freed memory for reuse, causing RSS to grow over time. This is the single most impactful "free" optimization — it makes memory usage much more stable without significant performance cost.

**Estimated improvement: Prevents memory creep, keeps usage 10-20% lower over time**

```ruby
# In gitlab.rb
gitlab_rails['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000'
}

gitaly['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000',
  'GITALY_COMMAND_SPAWN_MAX_PARALLEL' => '2'
}
```

---

### 4. 🟡 Disable Unused GitLab Features

**Impact: MEDIUM | Effort: EASY | Risk: LOW**

Several features run background Sidekiq workers and consume memory even when unused.

```ruby
# In gitlab.rb

# Kubernetes Agent — not needed for homelab
gitlab_kas['enable'] = false

# Disable unused features that run background workers
gitlab_rails['packages_enabled'] = false          # Package registry
gitlab_rails['dependency_proxy_enabled'] = false   # Dependency proxy
gitlab_rails['terraform_state_enabled'] = false    # Terraform state

# GitLab Pages — if not using
gitlab_pages['enable'] = false
pages_nginx['enable'] = false
```

**Estimated memory savings: 100-300 MB**

---

### 5. 🟡 Reduce Runner Concurrency from 20 to 2

**Impact: MEDIUM | Effort: EASY | Risk: LOW**

`concurrent = 20` means up to 20 CI jobs can run simultaneously. Each job spawns a Docker container using ~100 MB RAM. For a homelab with 4 repos, you'll never need 20 concurrent jobs. Even during peak, I observed at most 5 running simultaneously.

```toml
# In base/gitlab-runner/config.toml
concurrent = 2
```

**Estimated memory savings: Variable (0-1.8 GB during CI bursts)**

---

### 6. 🟡 Add Gitaly Concurrency Limits

**Impact: MEDIUM | Effort: EASY | Risk: LOW**

Gitaly handles Git operations. Without limits, it can fork many parallel processes during heavy Git operations (pushes, CI clones).

```ruby
# In gitlab.rb
gitaly['configuration'] = {
  concurrency: [
    {
      'rpc' => "/gitaly.SmartHTTPService/PostReceivePack",
      'max_per_repo' => 3,
    }, {
      'rpc' => "/gitaly.SSHService/SSHUploadPack",
      'max_per_repo' => 3,
    },
  ],
  cgroups: {
    repositories: {
      count: 2,
    },
    mountpoint: '/sys/fs/cgroup',
    hierarchy_root: 'gitaly',
    memory_bytes: 500000000,   # 500 MB
    cpu_shares: 512,
  },
}
```

---

### 7. 🟡 Reduce PostgreSQL Memory Allocation

**Impact: MEDIUM | Effort: EASY | Risk: LOW**

Current `shared_buffers = 2GB` is generous for a homelab. With small repos and few users, 512 MB - 1 GB is sufficient.

```ruby
# In gitlab.rb
postgresql['shared_buffers'] = "1GB"        # Was 2GB
postgresql['work_mem'] = "32MB"             # Was 64MB  
postgresql['maintenance_work_mem'] = "128MB" # Was 256MB
postgresql['effective_cache_size'] = "2GB"   # Was 4GB
```

**Estimated memory savings: 1-1.5 GB**

---

### 8. 🟢 Lower the Container Memory Limit

**Impact: LOW (operational) | Effort: EASY | Risk: MEDIUM**

The docker-compose says 14G but Docker reports 24G. After applying the above optimizations, GitLab should comfortably run within 8 GB. Setting a proper limit prevents runaway memory usage from affecting other containers.

```yaml
# In docker/docker-compose.yml
deploy:
  resources:
    limits:
      memory: 10G    # Was 14G — gives headroom after optimizations
    reservations:
      memory: 4G
```

**Apply this AFTER confirming memory usage drops with the gitlab.rb changes.**

---

### 9. 🟢 Increase Host Swap Space

**Impact: LOW-MEDIUM (stability) | Effort: EASY | Risk: LOW**

1 GB swap on a 128 GB RAM system is dangerously low and currently 100% full. Standard recommendation is 1-2x RAM for servers, but even 8-16 GB of swap would provide a safety net.

```bash
# On the router host
sudo fallocate -l 16G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Also tune swappiness:
```bash
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
sudo sysctl vm.swappiness=10
```

---

### 10. 🟢 Consider GitLab CE Instead of EE

**Impact: LOW-MEDIUM | Effort: HARD | Risk: MEDIUM**

GitLab EE includes many enterprise features that run background processes even when unused. GitLab CE has a smaller footprint. However, migration requires careful planning and you'd lose access to EE-only features.

**Only consider if other optimizations aren't sufficient.**

---

## Complete Recommended gitlab.rb

Here's the full optimized `gitlab.rb` combining all recommendations:

```ruby
# GitLab configuration
# Managed by homelab repo - do not edit directly on server
# OPTIMIZED for homelab single-user performance

external_url 'https://gitlab.lab.nkontur.com'

# Nginx configuration (SSL terminated by external nginx)
nginx['listen_port'] = 80
nginx['listen_https'] = false

###
# Container Registry Configuration
###
registry_external_url 'https://gitlab-registry.lab.nkontur.com'
gitlab_rails['registry_enabled'] = true
gitlab_rails['registry_host'] = "gitlab-registry.lab.nkontur.com"
gitlab_rails['registry_port'] = "443"
gitlab_rails['registry_api_url'] = "http://127.0.0.1:5000"
registry['enable'] = true
registry['registry_http_addr'] = "0.0.0.0:5050"
registry_nginx['enable'] = false
registry['storage'] = {
  'filesystem' => {
    'rootdirectory' => '/var/opt/gitlab/gitlab-rails/shared/registry'
  }
}

###
# Puma (web server) - Reduced for homelab
###
puma['worker_processes'] = 4
puma['per_worker_max_memory_mb'] = 1024
puma['min_threads'] = 1
puma['max_threads'] = 4
puma['exporter_enabled'] = false

###
# Sidekiq (background jobs) - Already optimized
###
sidekiq['concurrency'] = 10
sidekiq['metrics_enabled'] = false

###
# PostgreSQL - Right-sized for small instance
###
postgresql['shared_buffers'] = "1GB"
postgresql['work_mem'] = "32MB"
postgresql['maintenance_work_mem'] = "128MB"
postgresql['effective_cache_size'] = "2GB"

###
# Disable built-in monitoring (using external Telegraf + InfluxDB + Grafana)
###
prometheus_monitoring['enable'] = false
prometheus['enable'] = false
alertmanager['enable'] = false
node_exporter['enable'] = false
postgres_exporter['enable'] = false
redis_exporter['enable'] = false
gitlab_exporter['enable'] = false

###
# Disable unused features
###
gitlab_kas['enable'] = false
gitlab_rails['packages_enabled'] = false
gitlab_rails['dependency_proxy_enabled'] = false
gitlab_rails['terraform_state_enabled'] = false
gitlab_pages['enable'] = false
pages_nginx['enable'] = false

###
# Gitaly - Limit concurrency
###
gitaly['configuration'] = {
  concurrency: [
    {
      'rpc' => "/gitaly.SmartHTTPService/PostReceivePack",
      'max_per_repo' => 3,
    }, {
      'rpc' => "/gitaly.SSHService/SSHUploadPack",
      'max_per_repo' => 3,
    },
  ],
  cgroups: {
    repositories: {
      count: 2,
    },
    mountpoint: '/sys/fs/cgroup',
    hierarchy_root: 'gitaly',
    memory_bytes: 500000000,
    cpu_shares: 512,
  },
}

###
# Memory management - Aggressive jemalloc cleanup
###
gitlab_rails['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000'
}

gitaly['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000',
  'GITALY_COMMAND_SPAWN_MAX_PARALLEL' => '2'
}

# Preserve caches on restart
gitlab_rails['rake_cache_clear'] = false
```

---

## Expected Results

| Optimization | Memory Saved | Cumulative |
|-------------|-------------|------------|
| Reduce Puma 8→4 workers | ~2-4 GB | ~2-4 GB |
| Disable monitoring | ~300-500 MB | ~2.5-4.5 GB |
| jemalloc tuning | Prevents creep | More stable |
| Disable unused features | ~100-300 MB | ~2.7-4.8 GB |
| Reduce PostgreSQL buffers | ~1-1.5 GB | ~3.7-6.3 GB |
| **Total estimated savings** | | **~3.7-6.3 GB** |

Current usage: 5-8 GB → Expected after optimization: **3-5 GB**

This should reduce the container's memory footprint by roughly 40-50%, freeing 3-6 GB for other containers on the host and significantly reducing swap pressure.

---

## Implementation Order

1. **Phase 1 (Low risk, high impact):** jemalloc tuning + disable monitoring + disable unused features
2. **Phase 2 (Medium risk):** Reduce Puma workers + reduce PostgreSQL buffers
3. **Phase 3 (After validation):** Reduce container memory limit + increase host swap
4. **Phase 4 (If needed):** Runner concurrency + Gitaly limits

**Each phase should be followed by 24h of monitoring via Grafana/InfluxDB to verify stability.**

---

## Monitoring After Changes

Watch these metrics in InfluxDB/Grafana:
```flux
// GitLab memory usage trend
from(bucket: "metrics")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "docker_container_mem")
  |> filter(fn: (r) => r.container_name == "gitlab")
  |> filter(fn: (r) => r._field == "usage")
  |> aggregateWindow(every: 1h, fn: mean)
```

Also monitor: host swap usage, GitLab response times (HTTP health check latency), CI pipeline duration.

---

## UI Page Load & API Response Time Analysis

**Date:** 2026-02-05 (Part 2 — complementing the memory analysis above)
**Focus:** Making GitLab pages load faster and API calls respond faster

---

### Current Performance Baseline

#### API Response Times (measured from moltbot container on same Docker network)

| Endpoint | Total Time | TTFB | Size | Rating |
|----------|-----------|------|------|--------|
| `GET /version` | 0.09-0.12s | 0.09s | 258 B | ✅ Good |
| `GET /projects/4` | 0.39s | 0.39s | 5.2 KB | ⚠️ Acceptable |
| `GET /projects/4/pipelines?per_page=5` | 0.14s | 0.14s | 1.7 KB | ✅ Good |
| `GET /projects` (all) | 1.61s | 1.61s | 20 KB | 🔴 Slow |
| `GET /projects/4/merge_requests?per_page=5` | 0.70s | 0.70s | 16.5 KB | ⚠️ Slow |
| `GET /projects/4/merge_requests?per_page=100` | 1.24s | 1.24s | 327 KB | 🔴 Slow |
| `GET /projects/4/repository/branches` | 0.28s | 0.28s | 26 KB | ⚠️ Acceptable |
| `GET /projects/4/merge_requests?state=opened` | 0.27s | 0.27s | 12 KB | ✅ OK |

**Key observations:**
- DNS resolution: ~1.8ms (negligible — Docker internal DNS)
- TLS handshake: ~43ms (TLS 1.3, reasonable)
- Network overhead: <2ms (same Docker network)
- **95%+ of response time is server-side processing (TTFB ≈ Total)**
- Connection reuse cuts ~50ms per request (TLS saved): 0.12s → 0.07s for /version
- MR endpoints are the slowest — likely due to database JOINs and serialization

#### UI Page Load Times (server-side HTML generation)

| Page | TTFB | Size | Notes |
|------|------|------|-------|
| Sign-in page | 0.17s | 12.8 KB | Fast — simple page |
| Dashboard (/) | 0.34s | 15.7 KB | Redirects, then renders shell |
| Project page | 0.62s | 15.7 KB | Heavier — tree view, README |
| MR list | 0.28s | 15.7 KB | OK |
| Pipeline list | 0.32s | 15.7 KB | OK |
| MR detail (#107) | 0.18s | 12.8 KB | Surprisingly fast (SPA shell) |

**Critical insight:** All authenticated pages return ~12.8-15.7 KB. GitLab uses a SPA (Single Page Application) architecture — the server returns a small HTML shell, then JavaScript loads the actual content. The **perceived slowness** comes from:
1. Server-side TTFB (0.2-0.6s)
2. JavaScript bundle download (multiple chunks)
3. Client-side API calls to populate the page

---

### Infrastructure Analysis

#### Request Path (4 hops)
```
Browser → External nginx (TLS termination) → GitLab nginx (HTTP) → Workhorse → Puma (Rails)
```

#### What's Working Well ✅
- **TLS:** TLS 1.3 with session tickets, OCSP stapling — good config
- **SSL session cache:** 10m shared cache with 10m timeout — properly configured
- **Gzip compression:** Working on both API responses and static assets
  - API: 20KB → 3KB (85% compression)
  - Static assets served with `Content-Encoding: gzip`
- **Static asset caching:** `Cache-Control: public`, `Expires: +1 year` — excellent
- **Keepalive:** Configured at both nginx layers (21600s external, 70s SSL config)
- **DNS:** <2ms resolution (Docker internal DNS)
- **Sendfile:** Enabled for static file serving
- **Asset fingerprinting:** Webpack chunks use content hashes — proper cache busting

#### What's Missing/Broken 🔴

##### 1. No HTTP/2 on Internal Nginx
The external nginx proxies to GitLab over **HTTP/1.1**. The internal listen port is `443` (confusingly — it's `443:80` in Docker, meaning external 443 maps to container 80). But critically:
- External nginx serves **HTTP/1.1** to clients (no `http2` directive on the GitLab server block)
- Only external-facing services (nkontur.com domain) have HTTP/2 enabled
- Internal services (lab.nkontur.com) all use HTTP/1.1

**Impact:** HTTP/2 multiplexing would allow parallel loading of ~20 JS/CSS chunks in a single connection. Currently they serialize or require multiple TCP connections. This is a **major** factor for UI page load.

##### 2. No Gzip at External Nginx Level
The external nginx has no `gzip` directive. Gzip works because GitLab's **internal** nginx compresses responses. But:
- API responses to external clients may not be gzipped if GitLab Workhorse returns them directly
- The compression is handled per-request inside the container rather than at the proxy layer

##### 3. No Proxy Buffering for GitLab
The default proxy template has no `proxy_buffering` directive (defaults to `on` which is OK), but also:
- No `proxy_buffers` sizing — default is `8 4k` or `8 8k`
- No `proxy_busy_buffers_size`
- For large API responses (327KB for all MRs), inadequate buffering causes chunked reads

##### 4. No Brotli Compression
Static assets are served gzipped but not Brotli-compressed. Brotli provides 15-25% better compression than gzip for text assets. The 2.2MB JS chunk would benefit significantly.
- Current: `Content-Length: 2205899` (2.2MB raw for main.js chunk with only `br` accept-encoding → no compression)
- Brotli would reduce this to ~400-500KB

##### 5. Redis Not Explicitly Tuned
The current `gitlab.rb` has no Redis configuration. Defaults are:
- Single Redis instance handling cache, sessions, queues, and shared state
- No `maxmemory` limit
- No LRU eviction policy
- No separate cache instance

For a small deployment this is fine, but setting `maxmemory` and `allkeys-lru` for the cache would prevent Redis from consuming unlimited memory.

##### 6. No PgBouncer (Connection Pooling)
PostgreSQL connections are direct. Each Puma worker + thread can hold a DB connection. With 4 workers × 4 threads = 16 potential connections + Sidekiq (10) + Gitaly + internal = ~30+ connections. PgBouncer isn't necessary at this scale but would reduce connection overhead.

---

### Specific Recommendations for Speed

#### 🔴 HIGH IMPACT — Quick Wins

##### A. Enable HTTP/2 on External Nginx for GitLab (est. 30-50% UI load improvement)

The biggest single improvement for UI page loads. HTTP/2 multiplexing lets the browser fetch all JS/CSS chunks in parallel over a single connection.

**Implementation:** Add a GitLab-specific drop-in to `http-internal-drop-in.conf`:

```json
{
    "directive": "server",
    "args": [],
    "block": [
        {"directive": "listen", "args": ["443", "ssl", "http2"]},
        {"directive": "server_name", "args": ["gitlab.lab.nkontur.com"]},
        {"directive": "include", "args": ["ssl_config"]},
        {"directive": "client_max_body_size", "args": ["0"]},
        {"directive": "gzip", "args": ["on"]},
        {"directive": "gzip_comp_level", "args": ["5"]},
        {"directive": "gzip_min_length", "args": ["256"]},
        {"directive": "gzip_proxied", "args": ["any"]},
        {"directive": "gzip_types", "args": [
            "text/plain", "text/css", "application/json",
            "application/javascript", "text/xml", "application/xml",
            "image/svg+xml"
        ]},
        {
            "directive": "location",
            "args": ["/"],
            "block": [
                {"directive": "resolver", "args": ["127.0.0.11"]},
                {"directive": "set", "args": ["$backend", "http://gitlab:80"]},
                {"directive": "proxy_pass", "args": ["$backend"]},
                {"directive": "proxy_http_version", "args": ["1.1"]},
                {"directive": "proxy_set_header", "args": ["Upgrade", "$http_upgrade"]},
                {"directive": "proxy_set_header", "args": ["Connection", "$connection_upgrade"]},
                {"directive": "proxy_set_header", "args": ["Host", "$host"]},
                {"directive": "proxy_set_header", "args": ["X-Real-IP", "$remote_addr"]},
                {"directive": "proxy_set_header", "args": ["X-Forwarded-For", "$proxy_add_x_forwarded_for"]},
                {"directive": "proxy_set_header", "args": ["X-Forwarded-Host", "$server_name"]},
                {"directive": "proxy_set_header", "args": ["X-Forwarded-Proto", "https"]},
                {"directive": "proxy_set_header", "args": ["X-Forwarded-Ssl", "on"]},
                {"directive": "proxy_send_timeout", "args": ["300"]},
                {"directive": "proxy_read_timeout", "args": ["300"]},
                {"directive": "proxy_buffers", "args": ["8", "32k"]},
                {"directive": "proxy_buffer_size", "args": ["32k"]},
                {"directive": "proxy_busy_buffers_size", "args": ["64k"]}
            ]
        }
    ]
}
```

**Effort:** Medium (modify drop-in config, redeploy nginx)
**Risk:** Low (only affects GitLab routing)

##### B. Configure Redis Cache with LRU Eviction

```ruby
# In gitlab.rb
redis['maxmemory'] = "512mb"
redis['maxmemory_policy'] = "allkeys-lru"
redis['maxmemory_samples'] = 5

# TCP keepalive for Redis connections
redis['tcp_keepalive'] = "300"
```

This ensures Redis actively evicts stale cache entries rather than growing unbounded, and keeps the cache "warm" with frequently accessed data.

**Effort:** Easy (gitlab.rb change)
**Risk:** Low

##### C. Enable GitLab Performance Bar for Diagnostics

```ruby
# In gitlab.rb — enable for admin users
gitlab_rails['monitoring_whitelist'] = ['127.0.0.0/8', '10.0.0.0/8']
```

Then in GitLab Admin → Settings → Metrics → Performance bar, enable it. This adds a toolbar showing:
- SQL query count and time
- Redis calls
- Gitaly calls
- View rendering time

This will give Noah real-time visibility into what's slow on each page.

**Effort:** Easy
**Risk:** None

##### D. Optimize Puma for Throughput (Increase Threads, Decrease Workers)

Current: 8 workers × 4 threads = 32 max concurrent requests
Better for speed: 4 workers × 8 threads = 32 max concurrent requests (same capacity, less memory)

```ruby
puma['worker_processes'] = 4
puma['min_threads'] = 4  # Was 1 — pre-warm threads
puma['max_threads'] = 8  # Was 4 — more concurrent requests per worker
puma['per_worker_max_memory_mb'] = 1200
```

**Why this helps:** Each worker has its own memory space. Threads within a worker share memory. More threads per worker = same concurrency with less total RAM = more RAM for OS page cache = faster disk I/O for everything.

Also, `min_threads = 4` (up from 1) means threads are pre-warmed. With min_threads=1, the first requests after idle must spin up threads, adding latency.

**Effort:** Easy
**Risk:** Low (Ruby GVL limits true parallelism per worker, but I/O-bound Rails requests benefit from threading)

#### ⚠️ MEDIUM IMPACT

##### E. Add `proxy_cache` for Static Assets at External Nginx

GitLab's static assets (JS, CSS, fonts, images) are already cached in the browser for 1 year. But adding nginx proxy_cache means repeat visitors don't even need to hit the GitLab container for static files:

```nginx
# In http-master-template.conf (inside http block)
proxy_cache_path /tmp/nginx_cache levels=1:2 keys_zone=gitlab_assets:10m max_size=500m inactive=60m;
```

And in the GitLab server block:
```nginx
location ~* \.(js|css|png|jpg|gif|ico|svg|woff|woff2|ttf|eot)$ {
    proxy_cache gitlab_assets;
    proxy_cache_valid 200 60m;
    proxy_cache_use_stale error timeout http_500 http_502 http_503 http_504;
    proxy_pass $backend;
}
```

**Effort:** Medium
**Risk:** Low

##### F. PostgreSQL Slow Query Logging

Enable slow query logging to identify database bottlenecks:

```ruby
# In gitlab.rb
postgresql['log_min_duration_statement'] = 200  # Log queries taking >200ms
postgresql['log_statement'] = 'none'  # Don't log all queries, just slow ones
postgresql['log_temp_files'] = 0  # Log any temp file usage (indicates work_mem too low)
```

After enabling, check `/var/log/gitlab/postgresql/current` for slow queries. The MR list endpoint (0.7s) likely has slow JOINs worth investigating.

**Effort:** Easy
**Risk:** None (just logging)

##### G. Enable Workhorse API Response Caching

GitLab Workhorse can cache certain responses. By default it has a short-lived cache for project metadata:

```ruby
# In gitlab.rb
gitlab_workhorse['api_ci_long_polling_duration'] = "60s"
```

This reduces the frequency of CI status polling requests hitting Puma.

**Effort:** Easy
**Risk:** Low

#### 🟢 LOWER IMPACT / LONGER TERM

##### H. PgBouncer for Connection Pooling

Not strictly necessary at this scale, but reduces PostgreSQL connection overhead:

```ruby
# In gitlab.rb
pgbouncer['enable'] = true
pgbouncer['databases'] = {
  gitlabhq_production: {
    host: '127.0.0.1',
    user: 'pgbouncer',
    password: 'generate-a-password',
  }
}
```

**Effort:** High (needs password setup, testing)
**Risk:** Medium (can break DB connections if misconfigured)

##### I. Repository Housekeeping

Ensure git repos are well-packed. For the homelab repo (95.9 MB total with only 5.3 MB of actual repo data), there may be bloat:

```ruby
# In gitlab.rb
gitlab_rails['housekeeping_enabled'] = true
gitlab_rails['housekeeping_full_repack_period'] = 50
gitlab_rails['housekeeping_gc_period'] = 200
gitlab_rails['housekeeping_incremental_repack_period'] = 10
```

**Effort:** Easy
**Risk:** Low (runs during off-peak)

---

### Response Time Breakdown & Bottleneck Analysis

```
Typical API request: 0.4s total
├── DNS:        0.002s  (0.5%)
├── TCP:        0.001s  (0.3%)
├── TLS:        0.043s  (10.8%)  ← Unavoidable, already TLS 1.3
├── Server:     0.350s  (87.5%)  ← THE BOTTLENECK
│   ├── nginx proxy:    ~0.001s
│   ├── Workhorse:      ~0.005s
│   ├── Puma/Rails:     ~0.300s  ← Application processing
│   │   ├── DB queries: ~0.150s  (estimated)
│   │   ├── Redis:      ~0.020s  (estimated)
│   │   └── Serialize:  ~0.130s  (estimated)
│   └── Response xfer:  ~0.044s
└── Transfer:   0.004s  (1.0%)
```

**87.5% of time is in server processing.** The primary levers are:
1. **Reduce server processing time** → Puma threading, Redis caching, DB optimization
2. **Reduce TLS overhead** → Already TLS 1.3 (good), HTTP/2 reduces per-request overhead
3. **Reduce transfer time** → Already gzipped (good)

For UI specifically, the additional bottleneck is **client-side JavaScript loading**, which depends on:
1. Number of parallel connections (HTTP/2 fixes this)
2. Total JS bundle size (2.2MB main chunk is heavy — can't change this without patching GitLab)
3. Browser caching (already 1-year cache headers ✅)

---

### Priority-Ranked Action Items

| # | Action | Impact | Effort | Risk | Category |
|---|--------|--------|--------|------|----------|
| 1 | Enable HTTP/2 for GitLab in nginx | 🔴 High | Medium | Low | Network |
| 2 | Add gzip at external nginx level | 🔴 High | Easy | Low | Network |
| 3 | Tune Puma: 4 workers, 8 threads, min_threads=4 | 🔴 High | Easy | Low | Application |
| 4 | Configure Redis maxmemory + LRU | ⚠️ Medium | Easy | Low | Caching |
| 5 | Enable Performance Bar | ⚠️ Medium | Easy | None | Diagnostics |
| 6 | Enable PostgreSQL slow query logging | ⚠️ Medium | Easy | None | Diagnostics |
| 7 | Increase proxy_buffers for GitLab | ⚠️ Medium | Easy | Low | Network |
| 8 | Add nginx proxy_cache for static assets | 🟢 Low | Medium | Low | Caching |
| 9 | Workhorse CI polling optimization | 🟢 Low | Easy | Low | Application |
| 10 | Repository housekeeping config | 🟢 Low | Easy | Low | Maintenance |
| 11 | PgBouncer | 🟢 Low | High | Medium | Database |

---

### Recommended gitlab.rb Additions (UI/API Speed Focus)

Append these to the optimized gitlab.rb from the memory analysis above:

```ruby
###
# Redis — Cache tuning
###
redis['maxmemory'] = "512mb"
redis['maxmemory_policy'] = "allkeys-lru"
redis['maxmemory_samples'] = 5
redis['tcp_keepalive'] = "300"

###
# PostgreSQL — Diagnostics
###
postgresql['log_min_duration_statement'] = 200
postgresql['log_temp_files'] = 0

###
# Workhorse — API optimization  
###
gitlab_workhorse['api_ci_long_polling_duration'] = "60s"

###
# Performance monitoring for admins
###
gitlab_rails['monitoring_whitelist'] = ['127.0.0.0/8', '10.0.0.0/8']

###
# Housekeeping
###
gitlab_rails['housekeeping_enabled'] = true
gitlab_rails['housekeeping_full_repack_period'] = 50
gitlab_rails['housekeeping_gc_period'] = 200
gitlab_rails['housekeeping_incremental_repack_period'] = 10
```

---

### Summary: Expected Speed Improvements

| Change | Estimated Improvement |
|--------|----------------------|
| HTTP/2 + gzip at nginx | UI page load: -30-50% (parallel asset loading) |
| Puma 4w/8t + min_threads=4 | API TTFB: -10-20% (better concurrency, pre-warmed threads) |
| Redis LRU + maxmemory | Repeat requests: -5-15% (warmer cache) |
| proxy_buffers increase | Large API responses: -5-10% (reduced chunking) |
| **Combined** | **UI: 30-50% faster, API: 15-30% faster** |

