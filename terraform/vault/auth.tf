# =============================================================================
# JWT Auth Backend - GitLab CI
# =============================================================================
# Allows CI jobs to authenticate using GitLab-issued JWT tokens.
# GitLab CI sets $CI_JOB_JWT automatically in every pipeline job.

resource "vault_jwt_auth_backend" "gitlab" {
  description        = "GitLab CI JWT authentication"
  path               = "jwt"
  oidc_discovery_url = "https://gitlab.lab.nkontur.com"
  bound_issuer       = "https://gitlab.lab.nkontur.com"
}

# =============================================================================
# AppRole Auth Backend - Moltbot
# =============================================================================
# AppRole for service-to-service authentication.
# Moltbot uses role_id + secret_id to obtain short-lived tokens.

resource "vault_auth_backend" "approle" {
  type        = "approle"
  description = "AppRole authentication for services"
}
