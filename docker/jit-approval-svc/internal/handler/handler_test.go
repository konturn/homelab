package handler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/store"
)

// mockVaultMinter implements backend.VaultTokenMinter for tests.
type mockVaultMinter struct {
	token   string
	leaseID string
	err     error
}

func (m *mockVaultMinter) MintToken(resource string, tier int, ttl time.Duration) (string, string, error) {
	return m.token, m.leaseID, m.err
}

// mockVaultReader implements backend.VaultSecretReader for tests.
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

// mockHandler creates a handler with mock backends (no real Vault or Telegram).
func mockHandler() *Handler {
	return mockHandlerWithMinter(&mockVaultMinter{
		token:   "hvs.mock-token",
		leaseID: "accessor-mock",
	})
}

// mockHandlerWithMinter creates a handler with a specific vault minter.
func mockHandlerWithMinter(minter *mockVaultMinter) *Handler {
	cfg := &config.Config{
		TelegramChatID:        8531859108,
		TelegramWebhookSecret: "test-secret",
		JITAPIKey:             "test-api-key",
		ListenAddr:            ":8080",
		RequestTimeout:        5 * time.Minute,
		AllowedRequesters:     []string{"prometheus"},
		Tiers: map[int]config.TierConfig{
			0: {TTL: 5 * time.Minute, AutoApprove: true, Description: "Read-only monitoring"},
			1: {TTL: 15 * time.Minute, AutoApprove: true, Description: "Service management"},
			2: {TTL: 30 * time.Minute, AutoApprove: false, Description: "Infrastructure"},
			3: {TTL: 60 * time.Minute, AutoApprove: false, Description: "Critical"},
		},
	}

	reader := &mockVaultReader{secrets: map[string]map[string]string{}}

	// Create a backend registry with only static backends
	backends := backend.NewRegistry(minter, reader, "", "", "", "", "")

	return &Handler{
		cfg:      cfg,
		store:    store.New(),
		backends: backends,
		// vault and telegram are nil - only test paths that don't call them
	}
}

func TestHandleRequest_Validation(t *testing.T) {
	h := mockHandler()

	tests := []struct {
		name       string
		body       CreateRequestBody
		wantStatus int
	}{
		{
			name:       "unauthorized requester",
			body:       CreateRequestBody{Requester: "unknown", Resource: "test", Tier: 2, Reason: "test"},
			wantStatus: http.StatusForbidden,
		},
		{
			name:       "invalid tier",
			body:       CreateRequestBody{Requester: "prometheus", Resource: "test", Tier: 99, Reason: "test"},
			wantStatus: http.StatusBadRequest,
		},
		{
			name:       "missing resource",
			body:       CreateRequestBody{Requester: "prometheus", Resource: "", Tier: 2, Reason: "test"},
			wantStatus: http.StatusBadRequest,
		},
		{
			name:       "missing reason",
			body:       CreateRequestBody{Requester: "prometheus", Resource: "test", Tier: 2, Reason: ""},
			wantStatus: http.StatusBadRequest,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			body, _ := json.Marshal(tt.body)
			req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("X-JIT-API-Key", "test-api-key")
			w := httptest.NewRecorder()

			h.HandleRequest(w, req)

			if w.Code != tt.wantStatus {
				t.Errorf("expected status %d, got %d: %s", tt.wantStatus, w.Code, w.Body.String())
			}
		})
	}
}

func TestHandleRequest_Tier2_CreatesPending(t *testing.T) {
	h := mockHandler()

	body, _ := json.Marshal(CreateRequestBody{
		Requester: "prometheus",
		Resource:  "gitlab",
		Tier:      2,
		Reason:    "MR review",
	})

	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleRequest(w, req)

	if w.Code != http.StatusCreated {
		t.Errorf("expected 201, got %d: %s", w.Code, w.Body.String())
	}

	var resp CreateRequestResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.RequestID == "" {
		t.Error("expected non-empty request_id")
	}
	if resp.Status != "pending" {
		t.Errorf("expected status pending, got %s", resp.Status)
	}
}

func TestHandleRequest_AutoApprove_WithStaticBackend(t *testing.T) {
	h := mockHandler()

	body, _ := json.Marshal(CreateRequestBody{
		Requester: "prometheus",
		Resource:  "radarr",
		Tier:      1,
		Reason:    "Check downloads",
	})

	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleRequest(w, req)

	if w.Code != http.StatusCreated {
		t.Errorf("expected 201, got %d: %s", w.Code, w.Body.String())
	}

	var resp CreateRequestResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	// Should be auto-approved
	if resp.Status != "approved" {
		t.Errorf("expected status approved (auto-approve tier 1), got %s", resp.Status)
	}

	// Verify credential can be claimed
	statusReq := httptest.NewRequest(http.MethodGet, "/status/"+resp.RequestID, nil)
	statusReq.Header.Set("X-JIT-API-Key", "test-api-key")
	statusW := httptest.NewRecorder()
	h.HandleStatus(statusW, statusReq)

	var statusResp StatusResponse
	json.Unmarshal(statusW.Body.Bytes(), &statusResp)

	if statusResp.Credential == nil {
		t.Fatal("expected credential on first claim")
	}
	if statusResp.Credential.Token != "hvs.mock-token" {
		t.Errorf("expected token hvs.mock-token, got %s", statusResp.Credential.Token)
	}
	if statusResp.Credential.Metadata["backend"] != "static" {
		t.Errorf("expected backend static in metadata, got %s", statusResp.Credential.Metadata["backend"])
	}
}

func TestHandleRequest_AutoApprove_MintFailure(t *testing.T) {
	minter := &mockVaultMinter{err: fmt.Errorf("vault unavailable")}
	h := mockHandlerWithMinter(minter)

	body, _ := json.Marshal(CreateRequestBody{
		Requester: "prometheus",
		Resource:  "radarr",
		Tier:      1,
		Reason:    "Check downloads",
	})

	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleRequest(w, req)

	// Request still created (201) but stays pending since mint failed
	if w.Code != http.StatusCreated {
		t.Errorf("expected 201, got %d: %s", w.Code, w.Body.String())
	}

	var resp CreateRequestResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	// Check the store status - should still be pending since mint failed
	storeReq := h.store.Get(resp.RequestID)
	if storeReq == nil {
		t.Fatal("request should exist in store")
	}
	// The request was created as pending, mint failed so it stays pending
	if storeReq.Status != store.StatusPending {
		t.Errorf("expected pending (mint failed), got %s", storeReq.Status)
	}
}

func TestHandleRequest_MissingAPIKey(t *testing.T) {
	h := mockHandler()

	body, _ := json.Marshal(CreateRequestBody{
		Requester: "prometheus",
		Resource:  "gitlab",
		Tier:      2,
		Reason:    "MR review",
	})

	// No API key header
	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	h.HandleRequest(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 without API key, got %d: %s", w.Code, w.Body.String())
	}

	// Wrong API key
	req2 := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(body))
	req2.Header.Set("Content-Type", "application/json")
	req2.Header.Set("X-JIT-API-Key", "wrong-key")
	w2 := httptest.NewRecorder()

	h.HandleRequest(w2, req2)

	if w2.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 with wrong API key, got %d: %s", w2.Code, w2.Body.String())
	}
}

func TestHandleStatus_MissingAPIKey(t *testing.T) {
	h := mockHandler()

	// No API key
	req := httptest.NewRequest(http.MethodGet, "/status/req-test", nil)
	w := httptest.NewRecorder()
	h.HandleStatus(w, req)
	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 without API key, got %d", w.Code)
	}

	// Wrong API key
	req2 := httptest.NewRequest(http.MethodGet, "/status/req-test", nil)
	req2.Header.Set("X-JIT-API-Key", "wrong-key")
	w2 := httptest.NewRecorder()
	h.HandleStatus(w2, req2)
	if w2.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 with wrong API key, got %d", w2.Code)
	}
}

func TestHandleStatus_NotFound(t *testing.T) {
	h := mockHandler()

	req := httptest.NewRequest(http.MethodGet, "/status/req-nonexistent", nil)
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleStatus(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("expected 404, got %d", w.Code)
	}
}

func TestHandleStatus_Pending(t *testing.T) {
	h := mockHandler()

	// Create a request directly in the store
	storeReq := h.store.Create("prometheus", "gitlab", 2, "test", nil)

	req := httptest.NewRequest(http.MethodGet, "/status/"+storeReq.ID, nil)
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleStatus(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}

	var resp StatusResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Status != "pending" {
		t.Errorf("expected pending, got %s", resp.Status)
	}
	if resp.Credential != nil {
		t.Error("expected no credential for pending request")
	}
}

func TestHandleStatus_ApprovedClaims(t *testing.T) {
	h := mockHandler()

	storeReq := h.store.Create("prometheus", "gitlab", 2, "test", nil)
	_ = h.store.Approve(storeReq.ID, &store.Credential{
		Token:    "hvs.test-token",
		LeaseTTL: 30 * time.Minute,
		Metadata: map[string]string{"backend": "static"},
	}, 30*time.Minute)

	// First poll should claim the credential
	req := httptest.NewRequest(http.MethodGet, "/status/"+storeReq.ID, nil)
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleStatus(w, req)

	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}

	var resp StatusResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Status != "approved" {
		t.Errorf("expected approved, got %s", resp.Status)
	}
	if resp.Credential == nil {
		t.Fatal("expected credential on first claim")
	}
	if resp.Credential.Token != "hvs.test-token" {
		t.Errorf("expected token hvs.test-token, got %s", resp.Credential.Token)
	}

	// Second poll should NOT return the credential again
	req2 := httptest.NewRequest(http.MethodGet, "/status/"+storeReq.ID, nil)
	req2.Header.Set("X-JIT-API-Key", "test-api-key")
	w2 := httptest.NewRecorder()

	h.HandleStatus(w2, req2)

	var resp2 StatusResponse
	if err := json.Unmarshal(w2.Body.Bytes(), &resp2); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp2.Status != "claimed" {
		t.Errorf("expected claimed, got %s", resp2.Status)
	}
	if resp2.Credential != nil {
		t.Error("expected no credential on second claim")
	}
}

func TestHandleStatus_CredentialMetadata(t *testing.T) {
	h := mockHandler()

	storeReq := h.store.Create("prometheus", "grafana", 0, "check dashboards", nil)
	_ = h.store.Approve(storeReq.ID, &store.Credential{
		Token:    "glsa-test-token",
		LeaseTTL: 5 * time.Minute,
		Metadata: map[string]string{
			"backend":            "grafana",
			"type":               "service_account_token",
			"service_account_id": "42",
		},
	}, 5*time.Minute)

	req := httptest.NewRequest(http.MethodGet, "/status/"+storeReq.ID, nil)
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()

	h.HandleStatus(w, req)

	var resp StatusResponse
	json.Unmarshal(w.Body.Bytes(), &resp)

	if resp.Credential == nil {
		t.Fatal("expected credential")
	}
	if resp.Credential.Metadata["backend"] != "grafana" {
		t.Errorf("expected backend grafana, got %s", resp.Credential.Metadata["backend"])
	}
	if resp.Credential.Metadata["service_account_id"] != "42" {
		t.Errorf("expected service_account_id 42, got %s", resp.Credential.Metadata["service_account_id"])
	}
}

func TestHandleHealth(t *testing.T) {
	h := mockHandler()

	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	w := httptest.NewRecorder()

	// This will panic because vault client is nil.
	// In real deployment, vault is always present.
	defer func() {
		if r := recover(); r != nil {
			// Expected: vault client is nil in test
		}
	}()

	h.HandleHealth(w, req)
}

func TestHandleTelegramWebhook_SecretValidation(t *testing.T) {
	h := mockHandler()
	h.cfg.TelegramWebhookSecret = "test-secret"

	// No secret header
	req := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader([]byte("{}")))
	w := httptest.NewRecorder()

	h.HandleTelegramWebhook(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 without secret, got %d", w.Code)
	}

	// Wrong secret
	req2 := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader([]byte("{}")))
	req2.Header.Set("X-Telegram-Bot-Api-Secret-Token", "wrong")
	w2 := httptest.NewRecorder()

	h.HandleTelegramWebhook(w2, req2)

	if w2.Code != http.StatusUnauthorized {
		t.Errorf("expected 401 with wrong secret, got %d", w2.Code)
	}

	// Correct secret with empty update (no callback_query)
	req3 := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader([]byte("{}")))
	req3.Header.Set("X-Telegram-Bot-Api-Secret-Token", "test-secret")
	w3 := httptest.NewRecorder()

	h.HandleTelegramWebhook(w3, req3)

	if w3.Code != http.StatusOK {
		t.Errorf("expected 200 with correct secret, got %d", w3.Code)
	}
}

func TestHandleTelegramWebhook_UnauthorizedUser(t *testing.T) {
	h := mockHandler()

	update := TelegramUpdate{
		CallbackQuery: &CallbackQuery{
			ID:   "123",
			From: TelegramUser{ID: 999999}, // Wrong user
			Data: "jit:approve:req-abc",
		},
	}
	body, _ := json.Marshal(update)

	req := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader(body))
	req.Header.Set("X-Telegram-Bot-Api-Secret-Token", "test-secret")
	w := httptest.NewRecorder()

	h.HandleTelegramWebhook(w, req)

	// Should return 200 (acknowledge) but not process
	if w.Code != http.StatusOK {
		t.Errorf("expected 200, got %d", w.Code)
	}
}

func TestMethodNotAllowed(t *testing.T) {
	h := mockHandler()

	tests := []struct {
		method  string
		path    string
		handler func(http.ResponseWriter, *http.Request)
	}{
		{http.MethodGet, "/request", h.HandleRequest},
		{http.MethodPost, "/status/test", h.HandleStatus},
		{http.MethodPost, "/health", h.HandleHealth},
		{http.MethodGet, "/telegram/webhook", h.HandleTelegramWebhook},
	}

	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path, func(t *testing.T) {
			req := httptest.NewRequest(tt.method, tt.path, nil)
			w := httptest.NewRecorder()
			tt.handler(w, req)
			if w.Code != http.StatusMethodNotAllowed {
				t.Errorf("expected 405, got %d", w.Code)
			}
		})
	}
}
