# =============================================================================
# JWT Role: ci-deploy
# =============================================================================
# Bound to the homelab project (ID 4), main branch only.
# CI jobs authenticate with $CI_JOB_JWT and receive a 5-minute token.

resource "vault_jwt_auth_backend_role" "ci_deploy" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "ci-deploy"
  role_type = "jwt"

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
