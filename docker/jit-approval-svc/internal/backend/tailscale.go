package backend

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// TailscaleBackend mints short-lived OAuth access tokens via the Tailscale API.
// Credentials (client_id, client_secret) are read from Vault at runtime.
type TailscaleBackend struct {
	apiURL      string
	vaultReader VaultSecretReader
	vaultPath   string
	http        *http.Client
}

// NewTailscaleBackend creates a Tailscale dynamic backend.
func NewTailscaleBackend(apiURL string, vaultReader VaultSecretReader) *TailscaleBackend {
	return &TailscaleBackend{
		apiURL:      strings.TrimRight(apiURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/jit-approval-svc",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// tailscaleTokenRequest is the OAuth token request body.
type tailscaleTokenRequest struct {
	ClientID     string `json:"client_id"`
	ClientSecret string `json:"client_secret"`
	GrantType    string `json:"grant_type"`
}

// tailscaleTokenResponse is the OAuth token response.
type tailscaleTokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	ExpiresIn   int    `json:"expires_in"`
}

// MintCredential obtains a short-lived Tailscale OAuth access token.
// Scopes from opts are currently unused but may be passed to Tailscale in the future.
func (b *TailscaleBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	clientID, ok := secrets["tailscale_client_id"]
	if !ok || clientID == "" {
		return nil, fmt.Errorf("missing tailscale_client_id in vault path %s", b.vaultPath)
	}
	clientSecret, ok := secrets["tailscale_client_secret"]
	if !ok || clientSecret == "" {
		return nil, fmt.Errorf("missing tailscale_client_secret in vault path %s", b.vaultPath)
	}

	// Request OAuth token using client credentials grant
	payload := tailscaleTokenRequest{
		ClientID:     clientID,
		ClientSecret: clientSecret,
		GrantType:    "client_credentials",
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	url := b.apiURL + "/api/v2/oauth/token"
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("tailscale API request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		logger.Error("tailscale_token_failed", logger.Fields{
			"status": resp.StatusCode,
			"body":   string(respBody),
		})
		return nil, fmt.Errorf("tailscale API returned %d: %s", resp.StatusCode, string(respBody))
	}

	var tokenResp tailscaleTokenResponse
	if err := json.Unmarshal(respBody, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	if tokenResp.AccessToken == "" {
		return nil, fmt.Errorf("empty access_token in tailscale response")
	}

	actualTTL := ttl
	if tokenResp.ExpiresIn > 0 {
		actualTTL = time.Duration(tokenResp.ExpiresIn) * time.Second
	}

	logger.Info("tailscale_token_minted", logger.Fields{
		"resource":   resource,
		"expires_in": tokenResp.ExpiresIn,
	})

	return &Credential{
		Token:    tokenResp.AccessToken,
		LeaseTTL: actualTTL,
		Metadata: map[string]string{
			"backend":    "tailscale",
			"type":       "oauth_access_token",
			"token_type": tokenResp.TokenType,
		},
	}, nil
}

// Health checks if the Tailscale API is reachable.
func (b *TailscaleBackend) Health() error {
	resp, err := b.http.Get(b.apiURL + "/api/v2/tailnet/-/devices")
	if err != nil {
		return fmt.Errorf("tailscale health check: %w", err)
	}
	defer resp.Body.Close()
	// 401 is expected (no auth), but confirms API is reachable
	if resp.StatusCode >= 500 {
		return fmt.Errorf("tailscale API returned %d", resp.StatusCode)
	}
	return nil
}
