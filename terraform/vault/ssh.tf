# =============================================================================
# SSH Certificate Authority (Vault SSH Secrets Engine)
# =============================================================================
# Configures Vault as an SSH CA for signing short-lived user certificates.
# The 'claude' role issues certificates valid for 15 minutes (T1), used by
# the JIT approval service to grant ephemeral SSH access to target hosts.
# =============================================================================

resource "vault_mount" "ssh" {
  path        = "ssh-client-signer"
  type        = "ssh"
  description = "SSH certificate signing for JIT access"
}

resource "vault_ssh_secret_backend_ca" "ssh_ca" {
  backend              = vault_mount.ssh.path
  generate_signing_key = true
}

resource "vault_ssh_secret_backend_role" "claude" {
  name                    = "claude"
  backend                 = vault_mount.ssh.path
  key_type                = "ca"
  default_user            = "claude"
  allowed_users           = "claude"
  ttl                     = "900"
  max_ttl                 = "1800"
  allow_user_certificates = true
  default_extensions = {
    "permit-pty" = ""
  }
  allowed_extensions = "permit-pty"
}
