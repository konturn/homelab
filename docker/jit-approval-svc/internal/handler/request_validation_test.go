package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/nkontur/jit-approval-svc/internal/store"
)

func postRequest(h *Handler, raw []byte) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(raw))
	req.Header.Set("X-JIT-API-Key", "test-api-key")
	w := httptest.NewRecorder()
	h.HandleRequest(w, req)
	return w
}

// Regression: an oversized body is refused (413) before decoding into the
// store; it used to be read in full and accepted.
func TestHandleRequest_OversizedBodyRejected(t *testing.T) {
	h := mockHandler()
	raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "gitlab", Tier: 2,
		Reason: strings.Repeat("x", maxBodyBytes)})
	w := postRequest(h, raw)
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status %d, want 413", w.Code)
	}
	if h.store.Count() != 0 {
		t.Fatalf("store has %d requests", h.store.Count())
	}
}

func TestTelegramWebhook_OversizedBodyRejected(t *testing.T) {
	h := mockHandler()
	body := `{"update_id":1,"callback_query":{"id":"c","from":{"id":8531859108},"data":"` + strings.Repeat("x", maxBodyBytes) + `"}}`
	req := httptest.NewRequest(http.MethodPost, "/telegram/webhook", strings.NewReader(body))
	req.Header.Set("X-Telegram-Bot-Api-Secret-Token", "test-secret")
	w := httptest.NewRecorder()
	h.HandleTelegramWebhook(w, req)
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status %d, want 413", w.Code)
	}
}

// Regression: an invalid TTL is rejected before the request is stored or
// counted against the rate limit (it used to leave a pending entry behind).
func TestHandleRequest_InvalidTTLLeavesNoRecord(t *testing.T) {
	h := mockHandler()
	for i, ttl := range []string{"soon", "-5m", "0s", "1x"} {
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "grafana", Tier: 1, Reason: "x", TTL: ttl})
		if w := postRequest(h, raw); w.Code != http.StatusBadRequest {
			t.Fatalf("ttl %q: status %d", ttl, w.Code)
		}
		if h.store.Count() != 0 {
			t.Fatalf("ttl %q (#%d): store has %d requests", ttl, i, h.store.Count())
		}
	}
	// Rate limit is 5/15min: the rejected requests above used no slots.
	for i := 0; i < 5; i++ {
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "grafana", Tier: 1, Reason: "x"})
		if w := postRequest(h, raw); w.Code != http.StatusCreated {
			t.Fatalf("valid request %d: status %d %s", i, w.Code, w.Body)
		}
	}
}

// Regression: project_id becomes part of the GitLab API path. Traversal or
// other path characters are refused; a valid override is shown to the
// approver.
func TestHandleRequest_ProjectIDValidated(t *testing.T) {
	for _, tc := range []struct {
		id   string
		want int
	}{
		{"42", http.StatusCreated},
		{"homelab/infra", http.StatusCreated},
		{"4/../../groups/1", http.StatusBadRequest},
		{"../users/1", http.StatusBadRequest},
		{"4?x=1", http.StatusBadRequest},
		{"4#frag", http.StatusBadRequest},
		{"4%2F..%2F1", http.StatusBadRequest},
		{"4 ", http.StatusBadRequest},
		{"/4", http.StatusBadRequest},
		{"group/..", http.StatusBadRequest},
	} {
		h := mockHandler()
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "gitlab", Tier: 2, Reason: "mr", ProjectID: tc.id})
		w := postRequest(h, raw)
		if w.Code != tc.want {
			t.Errorf("project_id %q: status %d, want %d (%s)", tc.id, w.Code, tc.want, w.Body)
			continue
		}
		if tc.want != http.StatusCreated {
			continue
		}
		var resp CreateRequestResponse
		_ = json.Unmarshal(w.Body.Bytes(), &resp)
		req := h.store.Get(resp.RequestID)
		if got := displayReason(req); !strings.Contains(got, "(project: "+tc.id+")") {
			t.Errorf("approval text %q does not show project %q", got, tc.id)
		}
	}
}

// Regression: scopes for the Maintainer project token are checked, not
// passed through to GitLab.
func TestHandleRequest_GitLabScopesValidated(t *testing.T) {
	for _, tc := range []struct {
		scopes []string
		want   int
	}{
		{nil, http.StatusCreated},
		{[]string{"read_api", "read_repository"}, http.StatusCreated},
		{[]string{"sudo"}, http.StatusBadRequest},
		{[]string{"api", "admin_mode"}, http.StatusBadRequest},
		{[]string{"API"}, http.StatusBadRequest},
		{[]string{""}, http.StatusBadRequest},
	} {
		h := mockHandler()
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "gitlab", Tier: 2, Reason: "mr", Scopes: tc.scopes})
		if w := postRequest(h, raw); w.Code != tc.want {
			t.Errorf("scopes %q: status %d, want %d", tc.scopes, w.Code, tc.want)
		}
	}
}

func TestDisplayReason(t *testing.T) {
	got := displayReason(&store.Request{Reason: "r", SSHHost: "h", ProjectID: "7"})
	if got != "r (host: h) (project: 7)" {
		t.Fatalf("got %q", got)
	}
}
