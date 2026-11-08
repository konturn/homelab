package backend

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeGitLab struct {
	mu      sync.Mutex
	tokens  map[int]*gitlabImpersonationToken
	nextID  int
	deleted []int
	created []map[string]interface{}
}

func newFakeGitLab(t *testing.T) (*fakeGitLab, *httptest.Server) {
	f := &fakeGitLab{tokens: map[int]*gitlabImpersonationToken{}, nextID: 100}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("PRIVATE-TOKEN") != "admin-token" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		f.mu.Lock()
		defer f.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v4/users":
			if r.URL.Query().Get("username") != "moltbot" {
				json.NewEncoder(w).Encode([]interface{}{})
				return
			}
			json.NewEncoder(w).Encode([]map[string]interface{}{{"id": 7, "username": "moltbot"}})
		case r.Method == http.MethodPost && r.URL.Path == "/api/v4/users/7/impersonation_tokens":
			var body map[string]interface{}
			json.NewDecoder(r.Body).Decode(&body)
			f.created = append(f.created, body)
			f.nextID++
			var scopes []string
			for _, s := range body["scopes"].([]interface{}) {
				scopes = append(scopes, s.(string))
			}
			tok := &gitlabImpersonationToken{ID: f.nextID, Name: body["name"].(string), Scopes: scopes, Active: true, ExpiresAt: body["expires_at"].(string)}
			f.tokens[tok.ID] = tok
			out := *tok
			out.Token = fmt.Sprintf("glpat-fake-%d", tok.ID)
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(out)
		case r.Method == http.MethodGet && r.URL.Path == "/api/v4/users/7/impersonation_tokens":
			var out []gitlabImpersonationToken
			for _, tk := range f.tokens {
				if tk.Active {
					out = append(out, *tk)
				}
			}
			json.NewEncoder(w).Encode(out)
		case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/api/v4/users/7/impersonation_tokens/"):
			var id int
			fmt.Sscanf(strings.TrimPrefix(r.URL.Path, "/api/v4/users/7/impersonation_tokens/"), "%d", &id)
			if tk, ok := f.tokens[id]; ok {
				tk.Active = false
				tk.Revoked = true
			}
			f.deleted = append(f.deleted, id)
			w.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected %s %s", r.Method, r.URL.String())
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	return f, srv
}

func TestGitLabAgent_MintDefaultScopes(t *testing.T) {
	f, srv := newFakeGitLab(t)
	defer srv.Close()
	b := NewGitLabAgentBackend(srv.URL, "admin-token", "moltbot", []string{"api", "read_repository", "write_repository"})
	fixed := time.Date(2026, 11, 7, 23, 0, 0, 0, time.UTC)
	b.now = func() time.Time { return fixed }

	cred, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{Scopes: []string{"api"}, RequestID: "abc"})
	if err != nil {
		t.Fatalf("mint: %v", err)
	}
	if cred.Token == "" || cred.LeaseTTL != time.Hour {
		t.Fatalf("bad credential: %+v", cred)
	}
	if cred.Metadata["user"] != "moltbot" || cred.Metadata["type"] != "impersonation_token" {
		t.Errorf("bad metadata: %v", cred.Metadata)
	}
	body := f.created[0]
	wantName := fmt.Sprintf("jit-%d-abc", fixed.Add(time.Hour).Unix())
	if body["name"] != wantName {
		t.Errorf("name = %v, want %s", body["name"], wantName)
	}
	if body["expires_at"] != "2026-11-09" {
		t.Errorf("expires_at = %v, want 2026-11-09", body["expires_at"])
	}
	if got := fmt.Sprint(body["scopes"]); got != "[api read_repository write_repository]" {
		t.Errorf("scopes = %s", got)
	}
}

func TestGitLabAgent_RejectsScopeOutsideAllowlist(t *testing.T) {
	_, srv := newFakeGitLab(t)
	defer srv.Close()
	b := NewGitLabAgentBackend(srv.URL, "admin-token", "moltbot", []string{"api", "read_repository", "write_repository"})
	if _, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{Scopes: []string{"sudo"}}); err == nil {
		t.Fatal("expected sudo scope to be rejected")
	}
	if _, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{Scopes: []string{"api", "admin_mode"}}); err == nil {
		t.Fatal("expected admin_mode scope to be rejected")
	}
}

func TestGitLabAgent_SweepRevokesOnlyExpiredJITTokens(t *testing.T) {
	f, srv := newFakeGitLab(t)
	defer srv.Close()
	b := NewGitLabAgentBackend(srv.URL, "admin-token", "moltbot", []string{"api"})
	now := time.Date(2026, 11, 7, 23, 0, 0, 0, time.UTC)
	b.now = func() time.Time { return now }

	if _, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{RequestID: "a"}); err != nil {
		t.Fatal(err)
	}
	if _, err := b.MintCredential("gitlab-moltbot", 1, 10*time.Minute, MintOptions{RequestID: "b"}); err != nil {
		t.Fatal(err)
	}
	// A token not created by JIT must never be touched.
	f.mu.Lock()
	f.tokens[1] = &gitlabImpersonationToken{ID: 1, Name: "manual-token", Active: true}
	f.mu.Unlock()

	if n, err := b.Sweep(); err != nil || n != 0 {
		t.Fatalf("sweep before expiry: n=%d err=%v", n, err)
	}
	now = now.Add(15 * time.Minute)
	if n, err := b.Sweep(); err != nil || n != 1 {
		t.Fatalf("sweep after 15m: n=%d err=%v", n, err)
	}
	now = now.Add(time.Hour)
	if n, err := b.Sweep(); err != nil || n != 1 {
		t.Fatalf("sweep after 75m: n=%d err=%v", n, err)
	}
	for _, id := range f.deleted {
		if id == 1 {
			t.Fatal("sweep revoked a non-JIT token")
		}
	}
}

func TestGitLabAgent_UnknownUser(t *testing.T) {
	_, srv := newFakeGitLab(t)
	defer srv.Close()
	b := NewGitLabAgentBackend(srv.URL, "admin-token", "nobody", []string{"api"})
	if _, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{}); err == nil {
		t.Fatal("expected error for unknown user")
	}
}

func TestExpiryFromName(t *testing.T) {
	if _, ok := expiryFromName("other"); ok {
		t.Error("non-jit name parsed")
	}
	if ts, ok := expiryFromName("jit-1794100000-req"); !ok || ts.Unix() != 1794100000 {
		t.Errorf("parse failed: %v %v", ts, ok)
	}
}
