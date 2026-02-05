# HashiCorp Vault Configuration
# Initial setup - file storage backend for simplicity
# 
# Future MRs will add:
# - GitLab JWT auth method
# - AppRole for CI/CD pipelines
# - Secret migration from environment variables

# File storage backend
# Data stored in /vault/file (standard Vault container path, chowned by entrypoint)
storage "file" {
  path = "/vault/file"
}

# Listener configuration — TLS enabled using wildcard cert from Let's Encrypt
# Certs managed by networking/ssl/renew.sh (certbot dns-cloudflare)
# Mounted from {{ docker_persistent_data_path }}/certs:/vault/certs:ro
listener "tcp" {
  address       = "0.0.0.0:8200"
  tls_cert_file = "/vault/certs/nkontur.com/live/iot.lab.nkontur.com-0003/fullchain.pem"
  tls_key_file  = "/vault/certs/nkontur.com/live/iot.lab.nkontur.com-0003/privkey.pem"
  tls_disable   = 0
}

# API address for client redirects
api_addr = "https://vault.lab.nkontur.com:8200"

# Disable memory locking warning (we have IPC_LOCK capability)
disable_mlock = false

# UI is enabled for initial setup and debugging
ui = true

# Log level
log_level = "info"
