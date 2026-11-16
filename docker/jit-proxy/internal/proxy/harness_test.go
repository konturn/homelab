package proxy

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nkontur/jit-proxy/internal/grants"
	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/redact"
	"github.com/nkontur/jit-proxy/internal/secrets"
	"github.com/nkontur/jit-proxy/internal/templates"
	"github.com/nkontur/jit-proxy/internal/upstream"
)

// Held upstream credentials in the tests. Every one of them must stay out of
// agent responses and logs.
var held = map[string]secrets.Credential{
	"radarr":   {Secret: "0123456789abcdef0123456789abcdef"},
	"tautulli": {Secret: "fedcba9876543210fedcba9876543210"},
	"nzbget":   {Secret: "nzb-p4ss/word+=", Username: "nzbuser"},
	"deluge":   {Secret: "d3luge-web-pw"},
}

const (
	grantSecret = "jitp_GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG"
	noahKey     = "noah-requester-key-0000000000000000"
	opsKey      = "ops-requester-key-11111111111111111"
)

var testYAML = map[string]string{
	"radarr": `
resource: radarr
min_tier: 1
upstream: https://radarr.test
auth: {type: header, name: X-Api-Key, vault_path: homelab/data/docker/radarr, field: api_key}
deny:
  - {path: "/api/v3/config/**"}
  - {path: "/api/v3/indexer"}
templates:
  read:
    min_tier: 1
    description: read
    allow:
      - {method: GET, path: "/api/v3/queue", query: {page: int, pageSize: {type: int, max: 500}}}
      - {method: GET, path: "/api/v3/movie/{id:int}"}
      - {method: GET, path: "/api/v3/movie/lookup", query: {term: any}, require: [term]}
      - {method: GET, path: "/api/v3/config/host"}
  write:
    min_tier: 1
    description: write
    includes: [read]
    allow:
      - {method: POST, path: "/api/v3/command", json: {name: [MoviesSearch]}, body_max: 4096}
  delete-one:
    min_tier: 1
    single_use: true
    description: delete one
    allow:
      - {method: DELETE, path: "/api/v3/moviefile/{id:int}"}
`,
	"tautulli": `
resource: tautulli
min_tier: 1
upstream: https://tautulli.test
auth: {type: query, name: apikey, vault_path: homelab/data/docker/tautulli, field: api_key}
templates:
  read:
    min_tier: 1
    description: read
    allow:
      - {method: GET, path: "/api/v2", query: {cmd: [get_activity, get_history], length: int}, require: [cmd]}
`,
	"nzbget": `
resource: nzbget
min_tier: 1
upstream: https://nzbget.test
auth: {type: basic, vault_path: homelab/data/docker/nzbget, field: password, username_field: username}
templates:
  read:
    min_tier: 1
    description: read
    allow:
      - {method: POST, path: "/jsonrpc", rpc: {methods: [status, listgroups]}}
`,
	"deluge": `
resource: deluge
min_tier: 1
upstream: https://deluge.test
auth: {type: deluge, vault_path: homelab/data/docker/deluge, field: password}
templates:
  read:
    min_tier: 1
    description: read
    allow:
      - {method: POST, path: "/json", rpc: {methods: [core.get_torrents_status]}}
`,
}

type recorded struct {
	Method     string
	RequestURI string
	Host       string
	Header     http.Header
	Body       []byte
}

type harness struct {
	t        testing.TB
	px       *Proxy
	store    *grants.Store
	scrub    *redact.Scrubber
	now      time.Time
	mu       sync.Mutex
	got      []recorded
	respond  func(w http.ResponseWriter, r *http.Request)
	refresh  int
	logs     *bytes.Buffer
	upstream *httptest.Server
}

type fakeRefresher struct{ h *harness }

func (f fakeRefresher) Refresh(context.Context, string) bool { f.h.refresh++; return true }

var logMu sync.Mutex

func newHarness(t testing.TB) *harness {
	t.Helper()
	h := &harness{t: t, now: time.Date(2026, 11, 16, 12, 0, 0, 0, time.UTC), logs: &bytes.Buffer{}}
	h.respond = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"ok":true}`)
	}
	h.upstream = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		h.mu.Lock()
		h.got = append(h.got, recorded{r.Method, r.RequestURI, r.Host, r.Header.Clone(), body})
		h.mu.Unlock()
		if r.URL.Path == "/json" && strings.Contains(string(body), `"auth.login"`) {
			http.SetCookie(w, &http.Cookie{Name: "_session_id", Value: "deluge-session-cookie-value"})
			_, _ = io.WriteString(w, `{"result":true,"error":null,"id":1}`)
			return
		}
		if r.URL.Path == "/json" && strings.Contains(string(body), `"web.connected"`) {
			_, _ = io.WriteString(w, `{"result":true,"error":null,"id":1}`)
			return
		}
		h.respond(w, r)
	}))
	t.Cleanup(h.upstream.Close)
	up, _ := url.Parse(h.upstream.URL)

	set := templates.Set{}
	for name, y := range testYAML {
		r, err := templates.Parse([]byte(y))
		if err != nil {
			t.Fatal(err)
		}
		r.UpstreamURL = up
		set[name] = r
	}
	h.scrub = redact.New()
	for name, c := range held {
		h.scrub.Set(name, c.Secret)
	}
	cfg := grants.DefaultConfig()
	cfg.Now = func() time.Time { return h.now }
	cfg.GrantRate, cfg.GrantBurst = 1000, 1000
	cfg.ResourceRate, cfg.ResourceBurst = 1000, 1000
	h.store = grants.New(cfg, set)

	inj := map[string]upstream.Injector{}
	for name, r := range set {
		name := name
		i, err := upstream.New(r, func() (secrets.Credential, bool) { c, ok := held[name]; return c, ok },
			h.upstream.Client(), func(owner string, v ...string) { h.scrub.Set(owner, v...) })
		if err != nil {
			t.Fatal(err)
		}
		inj[name] = i
	}
	h.px = &Proxy{
		Set: set, Grants: h.store, Injectors: inj, Creds: fakeRefresher{h}, Scrub: h.scrub,
		Client: NewClient(), Stats: NewStats(),
		Health: func() (bool, map[string]interface{}) { return true, map[string]interface{}{"status": "ok"} },
	}
	logMu.Lock()
	logger.SetOutput(h.logs)
	// No log scrubber here: the audit lines themselves must carry no
	// secret. main installs the scrubber as a second layer
	// (TestLogScrubberLayer).
	logger.SetScrubber(nil)
	t.Cleanup(func() { logger.SetOutput(io.Discard); logMu.Unlock() })
	return h
}

func hexHash(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

func (h *harness) grant(id, secret, key, resource string, tmpl ...string) *grants.Grant {
	h.t.Helper()
	g, err := h.store.Register(grants.Registration{
		GrantID: id, SecretSHA256: hexHash(secret), RequestID: "req-0123456789ab",
		Requester: "openclaw-noah", RequesterKeySHA256: hexHash(key), Resource: resource,
		Templates: tmpl, Tier: 1, Approval: grants.ApprovalAuto, ExpiresAt: h.now.Add(10 * time.Minute),
	})
	if err != nil {
		h.t.Fatal(err)
	}
	return g
}

// do sends a request with a raw target straight to the handler.
func (h *harness) do(method, rawTarget string, hdr http.Header, body []byte) *httptest.ResponseRecorder {
	u, err := url.ParseRequestURI(rawTarget)
	if err != nil {
		u = &url.URL{Path: "/"}
	}
	r := &http.Request{
		Method: method, RequestURI: rawTarget, URL: u, Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1,
		Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(body)), RemoteAddr: "172.30.6.2:4000",
		Host: "jit-proxy:8090",
	}
	for k, vs := range hdr {
		for _, v := range vs {
			r.Header.Add(k, v)
		}
	}
	w := httptest.NewRecorder()
	h.px.ServeHTTP(w, r)
	return w
}

func authHdr(secret, key string) http.Header {
	return http.Header{"Authorization": {"Bearer " + secret}, RequesterKeyHeader: {key}}
}

func (h *harness) received() []recorded {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]recorded(nil), h.got...)
}

func (h *harness) agentCalls() []recorded {
	var out []recorded
	for _, r := range h.received() {
		if strings.Contains(string(r.Body), `"auth.login"`) || strings.Contains(string(r.Body), `"web.connected"`) {
			continue
		}
		out = append(out, r)
	}
	return out
}

// assertNoSecrets fails if any held secret, live session, grant secret or
// requester key appears in s.
func (h *harness) assertNoSecrets(where, s string) {
	h.t.Helper()
	var all []string
	for _, c := range held {
		all = append(all, c.Secret)
	}
	all = append(all, "deluge-session-cookie-value")
	for _, sec := range all {
		for _, v := range redact.Variants(sec) {
			if strings.Contains(s, v) {
				h.t.Fatalf("%s contains a held secret (%q): %s", where, v, s)
			}
		}
	}
	for _, sec := range []string{grantSecret, noahKey, opsKey} {
		if strings.Contains(s, sec) {
			h.t.Fatalf("%s contains a grant secret or requester key: %s", where, s)
		}
	}
}
