package backend

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// GitLabAgentBackend mints short-lived impersonation tokens for one GitLab
// user (the agent's own account, e.g. moltbot). The token acts with exactly
// that user's memberships and permissions, so MRs and comments keep the
// agent's identity and nothing is widened.
//
// GitLab token expiry is date-granular (the token stays valid until the end
// of its expires_at day), so the JIT TTL is enforced by revocation: every
// token's name carries its JIT expiry ("jit-<unix>-<request id>"), and a
// sweeper revokes active "jit-" tokens of the user once that time has
// passed. The sweeper lists tokens from GitLab, so it also catches tokens
// minted before a restart.
type GitLabAgentBackend struct {
	baseURL    string
	adminToken string
	username   string
	allowed    map[string]bool
	http       *http.Client
	now        func() time.Time

	mu     sync.Mutex
	userID int
}

// GitLabAgentTokenPrefix marks impersonation tokens minted (and swept) by JIT.
const GitLabAgentTokenPrefix = "jit-"

// NewGitLabAgentBackend creates the backend. adminToken must belong to a
// GitLab administrator (impersonation tokens are an admin API). allowedScopes
// caps what a request may ask for.
func NewGitLabAgentBackend(baseURL, adminToken, username string, allowedScopes []string) *GitLabAgentBackend {
	allowed := make(map[string]bool, len(allowedScopes))
	for _, s := range allowedScopes {
		if s = strings.TrimSpace(s); s != "" {
			allowed[s] = true
		}
	}
	return &GitLabAgentBackend{
		baseURL:    strings.TrimRight(baseURL, "/"),
		adminToken: adminToken,
		username:   username,
		allowed:    allowed,
		http:       &http.Client{Timeout: 10 * time.Second},
		now:        time.Now,
	}
}

type gitlabImpersonationToken struct {
	ID        int      `json:"id"`
	Name      string   `json:"name"`
	Token     string   `json:"token,omitempty"`
	Scopes    []string `json:"scopes"`
	Active    bool     `json:"active"`
	Revoked   bool     `json:"revoked"`
	ExpiresAt string   `json:"expires_at"`
}

func (b *GitLabAgentBackend) do(method, path string, body interface{}) (int, []byte, error) {
	var rdr io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return 0, nil, err
		}
		rdr = bytes.NewReader(buf)
	}
	req, err := http.NewRequest(method, b.baseURL+"/api/v4"+path, rdr)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("PRIVATE-TOKEN", b.adminToken)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := b.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, data, nil
}

// resolveUserID looks the user up once by username.
func (b *GitLabAgentBackend) resolveUserID() (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.userID != 0 {
		return b.userID, nil
	}
	code, data, err := b.do(http.MethodGet, "/users?username="+url.QueryEscape(b.username), nil)
	if err != nil {
		return 0, fmt.Errorf("look up gitlab user: %w", err)
	}
	if code != http.StatusOK {
		return 0, fmt.Errorf("gitlab user lookup returned %d", code)
	}
	var users []struct {
		ID       int    `json:"id"`
		Username string `json:"username"`
	}
	if err := json.Unmarshal(data, &users); err != nil {
		return 0, fmt.Errorf("decode gitlab users: %w", err)
	}
	for _, u := range users {
		if u.Username == b.username {
			b.userID = u.ID
			return u.ID, nil
		}
	}
	return 0, fmt.Errorf("gitlab user %q not found", b.username)
}

// scopesFor validates requested scopes against the allowlist. An empty
// request, or the handler's default ["api"], gets the full allowlist.
func (b *GitLabAgentBackend) scopesFor(requested []string) ([]string, error) {
	if len(requested) == 0 || (len(requested) == 1 && requested[0] == "api") {
		out := make([]string, 0, len(b.allowed))
		for _, s := range []string{"api", "read_api", "read_repository", "write_repository"} {
			if b.allowed[s] {
				out = append(out, s)
			}
		}
		if len(out) == 0 {
			return nil, fmt.Errorf("no gitlab scopes configured")
		}
		return out, nil
	}
	for _, s := range requested {
		if !b.allowed[s] {
			return nil, fmt.Errorf("scope %q not allowed for %s", s, b.username)
		}
	}
	return requested, nil
}

// MintCredential creates an impersonation token that the sweeper revokes
// once ttl has passed.
func (b *GitLabAgentBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	if ttl <= 0 {
		return nil, fmt.Errorf("ttl must be positive")
	}
	scopes, err := b.scopesFor(opts.Scopes)
	if err != nil {
		return nil, err
	}
	uid, err := b.resolveUserID()
	if err != nil {
		return nil, err
	}

	now := b.now()
	expires := now.Add(ttl)
	reqID := opts.RequestID
	if reqID == "" {
		reqID = "manual"
	}
	name := fmt.Sprintf("%s%d-%s", GitLabAgentTokenPrefix, expires.Unix(), reqID)
	// Date-granular backstop: the token dies at the end of the day after
	// its JIT expiry even if every revocation fails.
	expiresAt := expires.UTC().AddDate(0, 0, 1).Format("2006-01-02")

	code, data, err := b.do(http.MethodPost, fmt.Sprintf("/users/%d/impersonation_tokens", uid), map[string]interface{}{
		"name":       name,
		"scopes":     scopes,
		"expires_at": expiresAt,
	})
	if err != nil {
		return nil, fmt.Errorf("create impersonation token: %w", err)
	}
	if code != http.StatusCreated && code != http.StatusOK {
		return nil, fmt.Errorf("gitlab impersonation token endpoint returned %d: %s", code, truncate(string(data), 300))
	}
	var tok gitlabImpersonationToken
	if err := json.Unmarshal(data, &tok); err != nil {
		return nil, fmt.Errorf("decode impersonation token: %w", err)
	}
	if tok.Token == "" {
		return nil, fmt.Errorf("gitlab returned empty token")
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "gitlab-agent",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
		"user":     b.username,
		"token_id": tok.ID,
		"scopes":   strings.Join(scopes, ","),
	})

	return &Credential{
		Token:    tok.Token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":       "impersonation_token",
			"backend":    "gitlab-agent",
			"user":       b.username,
			"token_id":   strconv.Itoa(tok.ID),
			"token_name": tok.Name,
			"scopes":     strings.Join(scopes, ","),
			"expires_at": expires.UTC().Format(time.RFC3339),
		},
	}, nil
}

// expiryFromName parses the JIT expiry out of a token name.
func expiryFromName(name string) (time.Time, bool) {
	if !strings.HasPrefix(name, GitLabAgentTokenPrefix) {
		return time.Time{}, false
	}
	rest := strings.TrimPrefix(name, GitLabAgentTokenPrefix)
	ts := rest
	if i := strings.Index(rest, "-"); i >= 0 {
		ts = rest[:i]
	}
	sec, err := strconv.ParseInt(ts, 10, 64)
	if err != nil {
		return time.Time{}, false
	}
	return time.Unix(sec, 0), true
}

// Sweep revokes the user's active JIT impersonation tokens whose expiry has
// passed. Tokens not named by JIT are never touched. Returns how many were
// revoked.
func (b *GitLabAgentBackend) Sweep() (int, error) {
	uid, err := b.resolveUserID()
	if err != nil {
		return 0, err
	}
	now := b.now()
	revoked := 0
	for page := 1; page <= 20; page++ {
		code, data, err := b.do(http.MethodGet, fmt.Sprintf("/users/%d/impersonation_tokens?state=active&per_page=100&page=%d", uid, page), nil)
		if err != nil {
			return revoked, fmt.Errorf("list impersonation tokens: %w", err)
		}
		if code != http.StatusOK {
			return revoked, fmt.Errorf("list impersonation tokens returned %d", code)
		}
		var toks []gitlabImpersonationToken
		if err := json.Unmarshal(data, &toks); err != nil {
			return revoked, fmt.Errorf("decode impersonation tokens: %w", err)
		}
		for _, t := range toks {
			exp, ok := expiryFromName(t.Name)
			if !ok || !t.Active || t.Revoked || now.Before(exp) {
				continue
			}
			c, body, err := b.do(http.MethodDelete, fmt.Sprintf("/users/%d/impersonation_tokens/%d", uid, t.ID), nil)
			if err != nil || (c != http.StatusNoContent && c != http.StatusOK && c != http.StatusNotFound) {
				logger.Error("gitlab_agent_revoke_failed", logger.Fields{
					"token_id": t.ID,
					"status":   c,
					"body":     truncate(string(body), 200),
				})
				continue
			}
			revoked++
			logger.Info("backend_credential_revoked", logger.Fields{
				"backend":  "gitlab-agent",
				"user":     b.username,
				"token_id": t.ID,
			})
		}
		if len(toks) < 100 {
			break
		}
	}
	return revoked, nil
}

// StartSweeper runs Sweep every interval until stop is closed.
func (b *GitLabAgentBackend) StartSweeper(interval time.Duration, stop <-chan struct{}) {
	go func() {
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			if _, err := b.Sweep(); err != nil {
				logger.Error("gitlab_agent_sweep_failed", logger.Fields{"error": err.Error()})
			}
			select {
			case <-stop:
				return
			case <-t.C:
			}
		}
	}()
}

// Health checks that the admin token works.
func (b *GitLabAgentBackend) Health() error {
	code, _, err := b.do(http.MethodGet, "/version", nil)
	if err != nil {
		return fmt.Errorf("gitlab health check: %w", err)
	}
	if code != http.StatusOK {
		return fmt.Errorf("gitlab health returned %d", code)
	}
	return nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
