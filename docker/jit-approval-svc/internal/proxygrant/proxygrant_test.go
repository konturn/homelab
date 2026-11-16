package proxygrant

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// fakeProxy emulates the proxy's control plane.
type fakeProxy struct {
	mu       sync.Mutex
	bootID   string
	regs     map[string]registration
	deleted  []string
	refuse   bool
	ctlKey   string
	putCount int
}

func newFake(t *testing.T) (*fakeProxy, *Client) {
	f := &fakeProxy{bootID: "boot-1", regs: map[string]registration{}, ctlKey: "ctl-key"}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		if r.URL.Path == "/health" {
			_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "boot_id": f.bootID})
			return
		}
		if r.Header.Get("Authorization") != "Bearer "+f.ctlKey {
			w.WriteHeader(401)
			return
		}
		switch {
		case r.URL.Path == "/internal/catalog":
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"boot_id": f.bootID, "resources": map[string]ResourceInfo{
				"radarr": {MinTier: 1, Templates: map[string]TemplateInfo{
					"read":         {Description: "read", MinTier: 1},
					"replace-file": {Description: "delete one file", MinTier: 1, SingleUse: true},
					"remove-title": {Description: "remove a title", MinTier: 2, SingleUse: true},
				}},
				"pihole": {MinTier: 2, Templates: map[string]TemplateInfo{"read": {Description: "read", MinTier: 2}}},
			}})
		case strings.HasPrefix(r.URL.Path, "/internal/grants/") && r.Method == http.MethodPut:
			f.putCount++
			if f.refuse {
				w.WriteHeader(422)
				_, _ = w.Write([]byte(`{"error":"matrix denies"}`))
				return
			}
			var reg registration
			_ = json.NewDecoder(r.Body).Decode(&reg)
			f.regs[reg.GrantID] = reg
			single := false
			for _, t := range reg.Templates {
				if t == "replace-file" || t == "remove-title" {
					single = true
				}
			}
			w.WriteHeader(201)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{"grant_id": reg.GrantID, "single_use": single, "max_requests": 1000})
		case strings.HasPrefix(r.URL.Path, "/internal/grants/") && r.Method == http.MethodDelete:
			f.deleted = append(f.deleted, strings.TrimPrefix(r.URL.Path, "/internal/grants/"))
			w.WriteHeader(204)
		default:
			w.WriteHeader(404)
		}
	}))
	t.Cleanup(srv.Close)
	c := New(srv.URL, "ctl-key", "http://172.30.6.10:8090")
	return f, c
}

func keyHash(k string) string {
	s := sha256.Sum256([]byte(k))
	return hex.EncodeToString(s[:])
}

func mint(c *Client, requester, key string, tier int, approval string, tmpl ...string) (*MintRequest, error) {
	m := &MintRequest{RequestID: "req-0123456789ab", Requester: requester, RequesterKeySHA256: keyHash(key),
		Resource: "radarr", Templates: tmpl, Tier: tier, Approval: approval, TTL: 10 * time.Minute}
	_, err := c.Mint(*m)
	return m, err
}

func TestMintRegistersHashesOnly(t *testing.T) {
	f, c := newFake(t)
	var logs bytes.Buffer
	setLog(&logs)
	defer setLog(os.Stdout)
	cred, err := c.Mint(MintRequest{RequestID: "req-0123456789ab", Requester: "openclaw-noah",
		RequesterKeySHA256: keyHash("noah-key"), Resource: "radarr", Templates: []string{"read"},
		Tier: 1, Approval: ApprovalAuto, TTL: 10 * time.Minute})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(cred.Token, "jitp_") || len(cred.Token) != len("jitp_")+43 {
		t.Fatalf("token shape %q", cred.Token)
	}
	if len(f.regs) != 1 {
		t.Fatalf("%d registrations", len(f.regs))
	}
	var reg registration
	for _, r := range f.regs {
		reg = r
	}
	if reg.SecretSHA256 != keyHash(cred.Token) || reg.RequesterKeySHA256 != keyHash("noah-key") ||
		reg.Requester != "openclaw-noah" || reg.Approval != ApprovalAuto || reg.Tier != 1 {
		t.Fatalf("registration %+v", reg)
	}
	if d := time.Until(reg.ExpiresAt); d < 9*time.Minute || d > 10*time.Minute {
		t.Fatalf("expiry in %v", d)
	}
	md := cred.Metadata
	if md["type"] != CredentialType || md["grant_id"] != reg.GrantID || md["base_url"] != "http://172.30.6.10:8090/r/radarr" {
		t.Fatalf("metadata %+v", md)
	}
	if strings.Contains(logs.String(), cred.Token) {
		t.Fatal("grant secret logged")
	}
	if !strings.Contains(logs.String(), "grant_issued") {
		t.Fatal("issuance not logged")
	}
}

func TestMintFailsClosed(t *testing.T) {
	f, c := newFake(t)
	f.refuse = true
	if _, err := mint(c, "openclaw-noah", "k", 1, ApprovalAuto, "read"); err == nil {
		t.Fatal("refused registration issued a grant")
	}
	if len(c.grants) != 0 {
		t.Fatal("refused grant remembered")
	}
	c.CtlURL = "http://127.0.0.1:1"
	c.catalog = nil
	if _, err := mint(c, "openclaw-noah", "k", 1, ApprovalAuto, "read"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("proxy down: %v", err)
	}
}

func TestMintValidation(t *testing.T) {
	_, c := newFake(t)
	for name, m := range map[string]MintRequest{
		"tier below template": {Requester: "openclaw-noah", RequesterKeySHA256: keyHash("k"), Resource: "radarr", Templates: []string{"remove-title"}, Tier: 1, Approval: ApprovalAuto, TTL: time.Minute},
		"no requester key":    {Requester: "openclaw-noah", Resource: "radarr", Templates: []string{"read"}, Tier: 1, Approval: ApprovalAuto, TTL: time.Minute},
		"bad approval":        {Requester: "openclaw-noah", RequesterKeySHA256: keyHash("k"), Resource: "radarr", Templates: []string{"read"}, Tier: 1, Approval: "yes", TTL: time.Minute},
		"unknown template":    {Requester: "openclaw-noah", RequesterKeySHA256: keyHash("k"), Resource: "radarr", Templates: []string{"admin"}, Tier: 1, Approval: ApprovalAuto, TTL: time.Minute},
		"unknown resource":    {Requester: "openclaw-noah", RequesterKeySHA256: keyHash("k"), Resource: "gitlab", Templates: []string{"read"}, Tier: 2, Approval: ApprovalAuto, TTL: time.Minute},
	} {
		m.RequestID = "req-0123456789ab"
		if _, err := c.Mint(m); err == nil {
			t.Errorf("%s: issued", name)
		}
	}
}

// TestPropertyTierIsMaxOfTemplates: for random template sets, Tier is the
// highest of the resource's and the templates' minimum tiers.
func TestPropertyTierIsMaxOfTemplates(t *testing.T) {
	_, c := newFake(t)
	all := map[string]int{"read": 1, "replace-file": 1, "remove-title": 2}
	names := []string{"read", "replace-file", "remove-title"}
	rng := rand.New(rand.NewSource(7))
	for i := 0; i < 500; i++ {
		perm := rng.Perm(len(names))[:1+rng.Intn(len(names))]
		var tmpl []string
		want := 1
		for _, p := range perm {
			tmpl = append(tmpl, names[p])
			if all[names[p]] > want {
				want = all[names[p]]
			}
		}
		got, err := c.Tier("radarr", tmpl)
		if err != nil || got != want {
			t.Fatalf("%v: got %d %v, want %d", tmpl, got, err, want)
		}
	}
	if got, _ := c.Tier("pihole", []string{"read"}); got != 2 {
		t.Fatalf("resource minimum ignored: %d", got)
	}
}

func TestRevokeOnlyByOwner(t *testing.T) {
	f, c := newFake(t)
	if _, err := mint(c, "openclaw-noah", "k", 1, ApprovalAuto, "read"); err != nil {
		t.Fatal(err)
	}
	var id string
	for k := range c.grants {
		id = k
	}
	auth := func(who string) func(*http.Request) (string, bool) {
		return func(*http.Request) (string, bool) { return who, true }
	}
	h := c.HandleRevoke(auth("openclaw-ops"))
	w := httptest.NewRecorder()
	h(w, httptest.NewRequest(http.MethodPost, "/grants/"+id+"/revoke", nil))
	if w.Code != 404 || len(f.deleted) != 0 {
		t.Fatalf("other requester revoked: %d %v", w.Code, f.deleted)
	}
	w = httptest.NewRecorder()
	c.HandleRevoke(func(*http.Request) (string, bool) { return "", false })(w, httptest.NewRequest(http.MethodPost, "/grants/"+id+"/revoke", nil))
	if w.Code != 401 {
		t.Fatalf("unauthenticated: %d", w.Code)
	}
	w = httptest.NewRecorder()
	c.HandleRevoke(auth("openclaw-noah"))(w, httptest.NewRequest(http.MethodPost, "/grants/"+id+"/revoke", nil))
	if w.Code != 200 || len(f.deleted) != 1 || f.deleted[0] != id {
		t.Fatalf("owner revoke: %d %v", w.Code, f.deleted)
	}
}

func TestReregisterAfterProxyRestart(t *testing.T) {
	f, c := newFake(t)
	ctx := context.Background()
	c.Tick(ctx) // first contact
	if _, err := mint(c, "openclaw-noah", "k", 1, ApprovalAuto, "read"); err != nil {
		t.Fatal(err)
	}
	if _, err := mint(c, "openclaw-noah", "k", 1, ApprovalAuto, "replace-file"); err != nil {
		t.Fatal(err)
	}
	before := f.putCount
	c.Tick(ctx) // same boot: nothing re-pushed
	if f.putCount != before {
		t.Fatal("re-registered without a restart")
	}
	f.mu.Lock()
	f.bootID = "boot-2"
	f.regs = map[string]registration{}
	f.mu.Unlock()
	c.Tick(ctx)
	if len(f.regs) != 1 {
		t.Fatalf("re-registered %d grants, want 1 (single-use grants are never re-pushed)", len(f.regs))
	}
	for _, r := range f.regs {
		if r.Templates[0] != "read" {
			t.Fatalf("re-pushed %v", r.Templates)
		}
	}
	// Expired grants are revoked by the broker.
	c.Now = func() time.Time { return time.Now().Add(time.Hour) }
	c.Tick(ctx)
	if len(f.deleted) != 2 || len(c.grants) != 0 {
		t.Fatalf("expired: deleted %v, left %d", f.deleted, len(c.grants))
	}
}

func setLog(w io.Writer) { logger.SetOutput(w) }
