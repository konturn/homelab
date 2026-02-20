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

resource "vault_ssh_secret_backend_role" "satellite" {
  name                    = "satellite"
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

resource "vault_ssh_secret_backend_role" "zwave" {
  name                    = "zwave"
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

resource "vault_ssh_secret_backend_role" "nkontur_ws" {
  name                    = "nkontur-ws"
  backend                 = vault_mount.ssh.path
  key_type                = "ca"
  default_user            = "nkontur"
  allowed_users           = "nkontur"
  ttl                     = "900"
  max_ttl                 = "1800"
  allow_user_certificates = true
  default_extensions = {
    "permit-pty" = ""
  }
  allowed_extensions = "permit-pty"
}

resource "vault_ssh_secret_backend_role" "konoahko_ws" {
  name                    = "konoahko-ws"
  backend                 = vault_mount.ssh.path
  key_type                = "ca"
  default_user            = "konoahko"
  allowed_users           = "konoahko"
  ttl                     = "900"
  max_ttl                 = "1800"
  allow_user_certificates = true
  default_extensions = {
    "permit-pty" = ""
  }
  allowed_extensions = "permit-pty"
}

resource "vault_ssh_secret_backend_role" "konturn_ws" {
  name                    = "konturn-ws"
  backend                 = vault_mount.ssh.path
  key_type                = "ca"
  default_user            = "konturn"
  allowed_users           = "konturn"
  ttl                     = "900"
  max_ttl                 = "1800"
  allow_user_certificates = true
  default_extensions = {
    "permit-pty" = ""
  }
  allowed_extensions = "permit-pty"
}

resource "vault_ssh_secret_backend_role" "macmini" {
  name                    = "macmini"
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
