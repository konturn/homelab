package backend

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestGmailBackend_MintCredential_Read(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/x-www-form-urlencoded" {
			t.Errorf("expected Content-Type application/x-www-form-urlencoded, got %s", ct)
		}

		if err := r.ParseForm(); err != nil {
			t.Fatalf("parse form: %v", err)
		}
		if r.FormValue("grant_type") != "refresh_token" {
			t.Errorf("expected grant_type=refresh_token, got %s", r.FormValue("grant_type"))
		}
		if r.FormValue("client_id") != "test-client-id" {
			t.Errorf("unexpected client_id: %s", r.FormValue("client_id"))
		}
		if r.FormValue("client_secret") != "test-client-secret" {
			t.Errorf("unexpected client_secret: %s", r.FormValue("client_secret"))
		}
		if r.FormValue("refresh_token") != "test-refresh-read" {
			t.Errorf("unexpected refresh_token: %s", r.FormValue("refresh_token"))
		}
		if r.FormValue("scope") != "https://www.googleapis.com/auth/gmail.readonly" {
			t.Errorf("unexpected scope: %s", r.FormValue("scope"))
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(googleTokenResponse{
			AccessToken: "ya29.gmail-read-token",
			TokenType:   "Bearer",
			ExpiresIn:   3600,
			Scope:       "https://www.googleapis.com/auth/gmail.readonly",
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/email/google-oauth": {
				"client_id":          "test-client-id",
				"client_secret":      "test-client-secret",
				"refresh_token_read": "test-refresh-read",
				"refresh_token_send": "test-refresh-send",
			},
		},
	}

	b := NewGmailBackend(server.URL, reader, GmailScopeRead)
	cred, err := b.MintCredential("gmail-read", 2, 30*time.Minute, MintOptions{})
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "ya29.gmail-read-token" {
		t.Errorf("expected token ya29.gmail-read-token, got %s", cred.Token)
	}
	if cred.LeaseTTL != time.Hour {
		t.Errorf("expected TTL 1h (from expires_in), got %s", cred.LeaseTTL)
	}
	if cred.Metadata["backend"] != "gmail" {
		t.Errorf("expected backend gmail, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["type"] != "oauth2_access_token" {
		t.Errorf("expected type oauth2_access_token, got %s", cred.Metadata["type"])
	}
	if cred.Metadata["scope"] != "gmail.readonly" {
		t.Errorf("expected scope gmail.readonly, got %s", cred.Metadata["scope"])
	}
	if cred.Metadata["email"] != "konoahko@gmail.com" {
		t.Errorf("expected email konoahko@gmail.com, got %s", cred.Metadata["email"])
	}
}

func TestGmailBackend_MintCredential_Send(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Fatalf("parse form: %v", err)
		}
		if r.FormValue("refresh_token") != "test-refresh-send" {
			t.Errorf("unexpected refresh_token: %s", r.FormValue("refresh_token"))
		}
		if r.FormValue("scope") != "https://www.googleapis.com/auth/gmail.send" {
			t.Errorf("unexpected scope: %s", r.FormValue("scope"))
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(googleTokenResponse{
			AccessToken: "ya29.gmail-send-token",
			TokenType:   "Bearer",
			ExpiresIn:   3600,
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/email/google-oauth": {
				"client_id":          "test-client-id",
				"client_secret":      "test-client-secret",
				"refresh_token_read": "test-refresh-read",
				"refresh_token_send": "test-refresh-send",
			},
		},
	}

	b := NewGmailBackend(server.URL, reader, GmailScopeSend)
	cred, err := b.MintCredential("gmail-send", 2, 30*time.Minute, MintOptions{})
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "ya29.gmail-send-token" {
		t.Errorf("expected token ya29.gmail-send-token, got %s", cred.Token)
	}
	if cred.Metadata["scope"] != "gmail.send" {
		t.Errorf("expected scope gmail.send, got %s", cred.Metadata["scope"])
	}
}

func TestGmailBackend_MintCredential_APIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		w.Write([]byte(`{"error": "invalid_grant"}`))
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/email/google-oauth": {
				"client_id":          "test-client-id",
				"client_secret":      "test-client-secret",
				"refresh_token_read": "bad-token",
			},
		},
	}

	b := NewGmailBackend(server.URL, reader, GmailScopeRead)
	_, err := b.MintCredential("gmail-read", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for 400 response")
	}
}

func TestGmailBackend_MintCredential_MissingVaultSecret(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/email/google-oauth": {
				"client_id":     "test-client-id",
				"client_secret": "test-client-secret",
				// missing refresh_token_read
			},
		},
	}

	b := NewGmailBackend("https://oauth2.googleapis.com/token", reader, GmailScopeRead)
	_, err := b.MintCredential("gmail-read", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for missing refresh_token_read")
	}
}

func TestGmailBackend_MintCredential_MissingClientSecret(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/email/google-oauth": {
				"client_id":          "test-client-id",
				"refresh_token_read": "test-token",
				// missing client_secret
			},
		},
	}

	b := NewGmailBackend("https://oauth2.googleapis.com/token", reader, GmailScopeRead)
	_, err := b.MintCredential("gmail-read", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for missing client_secret")
	}
}

func TestGmailBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusMethodNotAllowed) // Expected for GET on token endpoint
	}))
	defer server.Close()

	b := NewGmailBackend(server.URL, nil, GmailScopeRead)
	if err := b.Health(); err != nil {
		t.Errorf("expected healthy (405 is ok), got: %v", err)
	}
}

func TestGmailBackend_Health_ServerError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()

	b := NewGmailBackend(server.URL, nil, GmailScopeRead)
	if err := b.Health(); err == nil {
		t.Error("expected error for 500 response")
	}
}
