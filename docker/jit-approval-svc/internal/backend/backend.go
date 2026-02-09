package backend

import (
	"time"
)

// MintOptions carries optional parameters for credential minting.
// Backends that don't support a given option simply ignore it.
type MintOptions struct {
	// Scopes restricts the credential to specific permission scopes.
	// Interpretation is backend-specific (e.g. GitLab project access token scopes).
	Scopes []string
}

// Backend defines the interface for credential backends.
// Dynamic backends generate ephemeral credentials from upstream services.
// The static backend falls back to minting Vault tokens.
type Backend interface {
	// MintCredential generates an ephemeral credential for this resource.
	MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error)
	// Health checks if the backend service is reachable.
	Health() error
}

// Credential holds an ephemeral credential returned by a backend.
type Credential struct {
	Token    string
	LeaseTTL time.Duration
	Metadata map[string]string // extra info (e.g. "type": "transient", "service_account_id": "5")
}
