package backend

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// HomeAssistantBackend mints ephemeral access tokens via the HA OAuth refresh flow.
type HomeAssistantBackend struct {
	baseURL      string
	vaultReader  VaultSecretReader
	vaultPath    string
	http         *http.Client
}

// NewHomeAssistantBackend creates a Home Assistant dynamic backend.
func NewHomeAssistantBackend(baseURL string, vaultReader VaultSecretReader) *HomeAssistantBackend {
	return &HomeAssistantBackend{
		baseURL:     strings.TrimRight(baseURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/homeassistant",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// MintCredential obtains a short-lived HA access token using the refresh token stored in Vault.
func (b *HomeAssistantBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	// Read refresh_token and client_id from Vault
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	refreshToken, ok := secrets["refresh_token"]
	if !ok || refreshToken == "" {
		return nil, fmt.Errorf("missing refresh_token in vault path %s", b.vaultPath)
	}
	clientID, ok := secrets["client_id"]
	if !ok || clientID == "" {
		return nil, fmt.Errorf("missing client_id in vault path %s", b.vaultPath)
	}

	// POST to HA token endpoint
	form := url.Values{}
	form.Set("grant_type", "refresh_token")
	form.Set("refresh_token", refreshToken)
	form.Set("client_id", clientID)

	req, err := http.NewRequest(http.MethodPost, b.baseURL+"/auth/token", strings.NewReader(form.Encode()))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post token endpoint: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HA token endpoint returned %d: %s", resp.StatusCode, string(body))
	}

	var tokenResp struct {
		AccessToken string `json:"access_token"`
		TokenType   string `json:"token_type"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if err := json.Unmarshal(body, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode token response: %w", err)
	}

	if tokenResp.AccessToken == "" {
		return nil, fmt.Errorf("HA returned empty access_token")
	}

	// HA access tokens are 30 min, non-configurable
	leaseTTL := 30 * time.Minute
	if tokenResp.ExpiresIn > 0 {
		leaseTTL = time.Duration(tokenResp.ExpiresIn) * time.Second
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "homeassistant",
		"resource": resource,
		"tier":     tier,
		"ttl":      leaseTTL.String(),
	})

	return &Credential{
		Token:    tokenResp.AccessToken,
		LeaseTTL: leaseTTL,
		Metadata: map[string]string{
			"type":    "oauth_access_token",
			"backend": "homeassistant",
		},
	}, nil
}

// Health checks if Home Assistant is reachable.
func (b *HomeAssistantBackend) Health() error {
	req, err := http.NewRequest(http.MethodGet, b.baseURL+"/api/", nil)
	if err != nil {
		return fmt.Errorf("create health request: %w", err)
	}

	resp, err := b.http.Do(req)
	if err != nil {
		return fmt.Errorf("HA health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusUnauthorized {
		return fmt.Errorf("HA health returned %d", resp.StatusCode)
	}
	return nil
}
