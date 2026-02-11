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

// PaperlessBackend retrieves API tokens from Paperless-ngx via its token endpoint.
// Note: Paperless Django REST Framework tokens are persistent per-user, not short-lived.
// The value is keeping the admin password in Vault and gating access behind T2 approval.
type PaperlessBackend struct {
	baseURL     string
	vaultReader VaultSecretReader
	vaultPath   string
	http        *http.Client
}

// NewPaperlessBackend creates a Paperless-ngx dynamic backend.
func NewPaperlessBackend(baseURL string, vaultReader VaultSecretReader) *PaperlessBackend {
	return &PaperlessBackend{
		baseURL:     strings.TrimRight(baseURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/paperless",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// MintCredential retrieves a Paperless API token using admin credentials from Vault.
func (b *PaperlessBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	username, ok := secrets["username"]
	if !ok || username == "" {
		return nil, fmt.Errorf("missing username in vault path %s", b.vaultPath)
	}
	password, ok := secrets["password"]
	if !ok || password == "" {
		return nil, fmt.Errorf("missing password in vault path %s", b.vaultPath)
	}

	// POST JSON to Paperless token endpoint
	payload := map[string]string{
		"username": username,
		"password": password,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal token request: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, b.baseURL+"/api/token/", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post paperless token endpoint: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("paperless token endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var tokenResp struct {
		Token string `json:"token"`
	}
	if err := json.Unmarshal(respBody, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode paperless token response: %w", err)
	}

	if tokenResp.Token == "" {
		return nil, fmt.Errorf("paperless returned empty token")
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "paperless",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
	})

	return &Credential{
		Token:    tokenResp.Token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":    "api_token",
			"backend": "paperless",
		},
	}, nil
}

// Health checks if Paperless-ngx is reachable.
func (b *PaperlessBackend) Health() error {
	resp, err := b.http.Get(b.baseURL + "/api/")
	if err != nil {
		return fmt.Errorf("paperless health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusUnauthorized && resp.StatusCode != http.StatusForbidden {
		return fmt.Errorf("paperless health returned %d", resp.StatusCode)
	}
	return nil
}
