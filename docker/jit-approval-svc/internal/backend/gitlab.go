package backend

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
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
func NewGitLabBackend(baseURL, adminToken, defaultProjectID string) *GitLabBackend {
	if defaultProjectID == "" {
		defaultProjectID = "4" // homelab project
	}
	return &GitLabBackend{
		baseURL:    strings.TrimRight(baseURL, "/"),
		projectID:  defaultProjectID,
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

// gitlabProjectRef is a numeric project ID or a "namespace/project" path.
var gitlabProjectRef = regexp.MustCompile(`^([0-9]{1,12}|[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)+)$`)

// gitlabProjectTokenScopes are the scopes GitLab accepts for project access
// tokens. Anything else is refused here instead of being passed through.
var gitlabProjectTokenScopes = map[string]bool{
	"api": true, "read_api": true, "read_repository": true, "write_repository": true,
	"read_registry": true, "write_registry": true, "create_runner": true, "manage_runner": true,
	"ai_features": true, "k8s_proxy": true, "self_rotate": true,
	"read_virtual_registry": true, "write_virtual_registry": true,
	"read_observability": true, "write_observability": true,
}

// ValidateGitLabProjectID checks a per-request project override: a numeric
// ID or a "namespace/project" path, nothing that could change the API path.
func ValidateGitLabProjectID(id string) error {
	if !gitlabProjectRef.MatchString(id) || strings.Contains(id, "..") {
		return fmt.Errorf("invalid project_id %q (want a numeric ID or namespace/project path)", id)
	}
	return nil
}

// ValidateGitLabProjectScopes rejects scopes GitLab project access tokens
// do not have.
func ValidateGitLabProjectScopes(scopes []string) error {
	for _, s := range scopes {
		if !gitlabProjectTokenScopes[s] {
			return fmt.Errorf("scope %q is not a GitLab project access token scope", s)
		}
	}
	return nil
}

// MintCredential creates a short-lived GitLab project access token.
func (b *GitLabBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	// Resolve project ID: per-request override or default.
	projectID := b.projectID
	if opts.ProjectID != "" {
		if err := ValidateGitLabProjectID(opts.ProjectID); err != nil {
			return nil, err
		}
		projectID = opts.ProjectID
	}

	// GitLab PAT expiry is date-based (minimum 1 day). Set to tomorrow.
	expiresAt := time.Now().Add(24 * time.Hour).UTC().Format("2006-01-02")
	tokenName := fmt.Sprintf("jit-gitlab-%d", time.Now().Unix())

	scopes := opts.Scopes
	if len(scopes) == 0 {
		scopes = []string{"api"}
	}
	if err := ValidateGitLabProjectScopes(scopes); err != nil {
		return nil, err
	}

	payload := gitlabTokenRequest{
		Name:        tokenName,
		Scopes:      scopes,
		ExpiresAt:   expiresAt,
		AccessLevel: 40, // Maintainer
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal token request: %w", err)
	}

	endpoint := fmt.Sprintf("%s/api/v4/projects/%s/access_tokens", b.baseURL, url.PathEscape(projectID))
	req, err := http.NewRequest(http.MethodPost, endpoint, bytes.NewReader(body))
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
			"project_id": projectID,
			"token_id":   fmt.Sprintf("%d", tokenResp.ID),
			"token_name": tokenResp.Name,
		},
	}, nil
}

// RevokeCredential attempts to revoke a project access token. Best-effort.
// If projectID is empty, the backend's default project is used.
func (b *GitLabBackend) RevokeCredential(tokenID, projectID string) error {
	if projectID == "" {
		projectID = b.projectID
	}
	endpoint := fmt.Sprintf("%s/api/v4/projects/%s/access_tokens/%s", b.baseURL, url.PathEscape(projectID), url.PathEscape(tokenID))
	req, err := http.NewRequest(http.MethodDelete, endpoint, nil)
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
	req, err := http.NewRequest(http.MethodGet, b.baseURL+"/api/v4/version", nil)
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
