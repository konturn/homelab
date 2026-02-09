package backend

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// --- GitLab Tests ---

func TestGitLabBackend_MintCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/projects/4/access_tokens" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if r.Header.Get("PRIVATE-TOKEN") != "gitlab-admin-token" {
			t.Errorf("unexpected auth header: %s", r.Header.Get("PRIVATE-TOKEN"))
		}

		var body gitlabTokenRequest
		json.NewDecoder(r.Body).Decode(&body)
		if body.Name == "" {
			t.Error("expected non-empty name in body")
		}
		if len(body.Scopes) != 1 || body.Scopes[0] != "api" {
			t.Errorf("expected scopes [api], got %v", body.Scopes)
		}
		if body.AccessLevel != 30 {
			t.Errorf("expected access_level 30, got %d", body.AccessLevel)
		}
		if body.ExpiresAt == "" {
			t.Error("expected non-empty expires_at")
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(gitlabTokenResponse{
			ID:    99,
			Token: "glpat-gitlab-ephemeral-token",
			Name:  body.Name,
		})
	}))
	defer server.Close()

	b := NewGitLabBackend(server.URL, "gitlab-admin-token")
	cred, err := b.MintCredential("gitlab", 2, 30*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "glpat-gitlab-ephemeral-token" {
		t.Errorf("expected token glpat-gitlab-ephemeral-token, got %s", cred.Token)
	}
	if cred.LeaseTTL != 30*time.Minute {
		t.Errorf("expected TTL 30m, got %s", cred.LeaseTTL)
	}
	if cred.Metadata["backend"] != "gitlab" {
		t.Errorf("expected backend gitlab, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["project_id"] != "4" {
		t.Errorf("expected project_id 4, got %s", cred.Metadata["project_id"])
	}
	if cred.Metadata["token_id"] != "99" {
		t.Errorf("expected token_id 99, got %s", cred.Metadata["token_id"])
	}
}

func TestGitLabBackend_MintCredential_Error(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Write([]byte(`{"message": "403 Forbidden"}`))
	}))
	defer server.Close()

	b := NewGitLabBackend(server.URL, "bad-token")
	_, err := b.MintCredential("gitlab", 2, 30*time.Minute)
	if err == nil {
		t.Fatal("expected error from bad gitlab response")
	}
}

func TestGitLabBackend_RevokeCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/projects/4/access_tokens/99" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodDelete {
			t.Errorf("expected DELETE, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	b := NewGitLabBackend(server.URL, "gitlab-admin-token")
	err := b.RevokeCredential("99")
	if err != nil {
		t.Fatalf("RevokeCredential failed: %v", err)
	}
}

func TestGitLabBackend_RevokeCredential_Error(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte(`{"message": "404 Not Found"}`))
	}))
	defer server.Close()

	b := NewGitLabBackend(server.URL, "gitlab-admin-token")
	err := b.RevokeCredential("999")
	if err == nil {
		t.Fatal("expected error from revoke of non-existent token")
	}
}

func TestGitLabBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v4/version" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"version": "16.0.0"}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	b := NewGitLabBackend(server.URL, "gitlab-admin-token")
	if err := b.Health(); err != nil {
		t.Errorf("Health() failed: %v", err)
	}
}

func TestRegistry_GitLabDynamicBackend(t *testing.T) {
	minter := &mockVaultMinter{token: "vault-token", leaseID: "acc-1"}
	reader := &mockVaultReader{secrets: map[string]map[string]string{}}

	r := NewRegistry(minter, reader, "", "", "", "https://gitlab.example.com", "admin-token")

	if !r.IsDynamic("gitlab") {
		t.Error("expected gitlab to be dynamic")
	}
}

func TestRegistry_GitLabFallbackWhenNoToken(t *testing.T) {
	minter := &mockVaultMinter{token: "vault-token", leaseID: "acc-1"}
	reader := &mockVaultReader{secrets: map[string]map[string]string{}}

	// URL set but no token — should NOT register dynamic backend
	r := NewRegistry(minter, reader, "", "", "", "https://gitlab.example.com", "")

	if r.IsDynamic("gitlab") {
		t.Error("expected gitlab to NOT be dynamic when admin token is empty")
	}
}
