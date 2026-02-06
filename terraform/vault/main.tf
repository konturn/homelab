terraform {
  required_version = ">= 1.7.0"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 4.0"
    }
  }

  # Local state stored on the runner (router)
  # State persists at /opt/terraform/vault/terraform.tfstate
  backend "local" {
    path = "/opt/terraform/vault/terraform.tfstate"
  }
}

provider "vault" {
  address = var.vault_addr
  token   = var.vault_token

  # Skip TLS verification for internal lab certs
  skip_tls_verify = true
}
