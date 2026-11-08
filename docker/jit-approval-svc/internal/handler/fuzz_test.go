package handler

// Fuzz and property tests for POST /request and the Telegram webhook:
// request parsing never panics, and nothing is minted without a human
// approval except auto-approved (tier 1) requests for resources whose
// minimum tier is 1, with a TTL capped at the resource/tier maximum.
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

// propHandler builds a handler with production tiers and TTL overrides and a
// recording backend for every dynamic resource. Telegram is nil, so
// approval-required requests just stay pending. The rate limit is out of
// the way so properties see every request.
func propHandler() (*Handler, *recorder) {
	rec := &recorder{}
	cfg := &config.Config{
		TelegramChatID:        1000001,
		TelegramWebhookSecret: "fake-webhook-secret",
		JITAPIKey:             "fake-api-key",
		RequestTimeout:        5 * time.Minute,
		AllowedRequesters:     []string{"prometheus"},
		Tiers: map[int]config.TierConfig{
			1: {TTL: 15 * time.Minute, AutoApprove: true},
			2: {TTL: 30 * time.Minute, AutoApprove: false},
			3: {TTL: 60 * time.Minute, AutoApprove: false},
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
	return &Handler{
		cfg:      cfg,
		store:    store.New(),
		backends: reg,
		limiter:  ratelimit.New(1<<30, time.Minute),
	}, rec
}

// checkNoUnapprovedMint asserts the auto-approval property for one
// /request call. body is what the handler would have decoded (nil if the
// body was not valid JSON).
func checkNoUnapprovedMint(t *testing.T, h *Handler, calls []mintCall, body *CreateRequestBody, code int, resp []byte) {
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
	tc, ok := h.cfg.Tiers[c.tier]
	if !ok || !tc.AutoApprove {
		t.Fatalf("minted without approval at tier %d (%+v)", c.tier, c)
	}
	if c.resource != body.Resource || c.tier != body.Tier {
		t.Fatalf("minted %+v for request %q tier %d", c, body.Resource, body.Tier)
	}
	min := vault.MinTierForResource(c.resource)
	if strings.HasPrefix(c.backend, "dyn:") {
		// A dynamic backend auto-minted: the resource must be a known tier-1
		// resource (unknown names never reach a dynamic backend).
		if min != 1 {
			t.Fatalf("auto-approved %q (minimum tier %d) via %s", c.resource, min, c.backend)
		}
	} else if min > c.tier {
		t.Fatalf("static fallback auto-minted %q below its minimum tier %d", c.resource, min)
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
	for _, s := range seeds {
		f.Add([]byte(s), true)
	}
	f.Add([]byte(seeds[0]), false)

	h, rec := propHandler()
	f.Fuzz(func(t *testing.T, raw []byte, authed bool) {
		key := ""
		if authed {
			key = "fake-api-key"
		}
		w := doRequest(h, key, raw)
		calls := rec.take()
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
		checkNoUnapprovedMint(t, h, calls, bp, w.Code, w.Body.Bytes())
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
	f.Fuzz(func(t *testing.T, resource string, tier int, ttl, scopes string) {
		h, rec := propHandler()
		b := CreateRequestBody{Requester: "prometheus", Resource: resource, Tier: tier, Reason: "fuzz", TTL: ttl}
		if scopes != "" {
			b.Scopes = strings.Split(scopes, ",")
		}
		raw, _ := json.Marshal(b)
		w := doRequest(h, "fake-api-key", raw)
		// json.Marshal replaces invalid UTF-8; compare against what was sent.
		var sent CreateRequestBody
		_ = json.Unmarshal(raw, &sent)
		checkNoUnapprovedMint(t, h, rec.take(), &sent, w.Code, w.Body.Bytes())
	})
}

// TestAutoApprovalMatrix is the exhaustive form of the property for every
// known resource and tier 0-4: auto-approval (a credential in the create
// response, minted without Telegram) happens exactly for tier 1 requests on
// tier-1 resources. In the GitLab family that is only the agent's own
// identity, gitlab-moltbot; the Maintainer project token ("gitlab") and
// every other gitlab-* name need approval or are refused.
func TestAutoApprovalMatrix(t *testing.T) {
	resources := append([]string{"gitlab-root", "gitlab-moltbot2", "GITLAB-MOLTBOT", "plex", "mqtt", "pihole", "ipmi"}, dynamicResources...)
	for _, res := range resources {
		for tier := 0; tier <= 4; tier++ {
			h, rec := propHandler()
			raw, _ := json.Marshal(CreateRequestBody{Requester: "prometheus", Resource: res, Tier: tier, Reason: "matrix"})
			w := doRequest(h, "fake-api-key", raw)
			calls := rec.take()
			min := vault.MinTierForResource(res)
			wantAuto := tier == 1 && min == 1
			gotAuto := len(calls) == 1 && strings.HasPrefix(calls[0].backend, "dyn:")
			if res == "plex" || res == "mqtt" {
				// Tier-1 resources without a dynamic backend go to the static
				// Vault fallback, still auto-approved.
				gotAuto = len(calls) == 1 && calls[0].backend == "static"
			}
			if gotAuto != wantAuto {
				t.Errorf("%s tier %d (min %d): auto-minted=%v want %v (status %d, calls %+v)",
					res, tier, min, gotAuto, wantAuto, w.Code, calls)
			}
			if strings.HasPrefix(strings.ToLower(res), "gitlab") && gotAuto && res != "gitlab-moltbot" {
				t.Errorf("%s tier %d auto-approved; only gitlab-moltbot may be", res, tier)
			}
			if min == 0 && len(calls) > 0 && calls[0].backend != "static" {
				t.Errorf("unknown resource %q reached dynamic backend %s", res, calls[0].backend)
			}
		}
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
