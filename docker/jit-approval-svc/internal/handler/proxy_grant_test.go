package handler

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/proxygrant"
	"github.com/nkontur/jit-approval-svc/internal/store"
)

// fakeGrants stands in for internal/proxygrant: a catalog of proxied
// resources and their template tiers, recording every Mint.
type fakeGrants struct {
	mu    sync.Mutex
	tiers map[string]map[string]int // resource -> template -> tier
	mints []proxygrant.MintRequest
}

func newFakeGrants() *fakeGrants {
	return &fakeGrants{tiers: map[string]map[string]int{
		// radarr is still a static resource too (transition); prowlarr is
		// treated as moved by removing it from the static set in tests via
		// a resource that has no static path: "proxyonly".
		"radarr":    {"read": 1, "write": 1, "remove-title": 2},
		"proxyonly": {"read": 1, "danger": 3},
	}}
}

func (f *fakeGrants) IsProxied(r string) bool { _, ok := f.tiers[r]; return ok }
func (f *fakeGrants) Tier(r string, tmpl []string) (int, error) {
	ts, ok := f.tiers[r]
	if !ok {
		return 0, fmt.Errorf("not proxied")
	}
	if len(tmpl) == 0 {
		return 0, fmt.Errorf("no templates")
	}
	max := 1
	for _, t := range tmpl {
		tier, ok := ts[t]
		if !ok {
			return 0, fmt.Errorf("unknown template %q", t)
		}
		if tier > max {
			max = tier
		}
	}
	return max, nil
}
func (f *fakeGrants) Describe(string, []string) string { return "described" }
func (f *fakeGrants) Mint(m proxygrant.MintRequest) (*backend.Credential, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.mints = append(f.mints, m)
	return &backend.Credential{Token: "jitp_fake", LeaseTTL: m.TTL, Metadata: map[string]string{"type": proxygrant.CredentialType}}, nil
}
func (f *fakeGrants) take() []proxygrant.MintRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := f.mints
	f.mints = nil
	return out
}

func proxyHandler() (*Handler, *fakeGrants, *recorder) {
	h, rec := propHandler()
	f := newFakeGrants()
	h.SetProxy(f)
	return h, f, rec
}

func sha(k string) string { s := sha256.Sum256([]byte(k)); return hex.EncodeToString(s[:]) }

func grantReq(t *testing.T, h *Handler, requester string, body map[string]interface{}) (int, map[string]interface{}) {
	t.Helper()
	raw, _ := json.Marshal(body)
	w := doRequest(h, keyFor(requester), raw)
	var out map[string]interface{}
	_ = json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

func TestProxyGrantTierFromTemplates(t *testing.T) {
	h, f, _ := proxyHandler()

	// noah: read is tier 1 (auto); remove-title raises the request to tier 2
	// (auto-fyi for noah), never lower.
	code, out := grantReq(t, h, "openclaw-noah", map[string]interface{}{
		"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"remove-title"}})
	m := f.take()
	if code != http.StatusCreated || len(m) != 1 || m[0].Tier != 2 || m[0].Approval != proxygrant.ApprovalAuto {
		t.Fatalf("noah remove-title: %d %v %+v", code, out, m)
	}
	if m[0].Requester != "openclaw-noah" || m[0].RequesterKeySHA256 != sha(keyFor("openclaw-noah")) {
		t.Fatalf("grant not bound to the authenticated requester: %+v", m[0])
	}

	// ops: the same request is tier 2, which needs Noah's approval: pending,
	// nothing minted.
	code, out = grantReq(t, h, "openclaw-ops", map[string]interface{}{
		"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"read", "remove-title"}})
	if code != http.StatusCreated || out["status"] != "pending" || len(f.take()) != 0 {
		t.Fatalf("ops remove-title: %d %v", code, out)
	}

	// ops: a tier-3 template is denied outright.
	code, _ = grantReq(t, h, "openclaw-ops", map[string]interface{}{
		"resource": "proxyonly", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"danger"}})
	if code != http.StatusForbidden || len(f.take()) != 0 {
		t.Fatalf("ops tier 3: %d", code)
	}

	// Default template is read.
	code, _ = grantReq(t, h, "openclaw-ops", map[string]interface{}{
		"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "proxy_grant"})
	if m := f.take(); code != http.StatusCreated || len(m) != 1 || strings.Join(m[0].Templates, ",") != "read" {
		t.Fatalf("default templates: %d %+v", code, m)
	}
}

func TestProxyGrantValidation(t *testing.T) {
	h, f, rec := proxyHandler()
	cases := []map[string]interface{}{
		{"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"admin"}},
		{"resource": "grafana", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"read"}},
		{"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "vault_token"},
		// moved to the proxy: no other credential type
		{"resource": "proxyonly", "tier": 1, "reason": "x"},
	}
	for _, c := range cases {
		if code, out := grantReq(t, h, "openclaw-noah", c); code != http.StatusBadRequest {
			t.Errorf("%v: %d %v", c, code, out)
		}
	}
	if len(f.take()) != 0 || len(rec.take()) != 0 {
		t.Fatal("something was minted")
	}
	// radarr has moved: no static token for it any more.
	if code, _ := grantReq(t, h, "openclaw-noah", map[string]interface{}{"resource": "radarr", "tier": 1, "reason": "x"}); code != http.StatusBadRequest {
		t.Fatalf("static radarr after cutover: %d", code)
	}
	// A resource with both (during a migration) keeps its static path.
	f.tiers["mqtt"] = map[string]int{"read": 1}
	if code, _ := grantReq(t, h, "openclaw-noah", map[string]interface{}{"resource": "mqtt", "tier": 1, "reason": "x"}); code != http.StatusCreated {
		t.Fatalf("static path during a migration: %d", code)
	}
}

func TestProxyGrantApprovedByTelegram(t *testing.T) {
	h, f, _ := proxyHandler()
	code, out := grantReq(t, h, "openclaw-ops", map[string]interface{}{
		"resource": "radarr", "tier": 1, "reason": "x", "credential_type": "proxy_grant", "scopes": []string{"remove-title"}})
	if code != http.StatusCreated || out["status"] != "pending" {
		t.Fatalf("%d %v", code, out)
	}
	req := h.store.Get(out["request_id"].(string))
	h.handleApprove(req)
	m := f.take()
	if len(m) != 1 || m[0].Approval != proxygrant.ApprovalApproved || m[0].Requester != "openclaw-ops" ||
		m[0].RequesterKeySHA256 != sha(keyFor("openclaw-ops")) || m[0].Tier != 2 {
		t.Fatalf("approved mint %+v", m)
	}
	if got := h.store.Get(req.ID); got.Status != store.StatusApproved {
		t.Fatalf("status %s", got.Status)
	}
	ttl, _ := h.cfg.TTLFor("radarr", 2)
	if m[0].TTL <= 0 || m[0].TTL > ttl {
		t.Fatalf("ttl %v", m[0].TTL)
	}
}

// FuzzProxyGrantRequest: whatever the body, a grant is minted only when the
// matrix lets the authenticated requester mint at the templates' tier
// without a human, bound to that requester's key, with approval "auto".
func FuzzProxyGrantRequest(f *testing.F) {
	f.Add("openclaw-noah", "radarr", "remove-title", 1, "")
	f.Add("openclaw-ops", "radarr", "read,remove-title", 1, "")
	f.Add("openclaw-ops", "proxyonly", "danger", 3, "openclaw-noah")
	f.Add("stranger", "radarr", "read", 1, "")
	f.Fuzz(func(t *testing.T, requester, resource, scopes string, tier int, claimed string) {
		if _, ok := map[string]bool{"openclaw-noah": true, "openclaw-ops": true, "stranger": true}[requester]; !ok {
			return
		}
		h, fg, _ := proxyHandler()
		body := map[string]interface{}{"resource": resource, "tier": tier, "reason": "fuzz", "credential_type": "proxy_grant"}
		if scopes != "" {
			body["scopes"] = strings.Split(scopes, ",")
		}
		if claimed != "" {
			body["requester"] = claimed
		}
		raw, _ := json.Marshal(body)
		w := doRequest(h, keyFor(requester), raw)
		if w.Code >= 500 {
			t.Fatalf("status %d", w.Code)
		}
		for _, m := range fg.take() {
			if m.Requester != requester || m.RequesterKeySHA256 != sha(keyFor(requester)) {
				t.Fatalf("grant for %s bound to %s", requester, m.Requester)
			}
			want, err := fg.Tier(m.Resource, m.Templates)
			if err != nil || m.Tier < want {
				t.Fatalf("minted tier %d below the templates' %d", m.Tier, want)
			}
			if !h.cfg.DecisionFor(requester, m.Tier).Mints() || m.Tier >= config.MaxTier {
				t.Fatalf("minted for %s at tier %d without approval", requester, m.Tier)
			}
			if m.Approval != proxygrant.ApprovalAuto || m.TTL <= 0 || m.TTL > 30*time.Minute {
				t.Fatalf("mint %+v", m)
			}
		}
	})
}
