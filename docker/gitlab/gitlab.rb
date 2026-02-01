# GitLab configuration
# Managed by homelab repo - do not edit directly on server

external_url 'https://gitlab.lab.nkontur.com'

# Nginx configuration (SSL terminated by external nginx)
nginx['listen_port'] = 80
nginx['listen_https'] = false

# Puma (web server) configuration
# Limit workers to prevent memory exhaustion on 32-core system
# Default is CPU cores + 2, which causes ~46GB memory usage
puma['worker_processes'] = 8
puma['per_worker_max_memory_mb'] = 1200
puma['min_threads'] = 1
puma['max_threads'] = 4

# Sidekiq (background jobs) configuration
# Reduce concurrency to limit memory usage
sidekiq['concurrency'] = 10

# PostgreSQL tuning
postgresql['shared_buffers'] = "2GB"
postgresql['work_mem'] = "64MB"
postgresql['maintenance_work_mem'] = "256MB"
postgresql['effective_cache_size'] = "4GB"

# Preserve caches on restart to avoid cold-start performance issues
gitlab_rails['rake_cache_clear'] = false
