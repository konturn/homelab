# =============================================================================
# Policy: ci-deploy
# =============================================================================
# Full read access to all secrets in the homelab KV v2 engine, plus write on
# one path (Z-Wave security keys, see below).
# Used by CI/CD pipelines to pull configuration during deployment.

resource "vault_policy" "ci_deploy" {
  name = "ci-deploy"

  policy = <<-EOT
    # Read all secrets from homelab KV v2
    path "homelab/data/*" {
      capabilities = ["read"]
    }

    # Read metadata for all secrets
    path "homelab/metadata/*" {
      capabilities = ["read", "list"]
    }

    # SSH CA public key (for TrustedUserCAKeys on target hosts)
    path "ssh-client-signer/config/ca" {
      capabilities = ["read"]
    }

    # zwave-js-ui S0/S2 keys, copied from the Z-Wave Pi's store on deploy
    # (ansible/roles/zwave-security-keys). The only path ci-deploy writes.
    # An exact path overrides the homelab/data/* glob, so read is repeated.
    path "homelab/data/docker/zwave-js-ui" {
      capabilities = ["read", "create", "update"]
    }
  EOT
}

# =============================================================================
# Policy: openclaw-ops
# =============================================================================
# Minimal access for openclaw: JIT API key + own config only.
# All service secrets (media, monitoring, IoT, cameras, etc.) are now accessed
# exclusively through the JIT approval service, which provides audit trails,
# scoped tokens with short TTLs, and centralized access control.

resource "vault_policy" "openclaw_ops" {
  name = "openclaw-ops"

  policy = <<-EOT
    # Agent shared secrets (JIT API key for requesting credentials)
    path "homelab/data/agents/*" {
      capabilities = ["read"]
    }

    # Moltbot own config (gitlab_token, etc.)
    path "homelab/data/openclaw/*" {
      capabilities = ["read"]
    }
  EOT
}

# =============================================================================
# Policy: vault-admin
# =============================================================================
# Scoped administrative access for Terraform-driven Vault configuration.
# Used by CI/CD (vault:configure job) to manage auth backends, policies,
# mounts, and secrets engines — without full root access.
#
# Explicitly EXCLUDES dangerous seal/rekey/root-generation operations.

resource "vault_policy" "vault_admin" {
  name = "vault-admin"

  policy = <<-EOT
    # Manage auth backends (JWT, AppRole, etc.)
    path "auth/*" {
      capabilities = ["create", "read", "update", "delete", "list", "sudo"]
    }

    # Manage policies
    path "sys/policies/*" {
      capabilities = ["create", "read", "update", "delete", "list"]
    }
    path "sys/policy/*" {
      capabilities = ["create", "read", "update", "delete", "list"]
    }

    # Manage secret mounts
    path "sys/mounts/*" {
      capabilities = ["create", "read", "update", "delete", "list"]
    }
    path "sys/mounts" {
      capabilities = ["read", "list"]
    }

    # Read health and seal status (monitoring, not control)
    path "sys/health" {
      capabilities = ["read"]
    }
    path "sys/seal-status" {
      capabilities = ["read"]
    }

    # Full access to the homelab secrets engine
    path "homelab/*" {
      capabilities = ["create", "read", "update", "delete", "list"]
    }

    # SSH secrets engine management
    path "ssh-client-signer/*" {
      capabilities = ["create", "read", "update", "delete", "list"]
    }

    # -------------------------------------------------------------------------
    # DENY dangerous operations — defense in depth
    # -------------------------------------------------------------------------
    path "sys/seal" {
      capabilities = ["deny"]
    }
    path "sys/step-down" {
      capabilities = ["deny"]
    }
    path "sys/rekey*" {
      capabilities = ["deny"]
    }
    path "sys/generate-root*" {
      capabilities = ["deny"]
    }
  EOT
}

# =============================================================================
# Policy: vault-read
# =============================================================================
# Read-only access for MR pipeline validation (terraform plan).
# Can read secrets and auth config but cannot modify anything.

resource "vault_policy" "vault_read" {
  name = "vault-read"

  policy = <<-EOT
    # Read auth configuration for plan
    path "auth/*" {
      capabilities = ["read", "list"]
    }

    # Read policies for plan
    path "sys/policies/*" {
      capabilities = ["read", "list"]
    }
    path "sys/policy/*" {
      capabilities = ["read", "list"]
    }

    # Read audit config for plan (requires sudo)
    path "sys/audit" {
      capabilities = ["read", "list", "sudo"]
    }

    # Read mounts for plan
    path "sys/mounts" {
      capabilities = ["read", "list"]
    }
    path "sys/mounts/*" {
      capabilities = ["read"]
    }
    # Auth mount tuning (vault_jwt_auth_backend refresh); Vault 2.2 requires
    # sudo to read it. Read only.
    path "sys/mounts/auth/*" {
      capabilities = ["read", "sudo"]
    }

    # Health check
    path "sys/health" {
      capabilities = ["read"]
    }

    # Allow Terraform provider to create limited child tokens during plan
    # Child tokens inherit parent's (read-only) policies — no privilege escalation
    path "auth/token/create" {
      capabilities = ["create", "update"]
    }

    # Allow looking up own token
    path "auth/token/lookup-self" {
      capabilities = ["read"]
    }

    # SSH secrets engine (read-only for plan)
    path "ssh-client-signer/*" {
      capabilities = ["read", "list"]
    }
  EOT
}

# =============================================================================
# Policy: jit-approval-svc
# =============================================================================
# The JIT approval service brokers credential access on behalf of the agent.
# It authenticates to Vault via AppRole, then mints short-lived child tokens
# with scoped policies (jit-static-<resource>, or a per-request jit-vault-*
# policy) when requests are approved. Creating an orphan token with sudo does
# not require the broker to hold the policies it attaches, so the broker reads
# none of the static services' secrets: only a token carrying
# jit-static-<resource> can.
#
# This policy grants only what the broker itself uses:
# - Token lifecycle management (create, revoke, lookup-self)
# - Read on the admin secrets of the dynamic backends that read Vault at mint
#   time (the GitLab admin token and the broker's own secrets reach it as
#   environment variables at deploy time instead)
# - The per-request jit-vault-* policies and SSH certificate signing

resource "vault_policy" "jit_approval_svc" {
  name = "jit-approval-svc"

  policy = <<-EOT
    # Create orphan tokens with specific policies and TTLs
    path "auth/token/create-orphan" {
      capabilities = ["create", "update", "sudo"]
    }

    # Revoke tokens (for deny/timeout/early-revoke)
    path "auth/token/revoke" {
      capabilities = ["create", "update"]
    }

    # Lookup own token (health checks)
    path "auth/token/lookup-self" {
      capabilities = ["read"]
    }

    # Dynamic backend admin credentials, read at mint time
    # Grafana: jit_admin_token + service_account_id to create SA tokens
    path "homelab/data/docker/grafana" {
      capabilities = ["read"]
    }
    # InfluxDB: admin_token + org_id to create auth tokens
    path "homelab/data/docker/influxdb" {
      capabilities = ["read"]
    }
    # Home Assistant, Paperless: API tokens
    path "homelab/data/docker/homeassistant" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/paperless" {
      capabilities = ["read"]
    }
    # Tailscale: OAuth client creds to mint short-lived API tokens
    path "homelab/data/infrastructure/tailscale" {
      capabilities = ["read"]
    }
    # Gmail: OAuth2 client and refresh token
    path "homelab/data/email/google-oauth" {
      capabilities = ["read"]
    }

    # Dynamic Vault backend: manage temporary JIT policies
    path "sys/policies/acl/jit-vault-*" {
      capabilities = ["create", "read", "update", "delete"]
    }

    # SSH certificate signing for JIT SSH access
    path "ssh-client-signer/sign/claude" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/satellite" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/zwave" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/nkontur-ws" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/konoahko-ws" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/konturn-ws" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/macmini" {
      capabilities = ["create", "update"]
    }
    # Elevated SSH roles
    path "ssh-client-signer/sign/ssh-sign-router-elevated" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/ssh-sign-zwave-elevated" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/ssh-sign-satellite-elevated" {
      capabilities = ["create", "update"]
    }
    path "ssh-client-signer/sign/ssh-sign-macmini-elevated" {
      capabilities = ["create", "update"]
    }
  EOT
}

# =============================================================================
# Policies: jit-static-<resource>
# =============================================================================
# One policy per resource served by the JIT static backend. A token the broker
# mints for a static resource carries only that resource's policy, read-only
# on its own secret. Tiers decide approval and TTL in the broker; they do not
# widen what a token can read. Keep in sync with staticResourcePaths in
# docker/jit-approval-svc/internal/vault/vault.go (a Go test compares them).

locals {
  jit_static_resources = {
    radarr   = ["homelab/data/docker/radarr"]
    sonarr   = ["homelab/data/docker/sonarr"]
    plex     = ["homelab/data/docker/plex"]
    ombi     = ["homelab/data/docker/ombi"]
    nzbget   = ["homelab/data/docker/nzbget"]
    deluge   = ["homelab/data/docker/deluge"]
    prowlarr = ["homelab/data/docker/prowlarr"]
    tautulli = ["homelab/data/docker/tautulli"]
    mqtt     = ["homelab/data/mqtt/mosquitto"]
    pihole   = ["homelab/data/infrastructure/pihole"]
    ipmi     = ["homelab/data/infrastructure/ipmi"]
  }
}

resource "vault_policy" "jit_static" {
  for_each = local.jit_static_resources

  name = "jit-static-${each.key}"

  policy = join("\n", [
    for path in each.value : "path \"${path}\" {\n  capabilities = [\"read\"]\n}\n"
  ])
}

# =============================================================================
# Policy: ha-config-deploy
# =============================================================================
# CI of the private Home Assistant config repo (JWT roles in roles.tf).

resource "vault_policy" "ha_config_deploy" {
  name = "ha-config-deploy"

  policy = <<-EOT
    # Home Assistant API token: reload services, counts, read-only checks
    path "homelab/data/docker/homeassistant" {
      capabilities = ["read"]
    }

    # Project access token of the drift job (opens MRs, cannot push main)
    path "homelab/data/gitlab/ha-config-bot" {
      capabilities = ["read"]
    }

    # 5-minute certificate for the ha-config forced-command user
    path "ssh-client-signer/sign/ha-config-deploy" {
      capabilities = ["update"]
    }
  EOT
}
