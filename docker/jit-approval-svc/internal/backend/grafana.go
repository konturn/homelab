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

// GrafanaBackend mints ephemeral service account tokens via the Grafana API.
type GrafanaBackend struct {
	baseURL     string
	vaultReader VaultSecretReader
	vaultPath   string
	http        *http.Client
}

// NewGrafanaBackend creates a Grafana dynamic backend.
func NewGrafanaBackend(baseURL string, vaultReader VaultSecretReader) *GrafanaBackend {
	return &GrafanaBackend{
		baseURL:     strings.TrimRight(baseURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/grafana",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// cleanupExpiredTokens removes expired tokens from the Grafana service account.
// Best-effort: logs warnings but does not fail the mint operation.
func (b *GrafanaBackend) cleanupExpiredTokens(adminToken, serviceAccountID string) {
	url := fmt.Sprintf("%s/api/serviceaccounts/%s/tokens", b.baseURL, serviceAccountID)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		logger.Warn("grafana_cleanup_list_error", logger.Fields{"error": err.Error()})
		return
	}
	req.Header.Set("Authorization", "Bearer "+adminToken)

	resp, err := b.http.Do(req)
	if err != nil {
		logger.Warn("grafana_cleanup_list_error", logger.Fields{"error": err.Error()})
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		logger.Warn("grafana_cleanup_list_status", logger.Fields{"status": resp.StatusCode})
		return
	}

	var tokens []struct {
		ID         int        `json:"id"`
		Name       string     `json:"name"`
		Expiration *time.Time `json:"expiration"`
	}
	body, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(body, &tokens); err != nil {
		logger.Warn("grafana_cleanup_decode_error", logger.Fields{"error": err.Error()})
		return
	}

	now := time.Now().UTC()
	deleted := 0
	for _, t := range tokens {
		if t.Expiration != nil && t.Expiration.Before(now) {
			delURL := fmt.Sprintf("%s/api/serviceaccounts/%s/tokens/%d", b.baseURL, serviceAccountID, t.ID)
			delReq, err := http.NewRequest(http.MethodDelete, delURL, nil)
			if err != nil {
				continue
			}
			delReq.Header.Set("Authorization", "Bearer "+adminToken)
			delResp, err := b.http.Do(delReq)
			if err != nil {
				continue
			}
			delResp.Body.Close()
			if delResp.StatusCode == http.StatusOK || delResp.StatusCode == http.StatusNoContent {
				deleted++
			}
		}
	}
	if deleted > 0 {
		logger.Info("grafana_expired_tokens_cleaned", logger.Fields{
			"deleted": deleted,
			"total":   len(tokens),
		})
	}
}

// MintCredential creates a short-lived Grafana service account token.
func (b *GrafanaBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	adminToken, ok := secrets["jit_admin_token"]
	if !ok || adminToken == "" {
		return nil, fmt.Errorf("missing jit_admin_token in vault path %s", b.vaultPath)
	}
	serviceAccountID, ok := secrets["service_account_id"]
	if !ok || serviceAccountID == "" {
		return nil, fmt.Errorf("missing service_account_id in vault path %s", b.vaultPath)
	}

	// Clean up expired tokens before minting a new one (best-effort)
	b.cleanupExpiredTokens(adminToken, serviceAccountID)

	// Create a token with expiration
	expires := time.Now().Add(ttl).UTC().Format(time.RFC3339)
	tokenName := fmt.Sprintf("jit-%d", time.Now().Unix())

	payload := map[string]interface{}{
		"name":    tokenName,
		"expires": expires,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal token request: %w", err)
	}

	url := fmt.Sprintf("%s/api/serviceaccounts/%s/tokens", b.baseURL, serviceAccountID)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+adminToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post grafana token: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("grafana token endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var tokenResp struct {
		Key string `json:"key"`
		ID  int    `json:"id"`
	}
	if err := json.Unmarshal(respBody, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode grafana token response: %w", err)
	}

	if tokenResp.Key == "" {
		return nil, fmt.Errorf("grafana returned empty token key")
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "grafana",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
	})

	return &Credential{
		Token:    tokenResp.Key,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":               "service_account_token",
			"backend":            "grafana",
			"service_account_id": serviceAccountID,
		},
	}, nil
}

// Health checks if Grafana is reachable.
func (b *GrafanaBackend) Health() error {
	resp, err := b.http.Get(b.baseURL + "/api/health")
	if err != nil {
		return fmt.Errorf("grafana health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("grafana health returned %d", resp.StatusCode)
	}
	return nil
}
