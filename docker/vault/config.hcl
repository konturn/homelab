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

# Listener configuration
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1  # TLS handled by nginx proxy
}

# API address for client redirects
api_addr = "http://vault.lab.nkontur.com:8200"

# Disable memory locking warning (we have IPC_LOCK capability)
disable_mlock = false

# UI is enabled for initial setup and debugging
ui = true

# Log level
log_level = "info"
