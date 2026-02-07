package vault

import (
	"fmt"
	"time"

	vaultapi "github.com/hashicorp/vault/api"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// Client wraps the Vault API for JIT credential operations.
type Client struct {
	client *vaultapi.Client
	roleID string
	secretID string
}

// New creates a new Vault client and authenticates via AppRole.
func New(addr, roleID, secretID string) (*Client, error) {
	config := vaultapi.DefaultConfig()
	config.Address = addr
	config.Timeout = 10 * time.Second

	client, err := vaultapi.NewClient(config)
	if err != nil {
		return nil, fmt.Errorf("create vault client: %w", err)
	}

	vc := &Client{
		client:   client,
		roleID:   roleID,
		secretID: secretID,
	}

	if err := vc.authenticate(); err != nil {
		return nil, fmt.Errorf("vault authentication: %w", err)
	}

	return vc, nil
}

// authenticate logs in via AppRole and sets the client token.
func (vc *Client) authenticate() error {
	resp, err := vc.client.Logical().Write("auth/approle/login", map[string]interface{}{
		"role_id":   vc.roleID,
		"secret_id": vc.secretID,
	})
	if err != nil {
		return fmt.Errorf("approle login: %w", err)
	}
	if resp == nil || resp.Auth == nil {
		return fmt.Errorf("approle login returned nil auth")
	}

	vc.client.SetToken(resp.Auth.ClientToken)

	logger.Info("vault_authenticated", logger.Fields{
		"lease_duration": resp.Auth.LeaseDuration,
		"renewable":      resp.Auth.Renewable,
	})

	return nil
}

// MintToken creates a scoped, short-lived Vault token for the given resource.
func (vc *Client) MintToken(resource string, tier int, ttl time.Duration) (string, string, error) {
	policies := policiesForResource(resource, tier)
	if len(policies) == 0 {
		return "", "", fmt.Errorf("no policies defined for resource %q tier %d", resource, tier)
	}

	displayName := fmt.Sprintf("jit-%s-tier%d-%d", resource, tier, time.Now().Unix())

	resp, err := vc.client.Auth().Token().CreateOrphan(&vaultapi.TokenCreateRequest{
		Policies:    policies,
		TTL:         ttl.String(),
		DisplayName: displayName,
		Renewable:   boolPtr(false),
		Metadata: map[string]string{
			"requester": "prometheus",
			"resource":  resource,
			"tier":      fmt.Sprintf("%d", tier),
			"source":    "jit-approval-svc",
		},
	})
	if err != nil {
		// Re-authenticate and retry once
		logger.Warn("vault_token_create_failed_retrying", logger.Fields{
			"error": err.Error(),
		})
		if authErr := vc.authenticate(); authErr != nil {
			return "", "", fmt.Errorf("re-auth failed: %w (original: %v)", authErr, err)
		}
		resp, err = vc.client.Auth().Token().CreateOrphan(&vaultapi.TokenCreateRequest{
			Policies:    policies,
			TTL:         ttl.String(),
			DisplayName: displayName,
			Renewable:   boolPtr(false),
			Metadata: map[string]string{
				"requester": "prometheus",
				"resource":  resource,
				"tier":      fmt.Sprintf("%d", tier),
				"source":    "jit-approval-svc",
			},
		})
		if err != nil {
			return "", "", fmt.Errorf("vault token create (after re-auth): %w", err)
		}
	}

	if resp == nil || resp.Auth == nil {
		return "", "", fmt.Errorf("vault token create returned nil auth")
	}

	logger.Info("token_issued", logger.Fields{
		"resource":       resource,
		"tier":           tier,
		"ttl":            ttl.String(),
		"policies":       policies,
		"display_name":   displayName,
	})

	return resp.Auth.ClientToken, resp.Auth.Accessor, nil
}

// Health checks if Vault is reachable and the token is valid.
func (vc *Client) Health() error {
	_, err := vc.client.Auth().Token().LookupSelf()
	if err != nil {
		// Try re-authenticating
		if authErr := vc.authenticate(); authErr != nil {
			return fmt.Errorf("vault unreachable: %w (re-auth: %v)", err, authErr)
		}
		return nil
	}
	return nil
}

// resourceTier maps each known resource to its tier level.
// This determines which Vault policy the minted token receives.
var resourceTier = map[string]int{
	// Tier 0: Monitoring (auto-approve, 5 min TTL)
	"grafana":  0,
	"influxdb": 0,

	// Tier 1: Service management (auto-approve, 15 min TTL)
	"plex":      1,
	"radarr":    1,
	"sonarr":    1,
	"ombi":      1,
	"nzbget":    1,
	"deluge":    1,
	"paperless": 1,
	"mqtt":      1,
	"cameras":   1,

	// Tier 2: Infrastructure (requires approval, 30 min TTL)
	"gitlab":        2,
	"homeassistant": 2,
}

// tierPolicy maps tier levels to the Vault policy name assigned to minted tokens.
// These policies must exist in Vault (defined in terraform/vault/policies.tf).
var tierPolicy = map[int]string{
	0: "jit-tier0-monitoring",
	1: "jit-tier1-services",
	2: "jit-tier2-infra",
}

// policiesForResource returns the Vault policies for a minted token.
// The resource must be in resourceTier, and the requested tier must match.
func policiesForResource(resource string, tier int) []string {
	expectedTier, known := resourceTier[resource]
	if !known {
		return nil
	}
	if tier != expectedTier {
		return nil
	}

	policy, ok := tierPolicy[tier]
	if !ok {
		return nil
	}

	return []string{policy}
}

func boolPtr(b bool) *bool {
	return &b
}
