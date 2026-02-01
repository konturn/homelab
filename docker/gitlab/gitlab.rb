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
