# =============================================================================
# Policy: ci-deploy
# =============================================================================
# Full read access to all secrets in the homelab KV v2 engine.
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
  EOT
}

# =============================================================================
# Policy: moltbot-ops
# =============================================================================
# Scoped read access to operational service secrets only.
# Intentionally excludes infrastructure, networking, backup, SSH keys, LUKS.

resource "vault_policy" "moltbot_ops" {
  name = "moltbot-ops"

  policy = <<-EOT
    # Media services
    path "homelab/data/docker/plex" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/radarr" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/sonarr" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/ombi" {
      capabilities = ["read"]
    }

    # Download clients
    path "homelab/data/docker/nzbget" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/deluge" {
      capabilities = ["read"]
    }

    # Monitoring
    path "homelab/data/docker/grafana" {
      capabilities = ["read"]
    }
    path "homelab/data/docker/influxdb" {
      capabilities = ["read"]
    }

    # Document management
    path "homelab/data/docker/paperless" {
      capabilities = ["read"]
    }

    # IoT / messaging
    path "homelab/data/mqtt" {
      capabilities = ["read"]
    }

    # Cameras
    path "homelab/data/cameras" {
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
    # Read secrets for terraform plan
    path "homelab/*" {
      capabilities = ["read", "list"]
    }

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

    # Read mounts for plan
    path "sys/mounts" {
      capabilities = ["read", "list"]
    }
    path "sys/mounts/*" {
      capabilities = ["read"]
    }

    # Health check
    path "sys/health" {
      capabilities = ["read"]
    }
  EOT
}
