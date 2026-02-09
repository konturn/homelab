# =============================================================================
# Vault Audit Logging
# =============================================================================
# Enables file-based audit logging for all Vault operations.
# Every secret read/write is logged with timestamps and accessor info.
# Logs are shipped to Loki via Promtail for centralized observability.

resource "vault_audit" "file" {
  type = "file"

  options = {
    file_path = "/vault/logs/audit.log"
  }
}
