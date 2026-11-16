package ctl

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/nkontur/jit-proxy/internal/grants"
	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/templates"
)

const key = "control-key-0123456789abcdef0123456789"

func handler(t *testing.T) *Handler {
	t.Helper()
	logger.SetOutput(io.Discard)
	r, err := templates.Parse([]byte(`
resource: radarr
min_tier: 1
upstream: https://radarr.example
auth: {type: header, name: X-Api-Key, vault_path: homelab/data/docker/radarr, field: api_key}
templates:
  read: {min_tier: 1, description: read, allow: [{method: GET, path: /api/v3/queue}]}
`))
	if err != nil {
		t.Fatal(err)
	}
	set := templates.Set{"radarr": r}
	return &Handler{
		KeyHash: sha256.Sum256([]byte(key)), Grants: grants.New(grants.DefaultConfig(), set), Set: set, BootID: "b1",
		Health: func() (bool, map[string]interface{}) { return true, map[string]interface{}{"status": "ok"} },
	}
}

func h256(s string) string { x := sha256.Sum256([]byte(s)); return hex.EncodeToString(x[:]) }

func call(h *Handler, method, path, auth, body string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	if auth != "" {
		r.Header.Set("Authorization", "Bearer "+auth)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func regBody(id string, extra map[string]interface{}) string {
	m := map[string]interface{}{
		"grant_id": id, "secret_sha256": h256("jitp_x" + id), "request_id": "req-0123456789ab",
		"requester": "openclaw-noah", "requester_key_sha256": h256("k"), "resource": "radarr",
		"templates": []string{"read"}, "tier": 1, "approval": "auto",
		"expires_at": time.Now().Add(5 * time.Minute).UTC().Format(time.RFC3339),
	}
	for k, v := range extra {
		m[k] = v
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func TestControlPlaneAuth(t *testing.T) {
	h := handler(t)
	for _, a := range []string{"", "wrong", key + "x"} {
		if w := call(h, "PUT", "/internal/grants/g-000000000001", a, regBody("g-000000000001", nil)); w.Code != 401 {
			t.Errorf("key %q: %d", a, w.Code)
		}
		if w := call(h, "GET", "/internal/catalog", a, ""); w.Code != 401 {
			t.Errorf("catalog with key %q: %d", a, w.Code)
		}
	}
	if w := call(h, "GET", "/health", "", ""); w.Code != 200 || !strings.Contains(w.Body.String(), `"boot_id":"b1"`) {
		t.Errorf("health: %d %s", w.Code, w.Body)
	}
}

func TestRegisterRevoke(t *testing.T) {
	h := handler(t)
	if w := call(h, "PUT", "/internal/grants/g-000000000001", key, regBody("g-000000000001", nil)); w.Code != 201 {
		t.Fatalf("register: %d %s", w.Code, w.Body)
	}
	if w := call(h, "PUT", "/internal/grants/g-000000000002", key, regBody("g-000000000003", nil)); w.Code != 400 {
		t.Errorf("id mismatch: %d", w.Code)
	}
	if w := call(h, "PUT", "/internal/grants/g-000000000004", key, regBody("g-000000000004", map[string]interface{}{"rules": "x"})); w.Code != 400 {
		t.Errorf("unknown field: %d", w.Code)
	}
	if w := call(h, "PUT", "/internal/grants/g-000000000005", key, regBody("g-000000000005", map[string]interface{}{"requester": "openclaw-ops", "tier": 3})); w.Code != 422 {
		t.Errorf("matrix-denied grant: %d", w.Code)
	}
	if w := call(h, "PUT", "/internal/grants/g-000000000001", key, regBody("g-000000000001", map[string]interface{}{"secret_sha256": h256("other")})); w.Code != 409 {
		t.Errorf("conflict: %d", w.Code)
	}
	if _, r := h.Grants.Lookup("jitp_xg-000000000001", "k", "radarr"); r != "" {
		t.Fatalf("registered grant unusable: %s", r)
	}
	if w := call(h, "DELETE", "/internal/grants/g-000000000001", key, ""); w.Code != 204 {
		t.Errorf("revoke: %d", w.Code)
	}
	if _, r := h.Grants.Lookup("jitp_xg-000000000001", "k", "radarr"); r != grants.ReasonUnknown {
		t.Errorf("revoked grant usable: %s", r)
	}
	// A revoked grant cannot be registered again under the same ID.
	if w := call(h, "PUT", "/internal/grants/g-000000000001", key, regBody("g-000000000001", nil)); w.Code != 409 {
		t.Errorf("re-register revoked: %d", w.Code)
	}
	if w := call(h, "DELETE", "/internal/grants", key, ""); w.Code != 400 {
		t.Errorf("bulk revoke without scope: %d", w.Code)
	}
	if w := call(h, "DELETE", "/internal/grants?all=1", key, ""); w.Code != 200 {
		t.Errorf("bulk revoke: %d", w.Code)
	}
	w := call(h, "GET", "/internal/catalog", key, "")
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"read"`) || strings.Contains(w.Body.String(), "vault_path") {
		t.Errorf("catalog: %s", w.Body)
	}
}
