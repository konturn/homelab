# =============================================================================
# JWT Role: ci-deploy
# =============================================================================
# Bound to the homelab project (ID 4), main branch only.
# CI jobs authenticate with $CI_JOB_JWT and receive a 5-minute token.

resource "vault_jwt_auth_backend_role" "ci_deploy" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "ci-deploy"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id = "4"
    ref        = "main"
    ref_type   = "branch"
  }

  user_claim      = "user_email"
  token_policies  = ["ci-deploy"]
  token_ttl       = 300
  token_max_ttl   = 300
}

# =============================================================================
# AppRole Role: moltbot
# =============================================================================
# Long-lived role for moltbot service authentication.
# secret_id_ttl = 0 means secret IDs don't expire (manual rotation).

resource "vault_approle_auth_backend_role" "moltbot" {
  backend        = vault_auth_backend.approle.path
  role_name      = "moltbot"
  token_policies = ["moltbot-ops"]
  token_ttl      = 3600
  token_max_ttl  = 86400

  secret_id_ttl          = 0
  token_num_uses         = 0
  secret_id_num_uses     = 0
}

# =============================================================================
# JWT Role: vault-admin
# =============================================================================
# Scoped admin role for the vault:configure CI job. Grants the vault-admin
# policy so Terraform can manage auth backends, policies, and mounts.
#
# Tightly bound: only project 4 (homelab), only the main branch.
# 15-minute TTL — enough for a terraform plan+apply cycle.

resource "vault_jwt_auth_backend_role" "vault_admin" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "vault-admin"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id = "4"
    ref        = "main"
    ref_type   = "branch"
  }

  user_claim     = "user_email"
  token_policies = ["vault-admin"]
  token_ttl      = 900
  token_max_ttl  = 900
}

# =============================================================================
# JWT Role: vault-read
# =============================================================================
# Read-only role for MR pipelines (terraform plan/validate).
# Bound to project 4 but ANY branch — MR pipelines need this.

resource "vault_jwt_auth_backend_role" "vault_read" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "vault-read"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id = "4"
  }

  user_claim     = "user_email"
  token_policies = ["vault-read"]
  token_ttl      = 300
  token_max_ttl  = 300
}
