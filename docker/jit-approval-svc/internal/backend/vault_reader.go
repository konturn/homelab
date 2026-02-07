package backend

// VaultSecretReader reads secret data from Vault KV v2.
// Implemented by vault.Client to avoid circular imports.
type VaultSecretReader interface {
	ReadSecret(path string) (map[string]string, error)
}
