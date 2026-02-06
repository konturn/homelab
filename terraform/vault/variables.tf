variable "vault_addr" {
  description = "Vault server address"
  type        = string
  default     = "https://vault.lab.nkontur.com:8200"
}

variable "vault_token" {
  description = "Vault authentication token"
  type        = string
  sensitive   = true
}
