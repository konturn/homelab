# =============================================================================
# Outputs
# =============================================================================
# Useful references after apply. Sensitive values are marked accordingly.

output "jwt_auth_path" {
  description = "Path of the JWT auth backend"
  value       = vault_jwt_auth_backend.gitlab.path
}

output "approle_auth_path" {
  description = "Path of the AppRole auth backend"
  value       = vault_auth_backend.approle.path
}

output "moltbot_role_id" {
  description = "Role ID for the moltbot AppRole"
  value       = vault_approle_auth_backend_role.moltbot.role_id
  sensitive   = true
}
