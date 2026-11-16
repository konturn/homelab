package proxy

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/rules"
)

func TestAllowedRequestForwarded(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	w := h.do("GET", "/r/radarr/api/v3/queue?page=2", authHdr(grantSecret, noahKey), nil)
	if w.Code != 200 || w.Body.String() != `{"ok":true}` {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
	got := h.agentCalls()
	if len(got) != 1 {
		t.Fatalf("upstream saw %d requests", len(got))
	}
	r := got[0]
	if r.Method != "GET" || r.RequestURI != "/api/v3/queue?page=2" {
		t.Errorf("forwarded %s %s", r.Method, r.RequestURI)
	}
	if r.Header.Get("X-Api-Key") != held["radarr"].Secret {
		t.Error("credential not injected")
	}
	for _, k := range []string{"Authorization", RequesterKeyHeader, "Cookie"} {
		if r.Header.Get(k) != "" {
			t.Errorf("%s forwarded", k)
		}
	}
	if !strings.Contains(h.logs.String(), `"decision":"allow"`) || !strings.Contains(h.logs.String(), `"grant_id":"g-000000000001"`) {
		t.Errorf("audit line missing: %s", h.logs)
	}
	h.assertNoSecrets("log", h.logs.String())

	// What is forwarded is the canonical form that was matched, never the
	// agent's spelling of it.
	w = h.do("GET", "/r/radarr/api/v3/m%6Fvie/1", authHdr(grantSecret, noahKey), nil)
	if w.Code != 200 {
		t.Fatalf("got %d", w.Code)
	}
	if got := h.agentCalls()[1].RequestURI; got != "/api/v3/movie/1" {
		t.Errorf("forwarded %q, want the canonical /api/v3/movie/1", got)
	}
	w = h.do("GET", "/r/radarr/api/v3/movie/lookup?term=a%20b", authHdr(grantSecret, noahKey), nil)
	if got := h.agentCalls()[2].RequestURI; w.Code != 200 || got != "/api/v3/movie/lookup?term=a+b" {
		t.Errorf("forwarded %q", got)
	}
}

func TestLogScrubberLayer(t *testing.T) {
	h := newHarness(t)
	logger.SetScrubber(h.scrub.KnownOnly)
	defer logger.SetScrubber(nil)
	logger.Info("test", logger.Fields{"oops": "key " + held["tautulli"].Secret + " in a field"})
	h.assertNoSecrets("log", h.logs.String())
}

func TestDenials(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	a := authHdr(grantSecret, noahKey)
	cases := []struct {
		method, target string
		hdr            http.Header
		body           string
		status         int
		reason         string
	}{
		{"GET", "/r/radarr/api/v3/config/host", a, "", 403, "denylist"}, // deny beats an allow rule
		{"GET", "/r/radarr/api/v3/CONFIG/host", a, "", 403, "denylist"},
		{"GET", "/r/radarr/api/v3/con%66ig/host", a, "", 403, "denylist"},
		{"GET", "/r/radarr/api/v3/indexer", a, "", 403, "denylist"},
		{"GET", "/r/radarr/api/v3/system/status", a, "", 403, "no_rule"},
		{"POST", "/r/radarr/api/v3/command", a, `{"name":"MoviesSearch"}`, 403, "no_rule"}, // write not granted
		{"GET", "/r/radarr/api/v3/queue?pageSize=9999", a, "", 403, "no_rule"},
		{"GET", "/r/radarr/api/v3/queue?apikey=x", a, "", 400, "credential_param"},
		{"GET", "/r/radarr/api/v3/queue?ApiKey=x", a, "", 400, "credential_param"},
		{"GET", "/r/radarr/api/../v3/queue", a, "", 400, "normalisation"},
		{"GET", "/r/radarr/api/v3/queue/", a, "", 400, "normalisation"},
		{"TRACE", "/r/radarr/api/v3/queue", a, "", 405, "method"},
		{"get", "/r/radarr/api/v3/queue", a, "", 405, "method"},
		{"GET", "/r/sonarr/api/v3/queue", a, "", 404, "unknown_resource"},
		{"GET", "/r/radarr/api/v3/queue", nil, "", 401, "no_grant"},
		{"GET", "/r/radarr/api/v3/queue", authHdr("jitp_unknown", noahKey), "", 401, "unknown_grant"},
		{"GET", "/r/radarr/api/v3/queue", authHdr(grantSecret, opsKey), "", 403, "requester_mismatch"},
		{"GET", "/r/radarr/api/v3/queue", authHdr(grantSecret, ""), "", 403, "requester_mismatch"},
		{"GET", "/r/tautulli/api/v2?cmd=get_activity", a, "", 403, "wrong_resource"},
		{"GET", "/r/radarr/api/v3/queue", http.Header{"Authorization": {"Bearer " + grantSecret}, RequesterKeyHeader: {noahKey}, "Upgrade": {"websocket"}}, "", 400, "upgrade"},
	}
	for _, c := range cases {
		w := h.do(c.method, c.target, c.hdr, []byte(c.body))
		var out map[string]string
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		if w.Code != c.status || out["error"] != c.reason {
			t.Errorf("%s %s: got %d %q, want %d %q", c.method, c.target, w.Code, out["error"], c.status, c.reason)
		}
	}
	if n := len(h.agentCalls()); n != 0 {
		t.Fatalf("upstream received %d denied requests", n)
	}
	h.assertNoSecrets("log", h.logs.String())
}

// TestExpiredAndRevokedGrantsRefused (I3, end to end).
func TestExpiredAndRevokedGrantsRefused(t *testing.T) {
	h := newHarness(t)
	g := h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	a := authHdr(grantSecret, noahKey)
	if w := h.do("GET", "/r/radarr/api/v3/queue", a, nil); w.Code != 200 {
		t.Fatalf("fresh grant refused: %d", w.Code)
	}
	h.now = g.ExpiresAt // now == expires_at is already expired
	if w := h.do("GET", "/r/radarr/api/v3/queue", a, nil); w.Code != 403 || !strings.Contains(w.Body.String(), "expired") {
		t.Fatalf("expired grant: %d %s", w.Code, w.Body)
	}
	h.now = g.ExpiresAt.Add(-time.Minute)
	if w := h.do("GET", "/r/radarr/api/v3/queue", a, nil); w.Code != 200 {
		t.Fatalf("clock back: %d", w.Code)
	}
	h.store.Revoke(g.ID)
	if w := h.do("GET", "/r/radarr/api/v3/queue", a, nil); w.Code != 401 {
		t.Fatalf("revoked grant: %d %s", w.Code, w.Body)
	}
	if n := len(h.agentCalls()); n != 2 {
		t.Fatalf("upstream saw %d requests, want 2", n)
	}
}

func TestSingleUseGrant(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "delete-one")
	a := authHdr(grantSecret, noahKey)
	// A request outside the rules does not spend it.
	if w := h.do("DELETE", "/r/radarr/api/v3/movie/1", a, nil); w.Code != 403 {
		t.Fatalf("got %d", w.Code)
	}
	h.respond = func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }
	if w := h.do("DELETE", "/r/radarr/api/v3/moviefile/7", a, nil); w.Code != 500 {
		t.Fatalf("first use: %d", w.Code)
	}
	// Spent even though the upstream failed.
	if w := h.do("DELETE", "/r/radarr/api/v3/moviefile/7", a, nil); w.Code != 403 || !strings.Contains(w.Body.String(), "spent") {
		t.Fatalf("second use: %d %s", w.Code, w.Body)
	}
	if n := len(h.agentCalls()); n != 1 {
		t.Fatalf("upstream saw %d", n)
	}
}

// TestClientCredentialsNeverForwarded (I5): whatever credential-bearing or
// routing headers the agent sends, the upstream sees only the injected one.
func TestClientCredentialsNeverForwarded(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	h.grant("g-000000000002", "jitp_TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT", noahKey, "tautulli", "read")
	h.grant("g-000000000003", "jitp_NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN", noahKey, "nzbget", "read")
	h.grant("g-000000000004", "jitp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", noahKey, "deluge", "read")
	evil := []string{"Authorization", "Proxy-Authorization", "Cookie", "X-Api-Key", "ApiKey", "X-Plex-Token",
		"X-FTL-SID", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "X-Original-URL",
		"X-Rewrite-URL", "X-HTTP-Method-Override", "Connection", "Te", "Transfer-Encoding", "Host"}
	rng := rand.New(rand.NewSource(1))
	type call struct{ secret, method, target, body string }
	calls := []call{
		{grantSecret, "GET", "/r/radarr/api/v3/movie/5", ""},
		{"jitp_TTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTT", "GET", "/r/tautulli/api/v2?cmd=get_activity", ""},
		{"jitp_NNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNNN", "POST", "/r/nzbget/jsonrpc", `{"method":"status","id":1}`},
		{"jitp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", "POST", "/r/deluge/json", `{"method":"core.get_torrents_status","params":[{},[]],"id":1}`},
	}
	for i := 0; i < 200; i++ {
		c := calls[rng.Intn(len(calls))]
		hdr := authHdr(c.secret, noahKey)
		for _, k := range evil {
			if rng.Intn(2) == 0 {
				v := "attacker-" + k
				if k == "Authorization" {
					v = "Basic " + base64.StdEncoding.EncodeToString([]byte("evil:evil"))
				}
				if k == "Cookie" {
					v = "_session_id=attacker"
				}
				hdr.Add(k, v)
			}
		}
		before := len(h.received())
		w := h.do(c.method, c.target, hdr, []byte(c.body))
		if w.Code != 200 {
			t.Fatalf("%s %s: %d %s", c.method, c.target, w.Code, w.Body)
		}
		for _, r := range h.received()[before:] {
			for k, vs := range r.Header {
				for _, v := range vs {
					if strings.Contains(v, "attacker") || strings.Contains(v, "evil") {
						t.Fatalf("agent header %s: %s forwarded", k, v)
					}
				}
			}
			if strings.Contains(r.RequestURI, "attacker") {
				t.Fatalf("agent value in target: %s", r.RequestURI)
			}
			if r.Host != strings.TrimPrefix(h.upstream.URL, "http://") {
				t.Fatalf("host %q", r.Host)
			}
			switch {
			case strings.HasPrefix(r.RequestURI, "/api/v3"):
				if r.Header.Get("X-Api-Key") != held["radarr"].Secret || len(r.Header.Values("X-Api-Key")) != 1 {
					t.Fatal("radarr credential wrong")
				}
			case strings.HasPrefix(r.RequestURI, "/api/v2"):
				q, _ := url.ParseQuery(strings.SplitN(r.RequestURI, "?", 2)[1])
				if q.Get("apikey") != held["tautulli"].Secret || len(q["apikey"]) != 1 {
					t.Fatal("tautulli credential wrong")
				}
			case r.RequestURI == "/jsonrpc":
				u, p, ok := (&http.Request{Header: r.Header}).BasicAuth()
				if !ok || u != "nzbuser" || p != held["nzbget"].Secret {
					t.Fatal("nzbget credential wrong")
				}
			case r.RequestURI == "/json" && strings.Contains(string(r.Body), "auth.login"):
				if r.Header.Get("Cookie") != "" {
					t.Fatal("cookie sent with the proxy's own login")
				}
			case r.RequestURI == "/json":
				if ck := r.Header.Get("Cookie"); ck != "_session_id=deluge-session-cookie-value" {
					t.Fatalf("deluge cookie %q", ck)
				}
			}
		}
	}
	h.assertNoSecrets("log", h.logs.String())
}

// TestSecretNeverReturned (I2, end to end): an upstream that echoes every
// held secret in body and headers, in several encodings, never gets one
// through to the agent.
func TestSecretNeverReturned(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	var echo []string
	for _, c := range held {
		echo = append(echo, c.Secret, url.QueryEscape(c.Secret), base64.StdEncoding.EncodeToString([]byte("u:"+c.Secret)))
	}
	echo = append(echo, "deluge-session-cookie-value", grantSecret)
	h.respond = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Set-Cookie", "session="+held["radarr"].Secret)
		w.Header().Set("X-Api-Key", held["radarr"].Secret)
		w.Header().Set("Location", "/api/v3/queue?apikey="+held["radarr"].Secret)
		w.WriteHeader(201)
		b, _ := json.Marshal(map[string]interface{}{"echo": echo, "apiKey": "unheld-key-value",
			"url": "http://x/?apikey=" + held["tautulli"].Secret})
		_, _ = w.Write(b)
	}
	// Prime the deluge session so its cookie is a held secret too.
	h.grant("g-000000000002", "jitp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", noahKey, "deluge", "read")
	h.do("POST", "/r/deluge/json", authHdr("jitp_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD", noahKey), []byte(`{"method":"core.get_torrents_status","params":[],"id":1}`))

	w := h.do("GET", "/r/radarr/api/v3/movie/1", authHdr(grantSecret, noahKey), nil)
	if w.Code != 201 {
		t.Fatalf("status %d", w.Code)
	}
	var hdrs strings.Builder
	for k, vs := range w.Header() {
		fmt.Fprintf(&hdrs, "%s: %s\n", k, strings.Join(vs, ","))
	}
	h.assertNoSecrets("response headers", hdrs.String())
	h.assertNoSecrets("response body", w.Body.String())
	if w.Header().Get("Set-Cookie") != "" || w.Header().Get("X-Api-Key") != "" {
		t.Error("credential-bearing response header passed")
	}
	if !strings.Contains(w.Body.String(), `"echo":[`) || !strings.Contains(w.Body.String(), "[REDACTED]") {
		t.Errorf("body withheld or not redacted in place: %s", w.Body)
	}
	if strings.Contains(w.Body.String(), "unheld-key-value") {
		t.Error("apiKey field not pattern-redacted")
	}
	if loc := w.Header().Get("Location"); loc != "/r/radarr/api/v3/queue" {
		t.Errorf("location %q", loc)
	}
	h.assertNoSecrets("log", h.logs.String())
	if !strings.Contains(h.logs.String(), "secret_echo_redacted") {
		t.Error("redaction not logged")
	}
}

func TestRedirectsNotFollowed(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	h.respond = func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "http://169.254.169.254/latest/meta-data", http.StatusFound)
	}
	w := h.do("GET", "/r/radarr/api/v3/movie/1", authHdr(grantSecret, noahKey), nil)
	if w.Code != 302 || w.Header().Get("Location") != "" {
		t.Fatalf("got %d location %q", w.Code, w.Header().Get("Location"))
	}
	if n := len(h.agentCalls()); n != 1 {
		t.Fatalf("upstream calls %d", n)
	}
}

func TestBinaryResponsesRefused(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	h.respond = func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/jpeg")
		_, _ = w.Write([]byte{0xff, 0xd8, 0xff})
	}
	w := h.do("GET", "/r/radarr/api/v3/movie/1", authHdr(grantSecret, noahKey), nil)
	if w.Code != 502 || !strings.Contains(w.Body.String(), "unsupported_content_type") {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
}

func TestUpstreamAuthFailureRefreshes(t *testing.T) {
	h := newHarness(t)
	h.grant("g-000000000001", grantSecret, noahKey, "radarr", "read")
	h.respond = func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(401) }
	w := h.do("GET", "/r/radarr/api/v3/movie/1", authHdr(grantSecret, noahKey), nil)
	if w.Code != 502 || !strings.Contains(w.Body.String(), "upstream_auth_failed") {
		t.Fatalf("got %d %s", w.Code, w.Body)
	}
	if h.refresh != 1 || len(h.agentCalls()) != 2 {
		t.Fatalf("refresh %d, calls %d", h.refresh, len(h.agentCalls()))
	}
}

// FuzzProxyRequest (I1): for any method, raw target, body and grant, if the
// upstream receives a request then its method, canonical path, query and body
// satisfy an allow rule of the grant's templates, no deny rule of the
// resource, and it went to the configured upstream host.
func FuzzProxyRequest(f *testing.F) {
	seeds := []struct{ m, t, b string }{
		{"GET", "/r/radarr/api/v3/queue?page=1", ""},
		{"GET", "/r/radarr/api/v3/config/host", ""},
		{"GET", "/r/radarr/api/v3/c%6Fnfig/host", ""},
		{"GET", "/r/radarr/api/v3/movie/lookup?term=a%20b", ""},
		{"POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch"}`},
		{"POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","NAME":"x"}`},
		{"DELETE", "/r/radarr/api/v3/moviefile/3", ""},
		{"GET", "/r/radarr/api/v3/movie/1?x=1", ""},
	}
	for _, s := range seeds {
		f.Add(s.m, s.t, []byte(s.b), uint8(0))
		f.Add(s.m, s.t, []byte(s.b), uint8(1))
	}
	tmplSets := [][]string{{"read"}, {"write"}, {"delete-one"}, {"read", "delete-one"}}
	f.Fuzz(func(t *testing.T, method, target string, body []byte, which uint8) {
		h := newHarness(t)
		tmpl := tmplSets[int(which)%len(tmplSets)]
		g := h.grant("g-000000000001", grantSecret, noahKey, "radarr", tmpl...)
		h.do(method, target, authHdr(grantSecret, noahKey), body)
		res := h.px.Set["radarr"]
		for _, r := range h.agentCalls() {
			if r.Host != strings.TrimPrefix(h.upstream.URL, "http://") {
				t.Fatalf("wrong host %q", r.Host)
			}
			tg, err := rules.ParseTarget("/r/radarr" + r.RequestURI)
			if err != nil {
				t.Fatalf("upstream got a non-canonical target %q (from %q)", r.RequestURI, target)
			}
			canon := tg.EscapedPath()
			if q := tg.Query.Encode(); q != "" {
				canon += "?" + q
			}
			if r.RequestURI != canon {
				t.Fatalf("forwarded %q is not the canonical form %q (from %q)", r.RequestURI, canon, target)
			}
			for _, d := range res.Deny {
				if d.MatchDeny(r.Method, tg.Segments) {
					t.Fatalf("denied path forwarded: %s %s (from %q)", r.Method, r.RequestURI, target)
				}
			}
			ok := false
			for _, rule := range g.Rules {
				if rule.Match(&rules.Request{Method: r.Method, Target: tg, Body: r.Body}, res.BodyMax) {
					ok = true
				}
			}
			if !ok {
				t.Fatalf("forwarded outside the grant: %s %s %q (from %q)", r.Method, r.RequestURI, r.Body, target)
			}
		}
		h.assertNoSecrets("log", h.logs.String())
	})
}
