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

// policiesForResource maps a resource name and tier to Vault policies.
func policiesForResource(resource string, tier int) []string {
	// Resource-specific policy mappings
	resourcePolicies := map[string][]string{
		"homeassistant": {"prometheus-tier1-homeassistant"},
		"grafana":       {"prometheus-tier1-grafana"},
		"influxdb":      {"prometheus-tier1-influxdb"},
		"tautulli":      {"prometheus-tier1-tautulli"},
		"plex":          {"prometheus-tier1-plex"},
		"radarr":        {"prometheus-tier1-radarr"},
		"sonarr":        {"prometheus-tier1-sonarr"},
		"gitlab":        {"prometheus-tier2-gitlab"},
		"portainer":     {"prometheus-tier2-portainer"},
		"docker":        {"prometheus-tier2-docker"},
		"ssh":           {"prometheus-tier2-ssh"},
		"vault-admin":   {"prometheus-tier3-vault-admin"},
		"network":       {"prometheus-tier3-network"},
	}

	if policies, ok := resourcePolicies[resource]; ok {
		return policies
	}

	// Fallback: tier-based generic policy
	return []string{fmt.Sprintf("prometheus-tier%d-generic", tier)}
}

func boolPtr(b bool) *bool {
	return &b
}
