# =============================================================================
# JWT Role: ci-deploy
# =============================================================================
# Bound to the homelab project (ID 4), protected main branch only.
# CI jobs authenticate with $CI_JOB_JWT and receive a 5-minute token.

resource "vault_jwt_auth_backend_role" "ci_deploy" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "ci-deploy"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id    = "4"
    ref           = "main"
    ref_type      = "branch"
    ref_protected = "true"
  }

  user_claim      = "user_email"
  token_policies  = ["ci-deploy"]
  token_ttl       = 300
  token_max_ttl   = 300
}

# =============================================================================
# AppRole Role: openclaw
# =============================================================================
# Long-lived role for openclaw service authentication.
# secret_id_ttl = 0 means secret IDs don't expire (manual rotation).

resource "vault_approle_auth_backend_role" "openclaw" {
  backend        = vault_auth_backend.approle.path
  role_name      = "openclaw"
  token_policies = ["openclaw-ops"]
  token_ttl      = 3600
  token_max_ttl  = 86400

  secret_id_ttl          = 0
  token_num_uses         = 0
  secret_id_num_uses     = 0
}

# =============================================================================
# AppRole Role: jit-approval-svc
# =============================================================================
# AppRole for the JIT approval service. The service brokers credential access
# on behalf of the agent — it authenticates to Vault, then mints short-lived
# scoped tokens (using jit-static-<resource> policies) when requests are approved.
#
# secret_id_ttl = 0 means secret IDs don't expire (manual rotation).
# token_num_uses = 0 means unlimited token uses (long-running service).

resource "vault_approle_auth_backend_role" "jit_approval_svc" {
  backend        = vault_auth_backend.approle.path
  role_name      = "jit-approval-svc"
  token_policies = ["jit-approval-svc"]
  token_ttl      = 3600
  token_max_ttl  = 86400

  secret_id_ttl          = 0
  token_num_uses         = 0
  secret_id_num_uses     = 0
}

# =============================================================================
# AppRole Role: jit-proxy
# =============================================================================
# The JIT credential proxy (policy jit-proxy: read on the proxied services'
# secrets only). role_id, secret_id and the control-plane key live in Vault KV
# (homelab/docker/jit-proxy); vault:rotate-approles rotates the AppRole. Logins
# and tokens are bound to the proxy's fixed address on the internal network
# (jit_proxy_internal_ip in ansible/inventory.yml).

resource "vault_approle_auth_backend_role" "jit_proxy" {
  backend        = vault_auth_backend.approle.path
  role_name      = "jit-proxy"
  token_policies = ["jit-proxy"]
  token_ttl      = 3600
  token_max_ttl  = 86400

  secret_id_bound_cidrs = ["10.3.32.17/32"]
  token_bound_cidrs     = ["10.3.32.17/32"]

  secret_id_ttl      = 0
  token_num_uses     = 0
  secret_id_num_uses = 0
}

# =============================================================================
# AppRole Role: router-helpers
# =============================================================================
# Root's helpers on the router (policy router-helpers). role_id and secret_id
# live in Vault KV (homelab/infrastructure/router-helpers), the router deploy
# writes them to a root-only file, and vault:rotate-approles rotates them.
# Logins and tokens are bound to the router's address as Vault sees it.

resource "vault_approle_auth_backend_role" "router_helpers" {
  backend        = vault_auth_backend.approle.path
  role_name      = "router-helpers"
  token_policies = ["router-helpers"]
  token_ttl      = 300
  token_max_ttl  = 900

  secret_id_bound_cidrs = ["10.3.0.2/32"]
  token_bound_cidrs     = ["10.3.0.2/32"]

  secret_id_ttl      = 0
  token_num_uses     = 0
  secret_id_num_uses = 0
}

# =============================================================================
# JWT Role: vault-admin
# =============================================================================
# Scoped admin role for the vault:configure CI job. Grants the vault-admin
# policy so Terraform can manage auth backends, policies, and mounts.
#
# Tightly bound: only project 4 (homelab), only the protected main branch.
# 15-minute TTL — enough for a terraform plan+apply cycle.

resource "vault_jwt_auth_backend_role" "vault_admin" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "vault-admin"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id    = "4"
    ref           = "main"
    ref_type      = "branch"
    ref_protected = "true"
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

# =============================================================================
# JWT Role: ha-config-deploy
# =============================================================================
# CI of the private Home Assistant config repo (root/homeassistant, project
# ID 14). See docs/HOMEASSISTANT.md.
#
# ha-config-deploy: protected main only. MR and branch pipelines there get
# no Vault role. Reads the Home Assistant API token, signs a 5-minute SSH
# certificate for the ha-config forced-command user and reads the GitLab
# token the drift job uses to open MRs in that project.

resource "vault_jwt_auth_backend_role" "ha_config_deploy" {
  backend   = vault_jwt_auth_backend.gitlab.path
  role_name = "ha-config-deploy"
  role_type = "jwt"

  bound_audiences = ["https://vault.lab.nkontur.com:8200"]

  bound_claims = {
    project_id    = "14"
    ref           = "main"
    ref_type      = "branch"
    ref_protected = "true"
  }

  user_claim     = "user_email"
  token_policies = ["ha-config-deploy"]
  token_ttl      = 600
  token_max_ttl  = 600
}
