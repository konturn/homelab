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

// GmailScope defines the OAuth2 scope and corresponding refresh token field.
type GmailScope struct {
	Scope             string
	RefreshTokenField string
	ShortName         string
}

// Predefined Gmail scopes.
var (
	GmailScopeRead = GmailScope{
		Scope:             "https://www.googleapis.com/auth/gmail.readonly",
		RefreshTokenField: "refresh_token_read",
		ShortName:         "gmail.readonly",
	}
	GmailScopeSend = GmailScope{
		Scope:             "https://www.googleapis.com/auth/gmail.send",
		RefreshTokenField: "refresh_token_send",
		ShortName:         "gmail.send",
	}
)

// GmailBackend mints short-lived OAuth2 access tokens via the Google token endpoint.
// Credentials (client_id, client_secret, refresh_token) are read from Vault at runtime.
type GmailBackend struct {
	tokenURL    string
	vaultReader VaultSecretReader
	vaultPath   string
	scope       GmailScope
	http        *http.Client
}

// NewGmailBackend creates a Gmail OAuth2 dynamic backend for the given scope.
func NewGmailBackend(tokenURL string, vaultReader VaultSecretReader, scope GmailScope) *GmailBackend {
	return &GmailBackend{
		tokenURL:    tokenURL,
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/email/google-oauth",
		scope:       scope,
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// googleTokenResponse is the OAuth2 token response from Google.
type googleTokenResponse struct {
	AccessToken string `json:"access_token"`
	TokenType   string `json:"token_type"`
	ExpiresIn   int    `json:"expires_in"`
	Scope       string `json:"scope"`
}

// MintCredential obtains a short-lived Gmail OAuth2 access token.
func (b *GmailBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	clientID, ok := secrets["client_id"]
	if !ok || clientID == "" {
		return nil, fmt.Errorf("missing client_id in vault path %s", b.vaultPath)
	}
	clientSecret, ok := secrets["client_secret"]
	if !ok || clientSecret == "" {
		return nil, fmt.Errorf("missing client_secret in vault path %s", b.vaultPath)
	}
	refreshToken, ok := secrets[b.scope.RefreshTokenField]
	if !ok || refreshToken == "" {
		return nil, fmt.Errorf("missing %s in vault path %s", b.scope.RefreshTokenField, b.vaultPath)
	}

	// Request OAuth2 token using refresh_token grant
	form := url.Values{
		"client_id":     {clientID},
		"client_secret": {clientSecret},
		"refresh_token": {refreshToken},
		"grant_type":    {"refresh_token"},
		"scope":         {b.scope.Scope},
	}

	req, err := http.NewRequest(http.MethodPost, b.tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("google token request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode != http.StatusOK {
		logger.Error("gmail_token_failed", logger.Fields{
			"status": resp.StatusCode,
			"body":   string(respBody),
			"scope":  b.scope.ShortName,
		})
		return nil, fmt.Errorf("google token endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var tokenResp googleTokenResponse
	if err := json.Unmarshal(respBody, &tokenResp); err != nil {
		return nil, fmt.Errorf("decode response: %w", err)
	}

	if tokenResp.AccessToken == "" {
		return nil, fmt.Errorf("empty access_token in google response")
	}

	actualTTL := ttl
	if tokenResp.ExpiresIn > 0 {
		actualTTL = time.Duration(tokenResp.ExpiresIn) * time.Second
	}

	logger.Info("gmail_token_minted", logger.Fields{
		"resource":   resource,
		"scope":      b.scope.ShortName,
		"expires_in": tokenResp.ExpiresIn,
	})

	return &Credential{
		Token:    tokenResp.AccessToken,
		LeaseTTL: actualTTL,
		Metadata: map[string]string{
			"backend": "gmail",
			"type":    "oauth2_access_token",
			"scope":   b.scope.ShortName,
			"email":   "konoahko@gmail.com",
		},
	}, nil
}

// Health checks if the Google token endpoint is reachable.
func (b *GmailBackend) Health() error {
	// A GET to the token endpoint returns 405 Method Not Allowed, confirming it's up.
	resp, err := b.http.Get(b.tokenURL)
	if err != nil {
		return fmt.Errorf("google token endpoint health check: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 500 {
		return fmt.Errorf("google token endpoint returned %d", resp.StatusCode)
	}
	return nil
}
