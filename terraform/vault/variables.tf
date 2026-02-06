variable "vault_addr" {
  description = "Vault server address"
  type        = string
  default     = "https://127.0.0.1:8200"
}

variable "vault_token" {
  description = "Vault authentication token"
  type        = string
  sensitive   = true
}
