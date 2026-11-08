package backend

// Fuzz and property tests for the security boundaries of the credential
// backends: backend selection, GitLab agent scopes and identity, token TTL
// and expiry bookkeeping, and dynamic Vault path validation.
//
// `go test ./...` runs every Fuzz* function over its seed corpus (the f.Add
// calls plus testdata/fuzz/<FuzzName>/), so CI checks these without any
// fuzzing time. To fuzz for real, one target at a time:
//
//	go test ./internal/backend -run '^$' -fuzz '^FuzzGitLabAgentScopes$' -fuzztime 2m
//
// All tokens, users and hosts below are fake.

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"
	"unicode/utf8"
)

// knownGitLabAgentScopes is every scope GitLabAgentBackend may hand out.
var knownGitLabAgentScopes = []string{"api", "read_api", "read_repository", "write_repository"}

// splitFuzzList turns one fuzzer string into a list ("," separated). An
// empty input means "no list" so the default-scope path is exercised too.
func splitFuzzList(s string) []string {
	if s == "" {
		return nil
	}
	return strings.Split(s, ",")
}

// --- Backend selection --------------------------------------------------------

type tagBackend struct{ tag string }

func (b *tagBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	return &Credential{Token: "fake-" + b.tag, LeaseTTL: ttl, Metadata: map[string]string{"backend": b.tag}}, nil
}
func (b *tagBackend) Health() error { return nil }

// fullRegistry builds a registry with every dynamic backend enabled (fake
// URLs, nothing is contacted) plus the GitLab agent backend for agentUser,
// registered the way main.go does it.
func fullRegistry(agentUser string) (*Registry, *GitLabAgentBackend) {
	minter := &mockVaultMinter{}
	r := NewRegistry(minter, &mockVaultReader{}, "http://ha.invalid", "http://grafana.invalid",
		"http://influx.invalid", "http://gitlab.invalid", "fake-admin", "4", "http://ts.invalid",
		"http://paperless.invalid", newMockPolicyManager(), &mockVaultSSHSigner{}, "ssh-client-signer",
		"http://google.invalid")
	agent := NewGitLabAgentBackend("http://gitlab.invalid", "fake-admin", agentUser, knownGitLabAgentScopes)
	r.Register("gitlab-"+agentUser, agent)
	return r, agent
}

// FuzzRegistryFor: the agent's impersonation backend is selected for exactly
// one resource name, "gitlab-<configured user>", byte for byte. Case
// changes, whitespace, unicode lookalikes and other users all get some other
// backend (and unknown names the static fallback).
func FuzzRegistryFor(f *testing.F) {
	for _, s := range []string{
		"gitlab-moltbot", "gitlab", "Gitlab-moltbot", "gitlab-moltbot ", " gitlab-moltbot",
		"gitlab-moltbot\x00", "gitlab-MOLTBOT", "gitlab-root", "gitlab-moltbot-elevated",
		"gitlab‐moltbot", "gitlab-mоltbot", "ssh-router", "vault", "", "gitlab-",
	} {
		f.Add(s)
	}
	reg, agent := fullRegistry("moltbot")
	static := reg.fallback
	f.Fuzz(func(t *testing.T, resource string) {
		b := reg.For(resource)
		if b == nil {
			t.Fatalf("For(%q) returned nil", resource)
		}
		if (b == Backend(agent)) != (resource == "gitlab-moltbot") {
			t.Fatalf("For(%q) agent backend selected=%v", resource, b == Backend(agent))
		}
		if reg.IsDynamic(resource) == (b == static) {
			t.Fatalf("For(%q): IsDynamic=%v disagrees with fallback selection", resource, reg.IsDynamic(resource))
		}
	})
}

// --- GitLab agent: scopes, identity, TTL --------------------------------------

// FuzzGitLabAgentScopes: whatever scopes a request asks for (and whatever the
// operator allowlist is), the backend either refuses or returns only scopes
// that are both allowlisted and real agent scopes. Unknown or extra scopes
// are rejected, never passed through.
func FuzzGitLabAgentScopes(f *testing.F) {
	f.Add("", "api,read_api,read_repository,write_repository")
	f.Add("api", "read_api")
	f.Add("api", "")
	f.Add("sudo", "api,read_api")
	f.Add("read_api,sudo", "api,read_api")
	f.Add("read_api,read_api", "read_api")
	f.Add("API", "api")
	f.Add(" api", "api")
	f.Add("api,", "api")
	f.Add("admin_mode", "api,admin_mode")
	f.Add("read_api", " read_api ,,write_repository")
	f.Add("аpi", "api")
	f.Fuzz(func(t *testing.T, requested, allowlist string) {
		allowedList := splitFuzzList(allowlist)
		b := NewGitLabAgentBackend("http://gitlab.invalid", "fake-admin", "moltbot", allowedList)
		allowed := map[string]bool{}
		for _, s := range allowedList {
			if s = strings.TrimSpace(s); s != "" {
				allowed[s] = true
			}
		}
		req := splitFuzzList(requested)
		got, err := b.scopesFor(req)
		if err != nil {
			if got != nil {
				t.Fatalf("scopesFor(%q) returned scopes %v along with error", req, got)
			}
			return
		}
		if len(got) == 0 {
			t.Fatalf("scopesFor(%q) granted an empty scope list without error", req)
		}
		for _, s := range got {
			if !allowed[s] {
				t.Fatalf("scopesFor(%q) with allowlist %q returned non-allowlisted scope %q", req, allowedList, s)
			}
		}
		defaulted := len(req) == 0 || (len(req) == 1 && req[0] == "api")
		if defaulted {
			known := map[string]bool{}
			for _, s := range knownGitLabAgentScopes {
				known[s] = true
			}
			for _, s := range got {
				if !known[s] {
					t.Fatalf("default scopes include unknown scope %q", s)
				}
			}
			return
		}
		// An explicit request is honoured exactly (no widening).
		if strings.Join(got, ",") != strings.Join(req, ",") {
			t.Fatalf("scopesFor(%q) = %q, want the request unchanged", req, got)
		}
	})
}

// agentFake is a minimal GitLab admin API for the agent backend. It records
// every created token and can be told to return an arbitrary user list.
type agentFake struct {
	mu       sync.Mutex
	users    []byte // raw JSON for GET /users
	created  []map[string]interface{}
	userPath []string
}

func newAgentFake(t *testing.T, users []byte) (*agentFake, *httptest.Server) {
	a := &agentFake{users: users}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		a.mu.Lock()
		defer a.mu.Unlock()
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v4/users":
			w.Write(a.users)
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/impersonation_tokens"):
			a.userPath = append(a.userPath, r.URL.Path)
			var body map[string]interface{}
			_ = json.NewDecoder(r.Body).Decode(&body)
			a.created = append(a.created, body)
			w.WriteHeader(http.StatusCreated)
			json.NewEncoder(w).Encode(map[string]interface{}{"id": 1, "name": body["name"], "token": "glpat-fake", "active": true})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	return a, srv
}

// FuzzGitLabAgentIdentity: the backend impersonates only the configured
// user. Whatever the user lookup returns (other users, lookalike names,
// duplicates, garbage), a token is minted only for the ID whose username is
// exactly the configured one.
func FuzzGitLabAgentIdentity(f *testing.F) {
	f.Add([]byte(`[{"id":7,"username":"moltbot"}]`))
	f.Add([]byte(`[{"id":1,"username":"root"},{"id":7,"username":"moltbot"}]`))
	f.Add([]byte(`[{"id":1,"username":"Moltbot"}]`))
	f.Add([]byte(`[{"id":1,"username":"moltbot "}]`))
	f.Add([]byte(`[{"id":1,"username":"mоltbot"}]`))
	f.Add([]byte(`[]`))
	f.Add([]byte(`{"id":7,"username":"moltbot"}`))
	f.Add([]byte(`[{"id":"7","username":"moltbot"}]`))
	f.Add([]byte(`not json`))
	f.Fuzz(func(t *testing.T, users []byte) {
		if len(users) > 4096 {
			return
		}
		a, srv := newAgentFake(t, users)
		defer srv.Close()
		b := NewGitLabAgentBackend(srv.URL, "fake-admin", "moltbot", knownGitLabAgentScopes)
		cred, err := b.MintCredential("gitlab-moltbot", 1, time.Hour, MintOptions{RequestID: "req-fuzz"})

		var parsed []struct {
			ID       int    `json:"id"`
			Username string `json:"username"`
		}
		// Decoded the way the backend decodes it (encoding/json matches
		// field names case-insensitively, so "usernAme" counts, and a
		// missing id is 0, which GitLab answers with 404).
		wantID, found := 0, false
		if json.Unmarshal(users, &parsed) == nil {
			for _, u := range parsed {
				if u.Username == "moltbot" {
					wantID, found = u.ID, true
					break
				}
			}
		}
		if err != nil {
			if cred != nil {
				t.Fatal("credential returned with error")
			}
			if len(a.created) != 0 {
				t.Fatalf("token created although mint failed: %v", a.userPath)
			}
			return
		}
		if !found {
			t.Fatalf("minted although no user is exactly %q (lookup %q)", "moltbot", users)
		}
		want := fmt.Sprintf("/api/v4/users/%d/impersonation_tokens", wantID)
		if len(a.userPath) != 1 || a.userPath[0] != want {
			t.Fatalf("token minted at %v, want %s", a.userPath, want)
		}
	})
}

// FuzzGitLabAgentTTL: the issued credential's TTL is exactly the requested
// TTL, the name encodes now+ttl (so the sweeper revokes it on time), the
// date backstop is at most two days out, and non-positive TTLs are refused.
func FuzzGitLabAgentTTL(f *testing.F) {
	f.Add(int64(time.Hour), "req-abc123")
	f.Add(int64(time.Second), "")
	f.Add(int64(0), "req-x")
	f.Add(int64(-1), "req-x")
	f.Add(int64(1), "req-x")
	f.Add(int64(48*time.Hour), "req-1-2-3")
	f.Add(int64(1<<62), "req-x")
	f.Fuzz(func(t *testing.T, ttlNanos int64, reqID string) {
		a, srv := newAgentFake(t, []byte(`[{"id":7,"username":"moltbot"}]`))
		defer srv.Close()
		b := NewGitLabAgentBackend(srv.URL, "fake-admin", "moltbot", knownGitLabAgentScopes)
		now := time.Date(2026, 3, 1, 12, 0, 0, 0, time.UTC)
		b.now = func() time.Time { return now }
		ttl := time.Duration(ttlNanos)

		cred, err := b.MintCredential("gitlab-moltbot", 1, ttl, MintOptions{RequestID: reqID})
		if ttl <= 0 {
			if err == nil {
				t.Fatalf("ttl %v accepted", ttl)
			}
			return
		}
		if err != nil {
			// Only an absurd TTL may fail (expiry not representable); a
			// failed mint must not have created anything.
			if len(a.created) != 0 {
				t.Fatalf("mint failed after creating a token: %v", err)
			}
			return
		}
		if cred.LeaseTTL != ttl {
			t.Fatalf("LeaseTTL %v != requested %v", cred.LeaseTTL, ttl)
		}
		if len(a.created) != 1 {
			t.Fatalf("created %d tokens", len(a.created))
		}
		name, _ := a.created[0]["name"].(string)
		exp, ok := expiryFromName(name)
		if !ok {
			t.Fatalf("token name %q not recognised by the sweeper", name)
		}
		if want := now.Add(ttl).Unix(); exp.Unix() != want {
			t.Fatalf("name %q encodes expiry %d, want %d", name, exp.Unix(), want)
		}
		expiresAt, _ := a.created[0]["expires_at"].(string)
		backstop, perr := time.Parse("2006-01-02", expiresAt)
		if perr != nil {
			t.Fatalf("expires_at %q: %v", expiresAt, perr)
		}
		if backstop.Before(now.Add(ttl).Truncate(24 * time.Hour)) {
			t.Fatalf("date backstop %s before JIT expiry %s", expiresAt, now.Add(ttl))
		}
		if backstop.After(now.Add(ttl).Add(48 * time.Hour)) {
			t.Fatalf("date backstop %s more than two days past JIT expiry %s", expiresAt, now.Add(ttl))
		}
	})
}

// FuzzExpiryFromName: the sweeper only ever considers names with the JIT
// prefix, never panics, and parses back exactly what MintCredential wrote.
func FuzzExpiryFromName(f *testing.F) {
	for _, s := range []string{"jit-1767225600-req-abc", "jit-1767225600", "jit--5-x", "jit-", "JIT-1-x",
		"manual-token", "jit-99999999999999999999-x", "jit-+1-x", "jit-0x10-x", ""} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, name string) {
		exp, ok := expiryFromName(name)
		if ok && !strings.HasPrefix(name, GitLabAgentTokenPrefix) {
			t.Fatalf("non-JIT name %q parsed as JIT (expiry %v)", name, exp)
		}
		if !ok {
			return
		}
		// Round trip: a name built from this expiry parses to the same time.
		rebuilt := fmt.Sprintf("%s%d-%s", GitLabAgentTokenPrefix, exp.Unix(), "req-x")
		exp2, ok2 := expiryFromName(rebuilt)
		if !ok2 || !exp2.Equal(exp) {
			t.Fatalf("round trip of %q: %v -> %q -> %v,%v", name, exp, rebuilt, exp2, ok2)
		}
	})
}

// TestGitLabAgentSweepOnlyRevokesExpiredJITTokens is a property over a
// generated token list: the sweeper revokes exactly the active JIT tokens
// whose encoded expiry is not in the future, and nothing else.
func TestGitLabAgentSweepOnlyRevokesExpiredJITTokens(t *testing.T) {
	now := time.Date(2026, 3, 1, 12, 0, 0, 0, time.UTC)
	type tok struct {
		gitlabImpersonationToken
		wantRevoke bool
	}
	var toks []tok
	id := 0
	for _, name := range []string{"jit-%d-req-a", "jit-%d", "JIT-%d-x", "personal-%d", "jit-x%d"} {
		for _, delta := range []time.Duration{-time.Hour, -time.Second, 0, time.Second, time.Hour} {
			for _, active := range []bool{true, false} {
				id++
				n := fmt.Sprintf(name, now.Add(delta).Unix())
				_, isJIT := expiryFromName(n)
				toks = append(toks, tok{
					gitlabImpersonationToken: gitlabImpersonationToken{ID: id, Name: n, Active: active},
					wantRevoke:               isJIT && active && delta <= 0,
				})
			}
		}
	}
	var mu sync.Mutex
	deleted := map[int]bool{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch {
		case r.URL.Path == "/api/v4/users":
			w.Write([]byte(`[{"id":7,"username":"moltbot"}]`))
		case r.Method == http.MethodGet && r.URL.Path == "/api/v4/users/7/impersonation_tokens":
			var out []gitlabImpersonationToken
			for _, tk := range toks {
				out = append(out, tk.gitlabImpersonationToken)
			}
			json.NewEncoder(w).Encode(out)
		case r.Method == http.MethodDelete:
			var n int
			fmt.Sscanf(strings.TrimPrefix(r.URL.Path, "/api/v4/users/7/impersonation_tokens/"), "%d", &n)
			deleted[n] = true
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()
	b := NewGitLabAgentBackend(srv.URL, "fake-admin", "moltbot", knownGitLabAgentScopes)
	b.now = func() time.Time { return now }
	if _, err := b.Sweep(); err != nil {
		t.Fatal(err)
	}
	for _, tk := range toks {
		if deleted[tk.ID] != tk.wantRevoke {
			t.Errorf("token %q active=%v: revoked=%v, want %v", tk.Name, tk.Active, deleted[tk.ID], tk.wantRevoke)
		}
	}
}

// --- Dynamic Vault paths -------------------------------------------------------

var hclPathLine = regexp.MustCompile(`^path "homelab/data/[a-zA-Z0-9/_.*\-]*" \{$`)
var hclCapsLine = regexp.MustCompile(`^  capabilities = \[("(read|list|create|update)"(, )?)+\]$`)

// FuzzValidateVaultPaths: any path set that passes validation stays under
// homelab/data/, has no traversal, only grants read/list/create/update, and
// renders to a policy with exactly one well-formed block per path (no HCL
// injection).
func FuzzValidateVaultPaths(f *testing.F) {
	f.Add("homelab/data/grafana", "read")
	f.Add("homelab/data/*", "read,list")
	f.Add("homelab/data/../sys/policy", "read")
	f.Add("homelab/data/x\" {\n capabilities=[\"sudo\"]}\npath \"sys/*", "read")
	f.Add("sys/policy/x", "read")
	f.Add("homelab/data/x", "sudo")
	f.Add("homelab/data/x", "deny")
	f.Add("homelab/data/x", "")
	f.Add("homelab/data/x", "READ")
	f.Add("homelab/data/‥", "read")
	f.Add("homelab/data/x\x00", "read")
	f.Add("Homelab/data/x", "read")
	f.Add("homelab/data/.%2e/x", "read")
	f.Fuzz(func(t *testing.T, path, caps string) {
		paths := []VaultPathRequest{{Path: path, Capabilities: splitFuzzList(caps)}}
		if err := ValidateVaultPaths(paths); err != nil {
			return
		}
		if !strings.HasPrefix(path, "homelab/data/") {
			t.Fatalf("accepted path outside homelab/data/: %q", path)
		}
		if strings.Contains(path, "..") || !utf8.ValidString(path) {
			t.Fatalf("accepted path %q", path)
		}
		for _, c := range paths[0].Capabilities {
			switch c {
			case "read", "list", "create", "update":
			default:
				t.Fatalf("accepted capability %q", c)
			}
		}
		hcl := BuildPolicyHCL(paths)
		var pathLines, capLines int
		for _, line := range strings.Split(hcl, "\n") {
			switch {
			case strings.HasPrefix(line, "path "):
				if !hclPathLine.MatchString(line) {
					t.Fatalf("malformed path line %q in\n%s", line, hcl)
				}
				pathLines++
			case strings.HasPrefix(line, "  capabilities"):
				if !hclCapsLine.MatchString(line) {
					t.Fatalf("malformed capabilities line %q in\n%s", line, hcl)
				}
				capLines++
			}
		}
		if pathLines != 1 || capLines != 1 || strings.Count(hcl, "{") != 1 || strings.Count(hcl, "}") != 1 {
			t.Fatalf("policy for one path has %d path / %d capability lines:\n%s", pathLines, capLines, hcl)
		}
	})
}

// TestValidateVaultPathsLimits: more than maxVaultPaths paths, or any bad
// entry among good ones, rejects the whole request.
func TestValidateVaultPathsLimits(t *testing.T) {
	good := VaultPathRequest{Path: "homelab/data/x", Capabilities: []string{"read"}}
	var many []VaultPathRequest
	for i := 0; i <= maxVaultPaths; i++ {
		many = append(many, good)
	}
	if err := ValidateVaultPaths(many); err == nil {
		t.Errorf("%d paths accepted", len(many))
	}
	if err := ValidateVaultPaths(many[:maxVaultPaths]); err != nil {
		t.Errorf("%d paths rejected: %v", maxVaultPaths, err)
	}
	for i := 0; i < maxVaultPaths; i++ {
		mixed := append([]VaultPathRequest(nil), many[:maxVaultPaths]...)
		mixed[i] = VaultPathRequest{Path: "homelab/data/x", Capabilities: []string{"read", "sudo"}}
		if err := ValidateVaultPaths(mixed); err == nil {
			t.Errorf("bad capability at index %d accepted", i)
		}
	}
}

// --- InfluxDB: tier decides write access -----------------------------------------

// TestInfluxDBTierPermissions: a tier 1 (auto-approved) InfluxDB token is
// read-only for every tier value <= 1; write access only from tier 2 on.
func TestInfluxDBTierPermissions(t *testing.T) {
	var mu sync.Mutex
	var lastActions []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		var body struct {
			Permissions []struct {
				Action string `json:"action"`
			} `json:"permissions"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		mu.Lock()
		lastActions = nil
		for _, p := range body.Permissions {
			lastActions = append(lastActions, p.Action)
		}
		mu.Unlock()
		w.WriteHeader(http.StatusCreated)
		w.Write([]byte(`{"id":"fake-id","token":"fake-influx-token"}`))
	}))
	defer srv.Close()
	reader := &mockVaultReader{secrets: map[string]map[string]string{
		"homelab/data/docker/influxdb": {"admin_token": "fake-admin", "org_id": "fake-org"},
	}}
	b := NewInfluxDBBackend(srv.URL, reader)
	for _, tier := range []int{-1 << 31, -1, 0, 1, 2, 3, 99} {
		if _, err := b.MintCredential("influxdb", tier, time.Hour, MintOptions{}); err != nil {
			t.Fatalf("tier %d: %v", tier, err)
		}
		mu.Lock()
		hasWrite := false
		for _, a := range lastActions {
			if a == "write" {
				hasWrite = true
			}
		}
		mu.Unlock()
		if hasWrite != (tier >= 2) {
			t.Errorf("tier %d: write permission = %v", tier, hasWrite)
		}
	}
}
