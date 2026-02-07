package backend

import (
	"fmt"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// VaultTokenMinter is the interface for minting Vault tokens (implemented by vault.Client).
type VaultTokenMinter interface {
	MintToken(resource string, tier int, ttl time.Duration) (token string, leaseID string, err error)
}

// StaticBackend falls back to minting a standard Vault token.
// This is the original behavior for resources without dynamic backends.
type StaticBackend struct {
	vault VaultTokenMinter
}

// NewStaticBackend creates a static Vault-token backend.
func NewStaticBackend(vault VaultTokenMinter) *StaticBackend {
	return &StaticBackend{vault: vault}
}

// MintCredential mints a standard scoped Vault token.
func (b *StaticBackend) MintCredential(resource string, tier int, ttl time.Duration) (*Credential, error) {
	token, leaseID, err := b.vault.MintToken(resource, tier, ttl)
	if err != nil {
		return nil, fmt.Errorf("vault mint token: %w", err)
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "static",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
	})

	return &Credential{
		Token:    token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":     "vault_token",
			"backend":  "static",
			"lease_id": leaseID,
		},
	}, nil
}

// Health always returns nil for the static backend (Vault health is checked separately).
func (b *StaticBackend) Health() error {
	return nil
}
