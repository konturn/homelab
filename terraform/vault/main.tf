terraform {
  required_version = ">= 1.7.0"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 4.0"
    }
  }

  # State stored in GitLab's built-in Terraform state backend
  # Accessible at: Settings > Infrastructure > Terraform states
  # Auth via CI_JOB_TOKEN in pipelines, or TF_HTTP_USERNAME/PASSWORD locally
  backend "http" {
  }
}

provider "vault" {
  address = var.vault_addr
  token   = var.vault_token

  # Skip TLS verification for internal lab certs
  skip_tls_verify = true
}
