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
