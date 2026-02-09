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

// GitLabBackend mints ephemeral project access tokens via the GitLab API.
// Tokens are scoped to a specific project (not instance-wide) for safety.
//
// NOTE: GitLab project access tokens have a minimum expiry of 1 day.
// The JIT TTL (30 min) controls actual access duration; the token expiry
// is set to tomorrow as a safety net. Best-effort revocation cleans up
// tokens when JIT requests expire.
type GitLabBackend struct {
	baseURL   string
	projectID string
	adminToken string
	http      *http.Client
}

// NewGitLabBackend creates a GitLab dynamic backend.
// adminToken is a Maintainer-level token that can create project access tokens.
func NewGitLabBackend(baseURL, adminToken string) *GitLabBackend {
	return &GitLabBackend{
		baseURL:    strings.TrimRight(baseURL, "/"),
		projectID:  "4", // homelab project
		adminToken: adminToken,
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// gitlabTokenRequest is the payload for creating a project access token.
type gitlabTokenRequest struct {
	Name        string   `json:"name"`
	Scopes      []string `json:"scopes"`
	ExpiresAt   string   `json:"expires_at"`
	AccessLevel int      `json:"access_level"`
}

// gitlabTokenResponse is the response from the project access token API.
type gitlabTokenResponse struct {
	ID    int    `json:"id"`
	Token string `json:"token"`
	Name  string `json:"name"`
}

// MintCredential creates a short-lived GitLab project access token.
func (b *GitLabBackend) MintCredential(resource string, tier int, ttl time.Duration) (*Credential, error) {
	// GitLab PAT expiry is date-based (minimum 1 day). Set to tomorrow.
	expiresAt := time.Now().Add(24 * time.Hour).UTC().Format("2006-01-02")
	tokenName := fmt.Sprintf("jit-gitlab-%d", time.Now().Unix())

	payload := gitlabTokenRequest{
		Name:        tokenName,
		Scopes:      []string{"api"},
		ExpiresAt:   expiresAt,
		AccessLevel: 30, // Developer
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal token request: %w", err)
	}

	url := fmt.Sprintf("%s/api/v4/projects/%s/access_tokens", b.baseURL, b.projectID)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("PRIVATE-TOKEN", b.adminToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post gitlab token: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("gitlab token endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var tokenResp gitlabTokenResponse
	if err := json.Unmarshal(respBody, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode gitlab token response: %w", err)
	}

	if tokenResp.Token == "" {
		return nil, fmt.Errorf("gitlab returned empty token")
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "gitlab",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
		"token_id": tokenResp.ID,
	})

	return &Credential{
		Token:    tokenResp.Token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":       "project_access_token",
			"backend":    "gitlab",
			"project_id": b.projectID,
			"token_id":   fmt.Sprintf("%d", tokenResp.ID),
			"token_name": tokenResp.Name,
		},
	}, nil
}

// RevokeCredential attempts to revoke a project access token. Best-effort.
func (b *GitLabBackend) RevokeCredential(tokenID string) error {
	url := fmt.Sprintf("%s/api/v4/projects/%s/access_tokens/%s", b.baseURL, b.projectID, tokenID)
	req, err := http.NewRequest(http.MethodDelete, url, nil)
	if err != nil {
		return fmt.Errorf("create revoke request: %w", err)
	}
	req.Header.Set("PRIVATE-TOKEN", b.adminToken)

	resp, err := b.http.Do(req)
	if err != nil {
		return fmt.Errorf("revoke gitlab token: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("gitlab revoke returned %d: %s", resp.StatusCode, string(body))
	}

	logger.Info("backend_credential_revoked", logger.Fields{
		"backend":  "gitlab",
		"token_id": tokenID,
	})
	return nil
}

// Health checks if GitLab is reachable.
func (b *GitLabBackend) Health() error {
	url := fmt.Sprintf("%s/api/v4/version", b.baseURL)
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("create health request: %w", err)
	}
	req.Header.Set("PRIVATE-TOKEN", b.adminToken)

	resp, err := b.http.Do(req)
	if err != nil {
		return fmt.Errorf("gitlab health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("gitlab health returned %d", resp.StatusCode)
	}
	return nil
}
