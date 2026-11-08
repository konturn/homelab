package backend

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// Regression: a project override is validated and path-escaped, so it can
// only ever name a project, and unknown scopes are refused before any API
// call.
func TestGitLabBackend_ProjectIDAndScopesValidated(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		paths = append(paths, r.URL.EscapedPath())
		mu.Unlock()
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(gitlabTokenResponse{ID: 1, Token: "glpat-fake", Name: "jit"})
	}))
	defer srv.Close()
	b := NewGitLabBackend(srv.URL, "fake-admin", "4")

	for _, bad := range []MintOptions{
		{ProjectID: "4/../../groups/1"},
		{ProjectID: "../users/1"},
		{ProjectID: "4?private_token=x"},
		{ProjectID: "group/.."},
		{Scopes: []string{"sudo"}},
		{Scopes: []string{"api", "admin_mode"}},
	} {
		if _, err := b.MintCredential("gitlab", 2, 30*time.Minute, bad); err == nil {
			t.Errorf("%+v accepted", bad)
		}
	}
	if len(paths) != 0 {
		t.Fatalf("API called for rejected requests: %v", paths)
	}

	for opts, want := range map[string]string{
		"":              "/api/v4/projects/4/access_tokens",
		"42":            "/api/v4/projects/42/access_tokens",
		"homelab/infra": "/api/v4/projects/homelab%2Finfra/access_tokens",
	} {
		paths = nil
		if _, err := b.MintCredential("gitlab", 2, 30*time.Minute, MintOptions{ProjectID: opts}); err != nil {
			t.Fatalf("project %q: %v", opts, err)
		}
		if len(paths) != 1 || paths[0] != want {
			t.Errorf("project %q: called %v, want %s", opts, paths, want)
		}
	}
}
