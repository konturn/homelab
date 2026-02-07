package backend

import (
	"time"
)

// Backend defines the interface for credential backends.
// Dynamic backends generate ephemeral credentials from upstream services.
// The static backend falls back to minting Vault tokens.
type Backend interface {
	// MintCredential generates an ephemeral credential for this resource.
	MintCredential(resource string, tier int, ttl time.Duration) (*Credential, error)
	// Health checks if the backend service is reachable.
	Health() error
}

// Credential holds an ephemeral credential returned by a backend.
type Credential struct {
	Token    string
	LeaseTTL time.Duration
	Metadata map[string]string // extra info (e.g. "type": "transient", "service_account_id": "5")
}
