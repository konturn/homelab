package backend

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// mockVaultReader implements VaultSecretReader for tests.
type mockVaultReader struct {
	secrets map[string]map[string]string
}

func (m *mockVaultReader) ReadSecret(path string) (map[string]string, error) {
	s, ok := m.secrets[path]
	if !ok {
		return nil, fmt.Errorf("no secret at %s", path)
	}
	return s, nil
}

// mockVaultMinter implements VaultTokenMinter for tests.
type mockVaultMinter struct {
	token   string
	leaseID string
	err     error
}

func (m *mockVaultMinter) MintToken(resource string, tier int, ttl time.Duration) (string, string, error) {
	return m.token, m.leaseID, m.err
}

// --- HomeAssistant Tests ---

func TestHomeAssistantBackend_MintCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/auth/token" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}

		if err := r.ParseForm(); err != nil {
			t.Errorf("parse form: %v", err)
		}
		if r.FormValue("grant_type") != "refresh_token" {
			t.Errorf("expected grant_type=refresh_token, got %s", r.FormValue("grant_type"))
		}
		if r.FormValue("refresh_token") != "test-refresh-token" {
			t.Errorf("unexpected refresh_token: %s", r.FormValue("refresh_token"))
		}
		if r.FormValue("client_id") != "test-client-id" {
			t.Errorf("unexpected client_id: %s", r.FormValue("client_id"))
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"access_token": "ha-access-token-123",
			"token_type":   "Bearer",
			"expires_in":   1800,
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/homeassistant": {
				"refresh_token": "test-refresh-token",
				"client_id":     "test-client-id",
			},
		},
	}

	b := NewHomeAssistantBackend(server.URL, reader)
	cred, err := b.MintCredential("homeassistant", 2, 30*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "ha-access-token-123" {
		t.Errorf("expected token ha-access-token-123, got %s", cred.Token)
	}
	if cred.LeaseTTL != 30*time.Minute {
		t.Errorf("expected TTL 30m, got %s", cred.LeaseTTL)
	}
	if cred.Metadata["backend"] != "homeassistant" {
		t.Errorf("expected backend homeassistant, got %s", cred.Metadata["backend"])
	}
}

func TestHomeAssistantBackend_MintCredential_Error(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"error": "invalid_grant"}`))
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/homeassistant": {
				"refresh_token": "bad-token",
				"client_id":     "test-client-id",
			},
		},
	}

	b := NewHomeAssistantBackend(server.URL, reader)
	_, err := b.MintCredential("homeassistant", 2, 30*time.Minute)
	if err == nil {
		t.Fatal("expected error from bad HA response")
	}
}

func TestHomeAssistantBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"message": "API running."}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	reader := &mockVaultReader{secrets: map[string]map[string]string{}}
	b := NewHomeAssistantBackend(server.URL, reader)

	if err := b.Health(); err != nil {
		t.Errorf("Health() failed: %v", err)
	}
}

// --- Grafana Tests ---

func TestGrafanaBackend_MintCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/serviceaccounts/42/tokens" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if r.Header.Get("Authorization") != "Bearer grafana-admin-token" {
			t.Errorf("unexpected auth header: %s", r.Header.Get("Authorization"))
		}

		var body map[string]interface{}
		json.NewDecoder(r.Body).Decode(&body)
		if name, ok := body["name"].(string); !ok || name == "" {
			t.Error("expected non-empty name in body")
		}
		if _, ok := body["expires"].(string); !ok {
			t.Error("expected expires in body")
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"id":  1,
			"key": "glsa_grafana-ephemeral-token",
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/grafana": {
				"jit_admin_token":    "grafana-admin-token",
				"service_account_id": "42",
			},
		},
	}

	b := NewGrafanaBackend(server.URL, reader)
	cred, err := b.MintCredential("grafana", 0, 5*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "glsa_grafana-ephemeral-token" {
		t.Errorf("expected token glsa_grafana-ephemeral-token, got %s", cred.Token)
	}
	if cred.LeaseTTL != 5*time.Minute {
		t.Errorf("expected TTL 5m, got %s", cred.LeaseTTL)
	}
	if cred.Metadata["service_account_id"] != "42" {
		t.Errorf("expected service_account_id 42, got %s", cred.Metadata["service_account_id"])
	}
}

func TestGrafanaBackend_MintCredential_Error(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Write([]byte(`{"message": "Forbidden"}`))
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/grafana": {
				"jit_admin_token":    "bad-token",
				"service_account_id": "42",
			},
		},
	}

	b := NewGrafanaBackend(server.URL, reader)
	_, err := b.MintCredential("grafana", 0, 5*time.Minute)
	if err == nil {
		t.Fatal("expected error from bad grafana response")
	}
}

func TestGrafanaBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/health" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"database": "ok"}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	reader := &mockVaultReader{secrets: map[string]map[string]string{}}
	b := NewGrafanaBackend(server.URL, reader)

	if err := b.Health(); err != nil {
		t.Errorf("Health() failed: %v", err)
	}
}

// --- InfluxDB Tests ---

func TestInfluxDBBackend_MintCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v2/authorizations" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method != http.MethodPost {
			t.Errorf("expected POST, got %s", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		if r.Header.Get("Authorization") != "Token influx-admin-token" {
			t.Errorf("unexpected auth header: %s", r.Header.Get("Authorization"))
		}

		var body map[string]interface{}
		json.NewDecoder(r.Body).Decode(&body)
		if body["orgID"] != "org-123" {
			t.Errorf("expected orgID org-123, got %v", body["orgID"])
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"id":    "auth-id-456",
			"token": "influx-ephemeral-token",
		})
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/influxdb": {
				"admin_token": "influx-admin-token",
				"org_id":      "org-123",
			},
		},
	}

	b := NewInfluxDBBackend(server.URL, reader)
	cred, err := b.MintCredential("influxdb", 0, 5*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "influx-ephemeral-token" {
		t.Errorf("expected token influx-ephemeral-token, got %s", cred.Token)
	}
	if cred.LeaseTTL != 5*time.Minute {
		t.Errorf("expected TTL 5m, got %s", cred.LeaseTTL)
	}
	if cred.Metadata["org_id"] != "org-123" {
		t.Errorf("expected org_id org-123, got %s", cred.Metadata["org_id"])
	}
}

func TestInfluxDBBackend_MintCredential_Error(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		w.Write([]byte(`{"message": "forbidden"}`))
	}))
	defer server.Close()

	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/influxdb": {
				"admin_token": "bad-token",
				"org_id":      "org-123",
			},
		},
	}

	b := NewInfluxDBBackend(server.URL, reader)
	_, err := b.MintCredential("influxdb", 0, 5*time.Minute)
	if err == nil {
		t.Fatal("expected error from bad influxdb response")
	}
}

func TestInfluxDBBackend_Health(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte(`{"status": "pass"}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer server.Close()

	reader := &mockVaultReader{secrets: map[string]map[string]string{}}
	b := NewInfluxDBBackend(server.URL, reader)

	if err := b.Health(); err != nil {
		t.Errorf("Health() failed: %v", err)
	}
}

// --- Static Backend Tests ---

func TestStaticBackend_MintCredential(t *testing.T) {
	minter := &mockVaultMinter{
		token:   "hvs.test-vault-token",
		leaseID: "accessor-123",
	}

	b := NewStaticBackend(minter)
	cred, err := b.MintCredential("radarr", 1, 15*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token != "hvs.test-vault-token" {
		t.Errorf("expected token hvs.test-vault-token, got %s", cred.Token)
	}
	if cred.Metadata["backend"] != "static" {
		t.Errorf("expected backend static, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["lease_id"] != "accessor-123" {
		t.Errorf("expected lease_id accessor-123, got %s", cred.Metadata["lease_id"])
	}
}

func TestStaticBackend_MintCredential_Error(t *testing.T) {
	minter := &mockVaultMinter{
		err: fmt.Errorf("vault unavailable"),
	}

	b := NewStaticBackend(minter)
	_, err := b.MintCredential("radarr", 1, 15*time.Minute)
	if err == nil {
		t.Fatal("expected error from vault")
	}
}

func TestStaticBackend_Health(t *testing.T) {
	b := NewStaticBackend(&mockVaultMinter{})
	if err := b.Health(); err != nil {
		t.Errorf("Health() should always return nil, got: %v", err)
	}
}

// --- Registry Tests ---

func TestRegistry_DynamicBackendSelection(t *testing.T) {
	minter := &mockVaultMinter{token: "vault-token", leaseID: "acc-1"}
	reader := &mockVaultReader{secrets: map[string]map[string]string{}}

	// Only register grafana as dynamic
	r := NewRegistry(minter, reader, "", "https://grafana.example.com", "", "", "")

	if !r.IsDynamic("grafana") {
		t.Error("expected grafana to be dynamic")
	}
	if r.IsDynamic("plex") {
		t.Error("expected plex to NOT be dynamic (URL not set)")
	}
	if r.IsDynamic("radarr") {
		t.Error("expected radarr to NOT be dynamic")
	}
}

func TestRegistry_FallbackToStatic(t *testing.T) {
	minter := &mockVaultMinter{token: "vault-token", leaseID: "acc-1"}
	reader := &mockVaultReader{secrets: map[string]map[string]string{}}

	r := NewRegistry(minter, reader, "", "", "", "", "")

	// All should be static (no dynamic URLs configured)
	b := r.For("radarr")
	cred, err := b.MintCredential("radarr", 1, 15*time.Minute)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}
	if cred.Metadata["backend"] != "static" {
		t.Errorf("expected static backend, got %s", cred.Metadata["backend"])
	}
}

// --- Vault Secret Reader missing field tests ---

func TestHomeAssistantBackend_MissingVaultField(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/homeassistant": {
				"refresh_token": "token",
				// missing client_id
			},
		},
	}

	b := NewHomeAssistantBackend("http://localhost", reader)
	_, err := b.MintCredential("homeassistant", 2, 30*time.Minute)
	if err == nil {
		t.Fatal("expected error for missing client_id")
	}
}

func TestGrafanaBackend_MissingVaultField(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/grafana": {
				"jit_admin_token": "token",
				// missing service_account_id
			},
		},
	}

	b := NewGrafanaBackend("http://localhost", reader)
	_, err := b.MintCredential("grafana", 0, 5*time.Minute)
	if err == nil {
		t.Fatal("expected error for missing service_account_id")
	}
}

func TestInfluxDBBackend_MissingVaultField(t *testing.T) {
	reader := &mockVaultReader{
		secrets: map[string]map[string]string{
			"homelab/data/docker/influxdb": {
				"admin_token": "token",
				// missing org_id
			},
		},
	}

	b := NewInfluxDBBackend("http://localhost", reader)
	_, err := b.MintCredential("influxdb", 0, 5*time.Minute)
	if err == nil {
		t.Fatal("expected error for missing org_id")
	}
}
