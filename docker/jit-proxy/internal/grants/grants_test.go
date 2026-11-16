package grants

import (
	"crypto/sha256"
	"encoding/hex"
	"math/rand"
	"testing"
	"time"

	"github.com/nkontur/jit-proxy/internal/matrix"
	"github.com/nkontur/jit-proxy/internal/templates"
)

const testTemplate = `
resource: radarr
min_tier: 1
upstream: https://radarr.example
auth: {type: header, name: X-Api-Key, vault_path: homelab/data/docker/radarr, field: api_key}
templates:
  read:
    min_tier: 1
    description: read
    allow:
      - {method: GET, path: "/api/v3/queue"}
  delete-one:
    min_tier: 1
    single_use: true
    description: delete
    allow:
      - {method: DELETE, path: "/api/v3/moviefile/{id:int}"}
  remove:
    min_tier: 2
    single_use: true
    description: remove
    allow:
      - {method: DELETE, path: "/api/v3/movie/{id:int}"}
  danger:
    min_tier: 3
    description: tier three
    allow:
      - {method: GET, path: "/api/v3/system/status"}
`

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func testStore(t testing.TB) (*Store, *clock) {
	t.Helper()
	r, err := templates.Parse([]byte(testTemplate))
	if err != nil {
		t.Fatal(err)
	}
	c := &clock{t: time.Date(2026, 11, 16, 12, 0, 0, 0, time.UTC)}
	cfg := DefaultConfig()
	cfg.Now = c.now
	cfg.GrantRate, cfg.GrantBurst, cfg.ResourceRate, cfg.ResourceBurst = 1000, 1000, 1000, 1000
	return New(cfg, templates.Set{"radarr": r}), c
}

func hashHex(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}

func reg(id, secret, key, requester string, tier int, approval string, tmpl []string, exp time.Time) Registration {
	return Registration{
		GrantID: id, SecretSHA256: hashHex(secret), RequestID: "req-a1b2c3d4e5f6",
		Requester: requester, RequesterKeySHA256: hashHex(key), Resource: "radarr",
		Templates: tmpl, Tier: tier, Approval: approval, ExpiresAt: exp,
	}
}

func TestRegisterValidation(t *testing.T) {
	s, c := testStore(t)
	exp := c.t.Add(10 * time.Minute)
	ok := reg("g-000000000001", "jitp_a", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, exp)
	if _, err := s.Register(ok); err != nil {
		t.Fatalf("valid registration refused: %v", err)
	}
	// Idempotent for identical content, conflict otherwise.
	if _, err := s.Register(ok); err != nil {
		t.Fatalf("identical re-registration refused: %v", err)
	}
	other := ok
	other.SecretSHA256 = hashHex("jitp_b")
	if _, err := s.Register(other); err != ErrConflict {
		t.Fatalf("conflicting re-registration: %v", err)
	}

	bad := []struct {
		name string
		r    Registration
	}{
		{"bad id", reg("x", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, exp)},
		{"unknown template", reg("g-000000000002", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"nope"}, exp)},
		{"no template", reg("g-000000000003", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, nil, exp)},
		{"tier below template", reg("g-000000000004", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"remove"}, exp)},
		{"unknown requester", reg("g-000000000005", "jitp_c", "k", "mallory", 1, ApprovalAuto, []string{"read"}, exp)},
		{"ops tier 3 denied", reg("g-000000000006", "jitp_c", "k", "openclaw-ops", 3, ApprovalApproved, []string{"danger"}, exp)},
		{"ops tier 2 auto", reg("g-000000000007", "jitp_c", "k", "openclaw-ops", 2, ApprovalAuto, []string{"remove"}, exp)},
		{"noah tier 3 auto", reg("g-000000000008", "jitp_c", "k", "openclaw-noah", 3, ApprovalAuto, []string{"danger"}, exp)},
		{"past expiry", reg("g-000000000009", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, c.t.Add(-time.Second))},
		{"ttl beyond tier", reg("g-00000000000a", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, c.t.Add(time.Hour))},
		{"bad approval", reg("g-00000000000b", "jitp_c", "k", "openclaw-noah", 1, "yes", []string{"read"}, exp)},
		{"duplicate template", reg("g-00000000000c", "jitp_c", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read", "read"}, exp)},
	}
	for _, b := range bad {
		if _, err := s.Register(b.r); err == nil {
			t.Errorf("%s: accepted", b.name)
		}
	}
	// What the matrix allows: ops tier 2 with approval, noah tier 2 auto
	// (auto-fyi), noah tier 3 approved.
	for i, r := range []Registration{
		reg("g-0000000000f1", "jitp_d", "k", "openclaw-ops", 2, ApprovalApproved, []string{"remove"}, exp),
		reg("g-0000000000f2", "jitp_e", "k", "openclaw-noah", 2, ApprovalAuto, []string{"remove"}, exp),
		reg("g-0000000000f3", "jitp_f", "k", "openclaw-noah", 3, ApprovalApproved, []string{"danger"}, exp),
	} {
		if _, err := s.Register(r); err != nil {
			t.Errorf("allowed registration %d refused: %v", i, err)
		}
	}
}

func TestGrantBoundToRequester(t *testing.T) {
	s, c := testStore(t)
	if _, err := s.Register(reg("g-000000000001", "jitp_noah", "noah-key", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, c.t.Add(time.Minute))); err != nil {
		t.Fatal(err)
	}
	if _, r := s.Lookup("jitp_noah", "noah-key", "radarr"); r != "" {
		t.Fatalf("owner refused: %s", r)
	}
	for _, key := range []string{"", "ops-key", "noah-key ", "NOAH-KEY"} {
		if _, r := s.Lookup("jitp_noah", key, "radarr"); r != ReasonRequester {
			t.Errorf("key %q: got %q", key, r)
		}
	}
	if _, r := s.Lookup("jitp_noah", "noah-key", "sonarr"); r != ReasonWrongRes {
		t.Errorf("other resource: %q", r)
	}
	if _, r := s.Lookup("noah", "noah-key", "radarr"); r != ReasonNoGrant {
		t.Errorf("no prefix: %q", r)
	}
	if _, r := s.Lookup("jitp_other", "noah-key", "radarr"); r != ReasonUnknown {
		t.Errorf("unknown: %q", r)
	}
}

func TestSingleUseAndBudget(t *testing.T) {
	s, c := testStore(t)
	g, err := s.Register(reg("g-000000000001", "jitp_x", "k", "openclaw-noah", 1, ApprovalAuto, []string{"delete-one"}, c.t.Add(time.Minute)))
	if err != nil {
		t.Fatal(err)
	}
	if r := s.Reserve(g); r != "" {
		t.Fatalf("first use: %s", r)
	}
	s.Refund(g) // upstream failed: a single-use grant stays spent
	if r := s.Reserve(g); r != ReasonSpent {
		t.Fatalf("second use: %q", r)
	}
}

// TestPropertyGrantLifecycle (I3): for random sequences of register, revoke,
// use and clock advance, a request is admitted only if its grant is
// registered, unrevoked, now < expires_at, under budget and, if single-use,
// unused. A reference model is kept alongside the store.
func TestPropertyGrantLifecycle(t *testing.T) {
	type model struct {
		exp       time.Time
		revoked   bool
		used, max int
		single    bool
	}
	for seed := int64(0); seed < 300; seed++ {
		rng := rand.New(rand.NewSource(seed))
		s, c := testStore(t)
		ids := []string{"g-000000000001", "g-000000000002", "g-000000000003", "g-000000000004"}
		m := map[string]*model{}
		gs := map[string]*Grant{}
		for step := 0; step < 200; step++ {
			id := ids[rng.Intn(len(ids))]
			switch op := rng.Intn(10); {
			case op < 2: // register
				if _, ok := m[id]; ok {
					continue
				}
				tmpl := []string{"read"}
				if rng.Intn(3) == 0 {
					tmpl = []string{"delete-one"}
				}
				ttl := time.Duration(1+rng.Intn(900)) * time.Second
				g, err := s.Register(reg(id, "jitp_"+id, "k", "openclaw-noah", 1, ApprovalAuto, tmpl, c.t.Add(ttl)))
				if err != nil {
					t.Fatalf("seed %d: register: %v", seed, err)
				}
				gs[id] = g
				m[id] = &model{exp: c.t.Add(ttl), max: g.MaxRequests, single: g.SingleUse}
			case op < 3: // revoke
				if s.Revoke(id) != (m[id] != nil) {
					t.Fatalf("seed %d: revoke result", seed)
				}
				if m[id] != nil {
					m[id].revoked = true
				}
			case op < 5: // advance the clock
				c.t = c.t.Add(time.Duration(rng.Intn(240)) * time.Second)
			default: // use
				g, lookup := s.Lookup("jitp_"+id, "k", "radarr")
				mm := m[id]
				want := mm != nil && !mm.revoked && c.t.Before(mm.exp) && mm.used < mm.max && !(mm.single && mm.used > 0)
				got := false
				if lookup == "" {
					got = s.Reserve(g) == ""
				} else if lookup != ReasonUnknown {
					t.Fatalf("seed %d: unexpected lookup reason %s", seed, lookup)
				}
				if got != want {
					t.Fatalf("seed %d step %d: admitted=%v want %v (model %+v, now %s)", seed, step, got, want, mm, c.t)
				}
				if got {
					mm.used++
					if rng.Intn(4) == 0 { // upstream failure
						s.Refund(g)
						if !mm.single {
							mm.used--
						}
					}
				}
			}
			if rng.Intn(20) == 0 {
				s.Sweep()
				for id, mm := range m {
					if c.t.Sub(mm.exp) > time.Minute {
						delete(m, id)
						delete(gs, id)
					}
				}
			}
		}
	}
}

func TestRateLimit(t *testing.T) {
	r, _ := templates.Parse([]byte(testTemplate))
	c := &clock{t: time.Unix(0, 0)}
	cfg := DefaultConfig()
	cfg.Now = c.now
	s := New(cfg, templates.Set{"radarr": r})
	g, err := s.Register(reg("g-000000000001", "jitp_x", "k", "openclaw-noah", 1, ApprovalAuto, []string{"read"}, c.t.Add(time.Minute)))
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for i := 0; i < 100; i++ {
		if s.Reserve(g) == "" {
			n++
		}
	}
	if n != int(cfg.GrantBurst) {
		t.Fatalf("burst admitted %d, want %v", n, cfg.GrantBurst)
	}
	c.t = c.t.Add(time.Second)
	if s.Reserve(g) != "" {
		t.Fatal("refill not applied")
	}
}

func TestMatrixParseMatchesDefault(t *testing.T) {
	m, err := matrix.Parse("openclaw-noah=auto,auto-fyi,approve;openclaw-ops=auto,approve,deny")
	if err != nil {
		t.Fatal(err)
	}
	d := matrix.Default()
	for req, row := range d {
		for tier, want := range row {
			if got := m.Decide(req, tier); got != want {
				t.Errorf("%s tier %d: %s vs default %s", req, tier, got, want)
			}
		}
	}
	if _, err := matrix.Parse("x=auto,auto,auto"); err == nil {
		t.Error("tier 3 auto accepted")
	}
	if m.Decide("nobody", 1) != matrix.Deny {
		t.Error("unknown requester not denied")
	}
}
