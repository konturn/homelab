package backend

import (
	"time"
)

// VaultPathRequest represents a requested Vault path with capabilities.
// Duplicated here to avoid circular import with store package.
type VaultPathRequest struct {
	Path         string
	Capabilities []string
}

// MintOptions carries optional parameters for credential minting.
// Backends that don't support a given option simply ignore it.
type MintOptions struct {
	// Scopes restricts the credential to specific permission scopes.
	// Interpretation is backend-specific (e.g. GitLab project access token scopes).
	Scopes []string

	// RequestID is the JIT request ID, used for naming temporary resources.
	RequestID string

	// VaultPaths specifies the Vault paths and capabilities for the dynamic Vault backend.
	VaultPaths []VaultPathRequest
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
