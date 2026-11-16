package handler

// Fuzz and property tests for POST /request and the Telegram webhook:
// request parsing never panics, the requester is whoever owns the key, and
// nothing is minted without a human approval unless the approval matrix
// cell for that requester and the resource's tier says so (never tier 3),
// with a TTL capped at the resource/tier maximum.
//
// `go test ./...` runs the Fuzz* functions over their seed corpus only. To
// fuzz one target:
//
//	go test ./internal/handler -run '^$' -fuzz '^FuzzHandleRequest$' -fuzztime 2m
//
// All keys, tokens and IDs are fake.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/ratelimit"
	"github.com/nkontur/jit-approval-svc/internal/store"
	"github.com/nkontur/jit-approval-svc/internal/vault"
)

// mintCall records one MintCredential call.
type mintCall struct {
	backend  string
	resource string
	tier     int
	ttl      time.Duration
	opts     backend.MintOptions
}

type recorder struct {
	mu    sync.Mutex
	calls []mintCall
}

func (r *recorder) add(c mintCall) {
	r.mu.Lock()
	r.calls = append(r.calls, c)
	r.mu.Unlock()
}

func (r *recorder) take() []mintCall {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := r.calls
	r.calls = nil
	return out
}

// recBackend is a dynamic backend that always succeeds and records the call.
type recBackend struct {
	name string
	rec  *recorder
}

func (b *recBackend) MintCredential(resource string, tier int, ttl time.Duration, opts backend.MintOptions) (*backend.Credential, error) {
	b.rec.add(mintCall{backend: b.name, resource: resource, tier: tier, ttl: ttl, opts: opts})
	return &backend.Credential{Token: "fake-token", LeaseTTL: ttl, Metadata: map[string]string{"backend": b.name}}, nil
}
func (b *recBackend) Health() error { return nil }

// recMinter is the static fallback's Vault minter; it records and succeeds
// (the real vault.Client refuses resources it has no policy for, which
// TestStaticFallbackPolicyGate covers).
type recMinter struct{ rec *recorder }

func (m *recMinter) MintToken(resource string, tier int, ttl time.Duration) (string, string, error) {
	m.rec.add(mintCall{backend: "static", resource: resource, tier: tier, ttl: ttl})
	return "hvs.fake", "acc-fake", nil
}
func (m *recMinter) MintDynamicToken(policyName string, ttl time.Duration, requestID string) (string, string, error) {
	return "hvs.fake", "acc-fake", nil
}

// dynamicResources are the resources main.go gives a dynamic backend with
// the default config (GITLAB_AGENT_USER=moltbot).
var dynamicResources = []string{
	"homeassistant", "grafana", "influxdb", "gitlab", "paperless", "tailscale",
	"ssh-router", "ssh-satellite", "ssh-zwave", "ssh-nkontur", "ssh-konoahko", "ssh-konturn",
	"ssh-macmini", "ssh-router-elevated", "ssh-zwave-elevated", "ssh-satellite-elevated",
	"ssh-macmini-elevated", "gmail-read", "gmail-send", "vault", "gitlab-moltbot",
}

// requesterKeys are the fake keys propHandler accepts and who they belong
// to. "stranger" has a key but no row in the approval matrix.
var requesterKeys = map[string]string{
	"fake-api-key":      "prometheus",
	"fake-noah-key":     "openclaw-noah",
	"fake-ops-key":      "openclaw-ops",
	"fake-stranger-key": "stranger",
}

// keyFor returns the fake key of a requester.
func keyFor(requester string) string {
	for k, r := range requesterKeys {
		if r == requester {
			return k
		}
	}
	panic("no key for " + requester)
}

// propHandler builds a handler with production tiers, TTL overrides and the
// default approval matrix, and a recording backend for every dynamic
// resource. Telegram is nil, so approval-required requests just stay
// pending; FYI lines are recorded in *fyis. The rate limit is out of the way
// so properties see every request.
func propHandler() (*Handler, *recorder) {
	h, rec, _ := propHandlerFYI()
	return h, rec
}

func propHandlerFYI() (*Handler, *recorder, *[]string) {
	rec := &recorder{}
	var keys []config.RequesterKey
	for k, r := range requesterKeys {
		if r != "prometheus" {
			keys = append(keys, config.RequesterKey{Name: r, Hash: config.HashKey(k)})
		}
	}
	cfg := &config.Config{
		TelegramChatID:        1000001,
		TelegramWebhookSecret: "fake-webhook-secret",
		JITAPIKey:             "fake-api-key",
		RequesterKeys:         keys,
		ApprovalMatrix:        config.DefaultApprovalMatrix(),
		RequestTimeout:        5 * time.Minute,
		Tiers: map[int]config.TierConfig{
			1: {TTL: 15 * time.Minute},
			2: {TTL: 30 * time.Minute},
			3: {TTL: 30 * time.Minute},
		},
		ResourceTTLOverrides: map[string]time.Duration{
			"ssh-nkontur":    8 * time.Hour,
			"ssh-konoahko":   8 * time.Hour,
			"ssh-konturn":    8 * time.Hour,
			"gitlab-moltbot": time.Hour,
		},
	}
	reg := backend.NewRegistry(&recMinter{rec: rec}, nil, "", "", "", "", "", "", "", "", nil, nil, "", "")
	for _, r := range dynamicResources {
		reg.Register(r, &recBackend{name: "dyn:" + r, rec: rec})
	}
	fyis := &[]string{}
	var mu sync.Mutex
	return &Handler{
		cfg:      cfg,
		store:    store.New(),
		backends: reg,
		limiter:  ratelimit.New(1<<30, time.Minute),
		fyi: func(text string) error {
			mu.Lock()
			*fyis = append(*fyis, text)
			mu.Unlock()
			return nil
		},
	}, rec, fyis
}

// effectiveTier is the tier a request is decided at: the requested tier,
// raised to the resource's minimum.
func effectiveTier(resource string, tier int) int {
	if min := vault.MinTierForResource(resource); min > tier {
		return min
	}
	return tier
}

// checkNoUnapprovedMint asserts the auto-approval property for one
// /request call made with requester's key. body is what the handler would
// have decoded (nil if the body was not valid JSON).
func checkNoUnapprovedMint(t *testing.T, h *Handler, requester string, calls []mintCall, body *CreateRequestBody, code int, resp []byte) {
	t.Helper()
	if code >= 500 && code != http.StatusServiceUnavailable {
		t.Fatalf("status %d: %s", code, resp)
	}
	if len(calls) == 0 {
		return
	}
	if len(calls) > 1 {
		t.Fatalf("%d mints for one request: %+v", len(calls), calls)
	}
	c := calls[0]
	if body == nil {
		t.Fatalf("minted %+v for an undecodable body", c)
	}
	if code != http.StatusCreated {
		t.Fatalf("minted %+v but answered %d", c, code)
	}
	if body.Requester != "" && body.Requester != requester {
		t.Fatalf("minted %+v for %s on a request claiming to be %q", c, requester, body.Requester)
	}
	if _, ok := h.cfg.Tiers[c.tier]; !ok {
		t.Fatalf("minted at unknown tier %d (%+v)", c.tier, c)
	}
	if !h.cfg.DecisionFor(requester, c.tier).Mints() {
		t.Fatalf("minted without approval for %s at tier %d (%+v)", requester, c.tier, c)
	}
	if c.tier >= config.MaxTier {
		t.Fatalf("minted tier %d without approval (%+v)", c.tier, c)
	}
	if c.resource != body.Resource || c.tier != effectiveTier(body.Resource, body.Tier) {
		t.Fatalf("minted %+v for request %q tier %d", c, body.Resource, body.Tier)
	}
	min := vault.MinTierForResource(c.resource)
	if min > c.tier {
		t.Fatalf("auto-minted %q below its minimum tier %d", c.resource, min)
	}
	if strings.HasPrefix(c.backend, "dyn:") && min == 0 {
		t.Fatalf("unknown resource %q reached %s", c.resource, c.backend)
	}
	max, err := h.cfg.TTLFor(c.resource, c.tier)
	if err != nil {
		t.Fatal(err)
	}
	if c.ttl <= 0 || c.ttl > max {
		t.Fatalf("minted %q with ttl %v (max %v)", c.resource, c.ttl, max)
	}
}

func doRequest(h *Handler, apiKey string, raw []byte) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/request", bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	if apiKey != "" {
		req.Header.Set("X-JIT-API-Key", apiKey)
	}
	w := httptest.NewRecorder()
	h.HandleRequest(w, req)
	return w
}

// FuzzHandleRequest feeds arbitrary bodies to POST /request. It must never
// panic or 500, and must never mint a credential without approval except
// for an auto-approve tier on a resource whose minimum tier allows it.
func FuzzHandleRequest(f *testing.F) {
	seeds := []string{
		`{"requester":"prometheus","resource":"grafana","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"gitlab-moltbot","tier":1,"reason":"x","ttl":"5m"}`,
		`{"requester":"prometheus","resource":"gitlab","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"gitlab","tier":2,"reason":"x","project_id":"4/../../groups/1"}`,
		`{"requester":"prometheus","resource":"ssh-router","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"ssh-router-elevated","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"vault","tier":1,"reason":"x","vault_paths":[{"path":"homelab/data/x","capabilities":["read"]}]}`,
		`{"requester":"prometheus","resource":"Gitlab-moltbot","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"gitlab-mоltbot","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":1,"reason":"x","ttl":"99999h"}`,
		`{"requester":"prometheus","resource":"grafana","tier":1,"reason":"x","ttl":"-5m"}`,
		`{"requester":"prometheus","resource":"grafana","tier":1,"reason":"x","ttl":"9223372036854775807ns"}`,
		`{"requester":"prometheus","resource":"grafana","tier":1.5,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":"1","reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":1,"tier":2,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","Tier":1,"reason":"x"}`,
		`{"requester":"prometheus\u0000","resource":"grafana","tier":1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":0,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":-1,"reason":"x"}`,
		`{"requester":"prometheus","resource":"grafana","tier":4294967297,"reason":"x"}`,
		`{"requester":"prometheus","resource":"influxdb","tier":1,"reason":"x","scopes":["write"]}`,
		`[]`, `null`, `{}`, `{"requester":`, "\xff\xfe", ``,
	}
	keys := []string{"", "fake-api-key", "fake-noah-key", "fake-ops-key", "fake-stranger-key", "fake-noah-key ", "wrong"}
	for _, s := range seeds {
		for i := range keys {
			f.Add([]byte(s), uint8(i))
		}
		f.Add([]byte(strings.ReplaceAll(s, `"prometheus"`, `"openclaw-noah"`)), uint8(2))
		f.Add([]byte(strings.ReplaceAll(s, `"prometheus"`, `"openclaw-noah"`)), uint8(3))
	}

	h, rec := propHandler()
	f.Fuzz(func(t *testing.T, raw []byte, which uint8) {
		key := keys[int(which)%len(keys)]
		w := doRequest(h, key, raw)
		calls := rec.take()
		requester, authed := requesterKeys[key]
		if !authed {
			if w.Code != http.StatusUnauthorized || len(calls) != 0 {
				t.Fatalf("unauthenticated request: %d, %d mints", w.Code, len(calls))
			}
			return
		}
		var body CreateRequestBody
		var bp *CreateRequestBody
		if json.NewDecoder(bytes.NewReader(raw)).Decode(&body) == nil {
			bp = &body
		}
		checkNoUnapprovedMint(t, h, requester, calls, bp, w.Code, w.Body.Bytes())
		if requester == "stranger" && len(calls) > 0 {
			t.Fatalf("requester without a matrix row minted %+v", calls)
		}
		// Keep the store from filling up across iterations.
		if h.store.Count() > 900 {
			h.store = store.New()
		}
	})
}

// FuzzHandleRequestFields drives the same property with structured inputs,
// so the fuzzer explores resource names, tiers and TTL strings directly
// instead of JSON syntax.
func FuzzHandleRequestFields(f *testing.F) {
	f.Add("grafana", 1, "", "")
	f.Add("gitlab-moltbot", 1, "2h", "api")
	f.Add("gitlab", 1, "", "api")
	f.Add("gitlab-root", 1, "", "")
	f.Add("ssh-macmini-elevated", 1, "", "")
	f.Add("vault", 1, "", "")
	f.Add("homeassistant", 1, "1ns", "")
	f.Add("gmail-send", 1, "", "")
	f.Add("paperless", 1, "", "")
	f.Add("grafana", 2, "", "")
	f.Add("unknown-thing", 1, "", "")
	f.Add("ssh-nkontur", 3, "100h", "")
	f.Add("ssh-router", 2, "", "")
	f.Add("ipmi", 1, "", "")
	requesters := []string{"prometheus", "openclaw-noah", "openclaw-ops", "stranger"}
	f.Fuzz(func(t *testing.T, resource string, tier int, ttl, scopes string) {
		for _, requester := range requesters {
			h, rec := propHandler()
			b := CreateRequestBody{Resource: resource, Tier: tier, Reason: "fuzz", TTL: ttl}
			if scopes != "" {
				b.Scopes = strings.Split(scopes, ",")
			}
			raw, _ := json.Marshal(b)
			w := doRequest(h, keyFor(requester), raw)
			// json.Marshal replaces invalid UTF-8; compare against what was sent.
			var sent CreateRequestBody
			_ = json.Unmarshal(raw, &sent)
			checkNoUnapprovedMint(t, h, requester, rec.take(), &sent, w.Code, w.Body.Bytes())
		}
	})
}

// TestAutoApprovalMatrix is the exhaustive form of the property for every
// requester, every known resource and tier 0-4: a credential is minted
// without Telegram exactly when the requester's matrix cell for the
// effective tier (the requested tier raised to the resource's) is auto or
// auto-fyi. A denied request is refused (403) and leaves nothing pending
// that a button could approve. In the GitLab family only the agent's own
// identity, gitlab-moltbot, is ever auto-minted.
func TestAutoApprovalMatrix(t *testing.T) {
	resources := append([]string{"gitlab-root", "gitlab-moltbot2", "GITLAB-MOLTBOT", "plex", "mqtt", "pihole", "ipmi"}, dynamicResources...)
	for _, requester := range []string{"prometheus", "openclaw-noah", "openclaw-ops", "stranger"} {
		for _, res := range resources {
			for tier := 0; tier <= 4; tier++ {
				h, rec, fyis := propHandlerFYI()
				b := CreateRequestBody{Resource: res, Tier: tier, Reason: "matrix"}
				if res == "vault" {
					b.VaultPaths = []VaultPathRequest{{Path: "homelab/data/x", Capabilities: []string{"read"}}}
				}
				raw, _ := json.Marshal(b)
				w := doRequest(h, keyFor(requester), raw)
				calls := rec.take()
				eff := effectiveTier(res, tier)
				if vault.MinTierForResource(res) == 0 {
					// Unknown names (case variants included) have no
					// dynamic backend; the real static fallback refuses
					// them (TestStaticFallbackPolicyGate).
					for _, c := range calls {
						if c.backend != "static" {
							t.Errorf("%s %s reached %s", requester, res, c.backend)
						}
					}
					continue
				}
				validTier := tier >= 1 && tier <= config.MaxTier
				decision := h.cfg.DecisionFor(requester, eff)
				wantAuto := validTier && decision.Mints()
				gotAuto := len(calls) == 1
				if gotAuto != wantAuto {
					t.Errorf("%s %s tier %d (effective %d, %s): auto-minted=%v want %v (status %d, calls %+v)",
						requester, res, tier, eff, decision, gotAuto, wantAuto, w.Code, calls)
				}
				if gotAuto && calls[0].tier != eff {
					t.Errorf("%s %s tier %d: minted at tier %d, want %d", requester, res, tier, calls[0].tier, eff)
				}
				if validTier && decision == config.DecisionDeny {
					if w.Code != http.StatusForbidden || h.store.Count() != 0 {
						t.Errorf("%s %s tier %d: denied cell answered %d with %d stored requests",
							requester, res, tier, w.Code, h.store.Count())
					}
				}
				wantFYI := wantAuto && decision == config.DecisionAutoFYI
				if (len(*fyis) == 1) != wantFYI || len(*fyis) > 1 {
					t.Errorf("%s %s tier %d: %d FYI lines, want fyi=%v", requester, res, tier, len(*fyis), wantFYI)
				}
				if wantFYI && !strings.Contains((*fyis)[0], res) {
					t.Errorf("FYI line %q does not name %s", (*fyis)[0], res)
				}
				// In the GitLab family only gitlab-moltbot is tier 1; every
				// other name is minted only where the tier-2 cell allows it.
				if strings.HasPrefix(strings.ToLower(res), "gitlab") && gotAuto && res != "gitlab-moltbot" && eff < 2 {
					t.Errorf("%s %s tier %d auto-approved at tier %d; only gitlab-moltbot is tier 1", requester, res, tier, eff)
				}
				if eff >= config.MaxTier && gotAuto {
					t.Errorf("%s %s tier %d: tier %d minted without approval", requester, res, tier, eff)
				}
			}
		}
	}
}

// TestOpsNeverGetsTier3: every tier-3 resource, requested by the ops agent
// at any tier, is refused before anything is stored, so no Telegram button
// can approve it either.
func TestOpsNeverGetsTier3(t *testing.T) {
	var tier3 []string
	for _, res := range append([]string{"ipmi"}, dynamicResources...) {
		if vault.MinTierForResource(res) == config.MaxTier {
			tier3 = append(tier3, res)
		}
	}
	if len(tier3) < 6 {
		t.Fatalf("only %d tier-3 resources: %v", len(tier3), tier3)
	}
	for _, res := range tier3 {
		for tier := 0; tier <= 4; tier++ {
			h, rec := propHandler()
			raw, _ := json.Marshal(CreateRequestBody{Resource: res, Tier: tier, Reason: "ops"})
			w := doRequest(h, "fake-ops-key", raw)
			if calls := rec.take(); len(calls) != 0 {
				t.Fatalf("ops got %s at tier %d: %+v", res, tier, calls)
			}
			if h.store.Count() != 0 {
				t.Fatalf("ops request for %s tier %d left a request to approve", res, tier)
			}
			if tier >= 1 && tier <= config.MaxTier && w.Code != http.StatusForbidden {
				t.Fatalf("ops %s tier %d: status %d, want 403", res, tier, w.Code)
			}
		}
	}
}

// TestUnknownRequesterAlwaysDenied: an unknown key is 401, and a known key
// whose requester has no matrix row is 403 for every resource and tier.
func TestUnknownRequesterAlwaysDenied(t *testing.T) {
	for _, res := range append([]string{"plex", "unknown-thing"}, dynamicResources...) {
		for tier := 1; tier <= config.MaxTier; tier++ {
			h, rec := propHandler()
			raw, _ := json.Marshal(CreateRequestBody{Resource: res, Tier: tier, Reason: "x"})
			if w := doRequest(h, "fake-stranger-key", raw); w.Code != http.StatusForbidden {
				t.Errorf("stranger %s tier %d: status %d", res, tier, w.Code)
			}
			if w := doRequest(h, "not-a-key", raw); w.Code != http.StatusUnauthorized {
				t.Errorf("unknown key %s tier %d: status %d", res, tier, w.Code)
			}
			if calls := rec.take(); len(calls) != 0 || h.store.Count() != 0 {
				t.Errorf("%s tier %d: %d mints, %d stored", res, tier, len(calls), h.store.Count())
			}
		}
	}
}

// TestRequesterFieldCannotImpersonate: the body's requester never changes
// who the broker thinks is asking; naming someone else is refused.
func TestRequesterFieldCannotImpersonate(t *testing.T) {
	for _, claimed := range []string{"openclaw-noah", "prometheus", "OPENCLAW-OPS", "x"} {
		h, rec := propHandler()
		raw, _ := json.Marshal(CreateRequestBody{Requester: claimed, Resource: "gitlab", Tier: 2, Reason: "x"})
		w := doRequest(h, "fake-ops-key", raw)
		if w.Code != http.StatusForbidden || len(rec.take()) != 0 || h.store.Count() != 0 {
			t.Errorf("ops claiming %q: status %d", claimed, w.Code)
		}
	}
	h, _ := propHandler()
	raw, _ := json.Marshal(CreateRequestBody{Requester: "openclaw-ops", Resource: "gitlab", Tier: 2, Reason: "x"})
	if w := doRequest(h, "fake-ops-key", raw); w.Code != http.StatusCreated {
		t.Errorf("ops naming itself: status %d %s", w.Code, w.Body)
	}
}

// TestStatusOnlyForOwnRequests: one requester cannot see or claim another's
// request, approved or not.
func TestStatusOnlyForOwnRequests(t *testing.T) {
	h, _ := propHandler()
	raw, _ := json.Marshal(CreateRequestBody{Resource: "gitlab", Tier: 2, Reason: "x"})
	w := doRequest(h, "fake-ops-key", raw)
	var created CreateRequestResponse
	_ = json.Unmarshal(w.Body.Bytes(), &created)
	if created.RequestID == "" {
		t.Fatalf("no request: %s", w.Body)
	}
	_ = h.store.Approve(created.RequestID, &store.Credential{Token: "secret-token", LeaseTTL: time.Minute}, time.Minute)

	status := func(key string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, "/status/"+created.RequestID, nil)
		req.Header.Set("X-JIT-API-Key", key)
		w := httptest.NewRecorder()
		h.HandleStatus(w, req)
		return w
	}
	for _, key := range []string{"fake-noah-key", "fake-api-key", "fake-stranger-key"} {
		if w := status(key); w.Code != http.StatusNotFound || strings.Contains(w.Body.String(), "secret-token") {
			t.Errorf("%s polled ops's request: %d %s", requesterKeys[key], w.Code, w.Body)
		}
	}
	if w := status("fake-ops-key"); !strings.Contains(w.Body.String(), "secret-token") {
		t.Errorf("owner could not claim: %d %s", w.Code, w.Body)
	}
}

// TestRequestedTTLNeverExceedsMax: for random requested TTLs, the minted TTL
// is min(requested, max) for auto-approved requests, and invalid or
// non-positive TTLs are rejected before anything is minted.
func TestRequestedTTLNeverExceedsMax(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	units := []string{"ns", "us", "ms", "s", "m", "h"}
	for i := 0; i < 400; i++ {
		res := []string{"grafana", "gitlab-moltbot", "influxdb", "ssh-macmini", "gmail-read"}[rng.Intn(5)]
		n := rng.Int63n(1_000_000) - 1000
		ttlStr := fmt.Sprintf("%d%s", n, units[rng.Intn(len(units))])
		h, rec := propHandler()
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: res, Tier: 1, Reason: "ttl", TTL: ttlStr})
		w := doRequest(h, "fake-api-key", raw)
		calls := rec.take()
		parsed, _ := time.ParseDuration(ttlStr)
		max, _ := h.cfg.TTLFor(res, 1)
		if parsed <= 0 {
			if w.Code != http.StatusBadRequest || len(calls) != 0 {
				t.Fatalf("ttl %q: status %d, %d mints", ttlStr, w.Code, len(calls))
			}
			continue
		}
		if len(calls) != 1 {
			t.Fatalf("ttl %q on %s: %d mints, status %d %s", ttlStr, res, len(calls), w.Code, w.Body)
		}
		want := max
		if parsed < max {
			want = parsed
		}
		if calls[0].ttl != want {
			t.Fatalf("ttl %q on %s: minted %v, want %v", ttlStr, res, calls[0].ttl, want)
		}
	}
}

// FuzzTelegramWebhook: arbitrary update bodies and callback data never
// panic, and nothing is approved unless the callback comes from the
// configured chat with the right secret and names a pending request.
func FuzzTelegramWebhook(f *testing.F) {
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"jit:approve:%s"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":42},"data":"jit:approve:%s"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"jit:approve:%s"}}`), false)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"jit:deny:%s"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"jit:approve:%s:extra"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"JIT:approve:%s"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"act:approve:%s"}}`), true)
	f.Add([]byte(`{"update_id":1,"callback_query":{"id":"c1","from":{"id":1000001},"data":"jit:approve:"}}`), true)
	f.Add([]byte(`{"callback_query":null}`), true)
	f.Add([]byte(`{"callback_query":{"from":{"id":"1000001"}}}`), true)
	f.Add([]byte(`garbage`), true)
	f.Fuzz(func(t *testing.T, tmpl []byte, secretOK bool) {
		h, rec := propHandler()
		// A pending tier 2 request the callback may refer to.
		raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: "gitlab", Tier: 2, Reason: "webhook"})
		w := doRequest(h, "fake-api-key", raw)
		var created CreateRequestResponse
		_ = json.Unmarshal(w.Body.Bytes(), &created)
		rec.take()

		body := bytes.ReplaceAll(tmpl, []byte("%s"), []byte(created.RequestID))
		req := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader(body))
		if secretOK {
			req.Header.Set("X-Telegram-Bot-Api-Secret-Token", "fake-webhook-secret")
		}
		ww := httptest.NewRecorder()
		h.HandleTelegramWebhook(ww, req)
		calls := rec.take()
		if ww.Code >= 500 {
			t.Fatalf("status %d", ww.Code)
		}
		if len(calls) == 0 {
			return
		}
		var upd TelegramUpdate
		if !secretOK || json.Unmarshal(body, &upd) != nil || upd.CallbackQuery == nil ||
			upd.CallbackQuery.From.ID != h.cfg.TelegramChatID ||
			upd.CallbackQuery.Data != "jit:approve:"+created.RequestID {
			t.Fatalf("minted %+v from webhook body %q (secret ok %v)", calls, body, secretOK)
		}
		if got := h.store.Get(created.RequestID); got == nil || got.Status != store.StatusApproved {
			t.Fatalf("minted but request not approved")
		}
	})
}

// TestUnknownGitLabAgentUserNeedsApproval: with GITLAB_AGENT_USER set to a
// user other than moltbot, main.go registers the agent backend as
// gitlab-<user>, which has no tier entry and so counts as tier 2. A tier-1
// request for it is raised to tier 2: it waits for approval for the legacy
// and ops requesters, and is never minted silently for anyone.
func TestUnknownGitLabAgentUserNeedsApproval(t *testing.T) {
	for _, user := range []string{"root", "alice", "moltbot2"} {
		res := "gitlab-" + user
		for _, tier := range []int{1, 2} {
			h, rec, fyis := propHandlerFYI()
			h.backends.Register(res, &recBackend{name: "dyn:" + res, rec: rec})

			raw, _ := json.Marshal(CreateRequestBody{Resource: res, Tier: tier, Reason: "agent"})
			w := doRequest(h, "fake-api-key", raw)
			if calls := rec.take(); len(calls) != 0 {
				t.Errorf("%s tier %d: minted %+v without approval", res, tier, calls)
			}
			var resp struct {
				Status string `json:"status"`
			}
			_ = json.Unmarshal(w.Body.Bytes(), &resp)
			if resp.Status != "pending" {
				t.Errorf("%s tier %d: status %d %q, want a pending request", res, tier, w.Code, resp.Status)
			}

			// Noah's agent gets tier 2 with an FYI line, never silently.
			w = doRequest(h, "fake-noah-key", raw)
			if calls := rec.take(); len(calls) != 1 || len(*fyis) != 1 {
				t.Errorf("%s tier %d for noah: %d mints, %d FYI lines (status %d)", res, tier, len(calls), len(*fyis), w.Code)
			}
		}
	}
}
