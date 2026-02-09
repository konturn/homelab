package backend

import (
	"fmt"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

const (
	// vaultPathPrefix is the required prefix for all requested Vault paths.
	vaultPathPrefix = "homelab/data/"

	// maxVaultPaths is the maximum number of paths allowed per request.
	maxVaultPaths = 10

	// policyPrefix is prepended to temporary policy names.
	policyPrefix = "jit-vault-"
)

// allowedCapabilities is the set of capabilities that may be requested.
var allowedCapabilities = map[string]bool{
	"read":   true,
	"list":   true,
	"create": true,
	"update": true,
}

// VaultPolicyManager can create and delete ACL policies in Vault.
type VaultPolicyManager interface {
	PutPolicy(name, rules string) error
	DeletePolicy(name string) error
}

// VaultDynamicBackend creates Vault tokens with dynamically scoped policies.
// Instead of pre-built policies, the requester specifies exactly which paths
// and capabilities they need. A temporary policy is created, a token is minted
// with that policy, and a cleanup goroutine deletes the policy after the TTL.
type VaultDynamicBackend struct {
	tokenMinter   VaultTokenMinter
	policyManager VaultPolicyManager
}

// NewVaultDynamicBackend creates a new dynamic Vault backend.
func NewVaultDynamicBackend(minter VaultTokenMinter, pm VaultPolicyManager) *VaultDynamicBackend {
	return &VaultDynamicBackend{
		tokenMinter:   minter,
		policyManager: pm,
	}
}

// ValidateVaultPaths checks that the requested paths and capabilities are allowed.
func ValidateVaultPaths(paths []VaultPathRequest) error {
	if len(paths) == 0 {
		return fmt.Errorf("vault_paths is required when resource is vault")
	}
	if len(paths) > maxVaultPaths {
		return fmt.Errorf("too many vault paths: %d (max %d)", len(paths), maxVaultPaths)
	}

	for i, p := range paths {
		if p.Path == "" {
			return fmt.Errorf("vault_paths[%d]: path is required", i)
		}
		if !strings.HasPrefix(p.Path, vaultPathPrefix) {
			return fmt.Errorf("vault_paths[%d]: path %q must start with %q", i, p.Path, vaultPathPrefix)
		}
		if len(p.Capabilities) == 0 {
			return fmt.Errorf("vault_paths[%d]: at least one capability is required", i)
		}
		for _, cap := range p.Capabilities {
			if !allowedCapabilities[cap] {
				return fmt.Errorf("vault_paths[%d]: capability %q is not allowed (allowed: read, list, create, update)", i, cap)
			}
		}
	}
	return nil
}

// BuildPolicyHCL generates an HCL policy string from the requested paths.
func BuildPolicyHCL(paths []VaultPathRequest) string {
	var sb strings.Builder
	sb.WriteString("# Auto-generated JIT dynamic Vault policy\n")
	for _, p := range paths {
		sb.WriteString(fmt.Sprintf("\npath %q {\n", p.Path))
		caps := make([]string, len(p.Capabilities))
		for i, c := range p.Capabilities {
			caps[i] = fmt.Sprintf("%q", c)
		}
		sb.WriteString(fmt.Sprintf("  capabilities = [%s]\n", strings.Join(caps, ", ")))
		sb.WriteString("}\n")
	}
	return sb.String()
}

// MintCredential creates a dynamically scoped Vault token.
// The opts.VaultPaths and opts.RequestID must be set.
func (b *VaultDynamicBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	if len(opts.VaultPaths) == 0 {
		return nil, fmt.Errorf("vault_paths required for dynamic vault backend")
	}
	if opts.RequestID == "" {
		return nil, fmt.Errorf("request_id required for dynamic vault backend")
	}

	// Validate paths (defense in depth, handler validates too)
	if err := ValidateVaultPaths(opts.VaultPaths); err != nil {
		return nil, fmt.Errorf("path validation: %w", err)
	}

	// Build and create temporary policy
	policyName := policyPrefix + opts.RequestID
	policyHCL := BuildPolicyHCL(opts.VaultPaths)

	if err := b.policyManager.PutPolicy(policyName, policyHCL); err != nil {
		return nil, fmt.Errorf("create temporary policy %s: %w", policyName, err)
	}

	logger.Info("vault_dynamic_policy_created", logger.Fields{
		"policy_name": policyName,
		"paths_count": len(opts.VaultPaths),
		"request_id":  opts.RequestID,
	})

	// Mint token with the temporary policy
	token, accessor, err := b.tokenMinter.MintDynamicToken(policyName, ttl, opts.RequestID)
	if err != nil {
		// Clean up the policy on failure
		if delErr := b.policyManager.DeletePolicy(policyName); delErr != nil {
			logger.Error("vault_dynamic_policy_cleanup_failed", logger.Fields{
				"policy_name": policyName,
				"error":       delErr.Error(),
			})
		}
		return nil, fmt.Errorf("mint dynamic token: %w", err)
	}

	// Schedule policy cleanup after TTL expires (with buffer)
	go func() {
		cleanupDelay := ttl + 5*time.Minute
		time.Sleep(cleanupDelay)
		if delErr := b.policyManager.DeletePolicy(policyName); delErr != nil {
			logger.Error("vault_dynamic_policy_cleanup_failed", logger.Fields{
				"policy_name": policyName,
				"error":       delErr.Error(),
			})
		} else {
			logger.Info("vault_dynamic_policy_cleaned_up", logger.Fields{
				"policy_name": policyName,
				"request_id":  opts.RequestID,
			})
		}
	}()

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":     "vault_dynamic",
		"resource":    resource,
		"tier":        tier,
		"ttl":         ttl.String(),
		"policy_name": policyName,
		"request_id":  opts.RequestID,
	})

	return &Credential{
		Token:    token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":        "vault_token",
			"backend":     "vault_dynamic",
			"lease_id":    accessor,
			"policy_name": policyName,
		},
	}, nil
}

// Health always returns nil (Vault health checked separately).
func (b *VaultDynamicBackend) Health() error {
	return nil
}
