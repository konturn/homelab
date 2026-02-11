package backend

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestTailscaleBackend_MintCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v2/oauth/token" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/x-www-form-urlencoded" {
			t.Errorf("expected Content-Type application/x-www-form-urlencoded, got %s", ct)
		}

		if err := r.ParseForm(); err != nil {
			t.Fatalf("failed to parse form: %v", err)
		}
		if v := r.FormValue("client_id"); v != "ts-client-id" {
			t.Errorf("expected client_id ts-client-id, got %s", v)
		}
		if v := r.FormValue("client_secret"); v != "ts-client-secret" {
			t.Errorf("expected client_secret ts-client-secret, got %s", v)
		}
		if v := r.FormValue("grant_type"); v != "client_credentials" {
			t.Errorf("expected grant_type client_credentials, got %s", v)
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(tailscaleTokenResponse{
			AccessToken: "tskey-access-12345",
			TokenType:   "Bearer",
			ExpiresIn:   3600,
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/infrastructure/tailscale": {
				"oauth_client_id":     "ts-client-id",
				"oauth_client_secret": "ts-client-secret",
			},
		},
	}

	b := NewTailscaleBackend(server.URL, reader)
	cred, err := b.MintCredential("tailscale", 2, 30*time.Minute, MintOptions{})
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "tskey-access-12345" {
		t.Errorf("expected token tskey-access-12345, got %s", cred.Token)
	}
	if cred.LeaseTTL != time.Hour {
		t.Errorf("expected TTL 1h (from expires_in), got %s", cred.LeaseTTL)
	}
	if cred.Metadata["backend"] != "tailscale" {
		t.Errorf("expected backend tailscale, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["type"] != "oauth_access_token" {
		t.Errorf("expected type oauth_access_token, got %s", cred.Metadata["type"])
	}
}

func TestTailscaleBackend_MintCredential_APIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"error": "invalid_client"}`))
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/infrastructure/tailscale": {
				"oauth_client_id":     "bad-id",
				"oauth_client_secret": "bad-secret",
			},
		},
	}

	b := NewTailscaleBackend(server.URL, reader)
	_, err := b.MintCredential("tailscale", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for 401 response")
	}
}

func TestTailscaleBackend_MintCredential_MissingVaultSecret(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/infrastructure/tailscale": {
				"oauth_client_id": "ts-id",
				// missing oauth_client_secret
			},
		},
	}

	b := NewTailscaleBackend("https://api.tailscale.com", reader)
	_, err := b.MintCredential("tailscale", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for missing client_secret")
	}
}

func TestTailscaleBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized) // Expected without auth
	}))
	defer server.Close()

	b := NewTailscaleBackend(server.URL, nil)
	if err := b.Health(); err != nil {
		t.Errorf("expected healthy (401 is ok), got: %v", err)
	}
}

func TestTailscaleBackend_Health_ServerError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	b := NewTailscaleBackend(server.URL, nil)
	if err := b.Health(); err == nil {
		t.Error("expected error for 500 response")
	}
}
