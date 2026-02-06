# GitLab configuration
# Managed by homelab repo - do not edit directly on server

external_url 'https://gitlab.lab.nkontur.com'

# Nginx configuration (SSL terminated by external nginx)
nginx['listen_port'] = 80
nginx['listen_https'] = false

###
# Container Registry Configuration
# Enables Docker container registry at gitlab-registry.lab.nkontur.com
# SSL is terminated by external nginx proxy
###
registry_external_url 'https://gitlab-registry.lab.nkontur.com'
gitlab_rails['registry_enabled'] = true
gitlab_rails['registry_host'] = "gitlab-registry.lab.nkontur.com"
gitlab_rails['registry_port'] = "443"
gitlab_rails['registry_api_url'] = "http://127.0.0.1:5000"

# Registry runs on port 5050, exposed to nginx proxy
registry['enable'] = true
registry['registry_http_addr'] = "0.0.0.0:5050"

# Disable registry's built-in nginx (using external nginx)
registry_nginx['enable'] = false

# Store images in GitLab data directory
registry['storage'] = {
  'filesystem' => {
    'rootdirectory' => '/var/opt/gitlab/gitlab-rails/shared/registry'
  }
}

###
# Puma (web server) - Tuned for homelab single-user
# 4 workers x 8 threads = 32 max concurrent requests (same as before, less memory)
# min_threads=4 pre-warms threads to avoid cold-start latency
###
puma['worker_processes'] = 4
puma['per_worker_max_memory_mb'] = 1200
puma['min_threads'] = 4
puma['max_threads'] = 8

###
# Sidekiq (background jobs) - Reduced concurrency
###
sidekiq['concurrency'] = 5

###
# PostgreSQL - Right-sized for small instance
###
postgresql['shared_buffers'] = "1GB"
postgresql['work_mem'] = "32MB"
postgresql['maintenance_work_mem'] = "256MB"
postgresql['effective_cache_size'] = "4GB"

# Slow query logging (>500ms) for diagnostics
postgresql['log_min_duration_statement'] = 500

###
# Redis
# Note: maxmemory/maxmemory_policy are not valid Omnibus keys.
# Redis memory config requires a custom redis config file if needed.
###

###
# Disable built-in monitoring (using external Telegraf + InfluxDB + Grafana)
###
prometheus_monitoring['enable'] = false
alertmanager['enable'] = false
node_exporter['enable'] = false
redis_exporter['enable'] = false
postgres_exporter['enable'] = false
gitlab_exporter['enable'] = false

###
# Disable unused features to reduce memory and background worker load
###
gitlab_kas['enable'] = false
gitlab_pages['enable'] = false
mattermost['enable'] = false

###
# Memory management - jemalloc tuning to prevent RSS creep
###
gitlab_rails['env'] = {
  'MALLOC_CONF' => 'dirty_decay_ms:1000,muzzy_decay_ms:1000'
}

# Preserve caches on restart to avoid cold-start performance issues
gitlab_rails['rake_cache_clear'] = false
