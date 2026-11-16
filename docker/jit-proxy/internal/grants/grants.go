// Package grants holds the proxy grants the broker registers. A grant is
// stored by the SHA-256 of its secret (the proxy never sees the secret until
// the agent presents it) and of its requester's broker key, so a grant is
// only usable by the requester it was issued to.
//
// Every check that decides whether a request may reach an upstream happens in
// Reserve, under one lock and against a fresh clock read: registered, not
// revoked, now < expires_at, under budget and, for single-use grants, unused.
package grants

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/nkontur/jit-proxy/internal/matrix"
	"github.com/nkontur/jit-proxy/internal/rules"
	"github.com/nkontur/jit-proxy/internal/templates"
)

// SecretPrefix starts every grant secret.
const SecretPrefix = "jitp_"

// Approval values a registration may carry.
const (
	ApprovalAuto     = "auto"     // issued without a human (matrix auto / auto-fyi)
	ApprovalApproved = "approved" // Noah pressed Approve in Telegram
)

// Registration is what the broker sends to register a grant.
type Registration struct {
	GrantID            string    `json:"grant_id"`
	SecretSHA256       string    `json:"secret_sha256"`
	RequestID          string    `json:"request_id"`
	Requester          string    `json:"requester"`
	RequesterKeySHA256 string    `json:"requester_key_sha256"`
	Resource           string    `json:"resource"`
	Templates          []string  `json:"templates"`
	Tier               int       `json:"tier"`
	Approval           string    `json:"approval"`
	ExpiresAt          time.Time `json:"expires_at"`
}

// Grant is a registered grant.
type Grant struct {
	ID        string
	RequestID string
	Requester string
	Resource  string
	Templates []string
	Tier      int
	Approval  string
	IssuedAt  time.Time
	ExpiresAt time.Time

	Rules       []*rules.Rule
	MaxRequests int
	SingleUse   bool

	secretHash [32]byte
	keyHash    [32]byte
	used       int
	revoked    bool
	bucket     bucket
}

// Reasons a request is refused (also the audit log's "reason").
const (
	ReasonNoGrant      = "no_grant"
	ReasonUnknown      = "unknown_grant"
	ReasonRequester    = "requester_mismatch"
	ReasonWrongRes     = "wrong_resource"
	ReasonRevoked      = "revoked"
	ReasonExpired      = "expired"
	ReasonSpent        = "spent"
	ReasonBudget       = "budget"
	ReasonRate         = "rate"
	ReasonResourceRate = "resource_rate"
)

// Config tunes the store.
type Config struct {
	Matrix        matrix.Matrix
	MaxTTL        map[int]time.Duration // per tier
	Now           func() time.Time
	GrantRate     float64 // requests per second per grant
	GrantBurst    float64
	ResourceRate  float64 // requests per second per resource (all grants)
	ResourceBurst float64
	MaxGrants     int
}

// DefaultConfig returns the deployed limits (design 5.1, Q4).
func DefaultConfig() Config {
	return Config{
		Matrix:        matrix.Default(),
		MaxTTL:        map[int]time.Duration{1: 15 * time.Minute, 2: 30 * time.Minute, 3: 60 * time.Minute},
		Now:           time.Now,
		GrantRate:     5,
		GrantBurst:    20,
		ResourceRate:  20,
		ResourceBurst: 60,
		MaxGrants:     5000,
	}
}

// Store holds grants in memory.
type Store struct {
	cfg  Config
	set  templates.Set
	mu   sync.Mutex
	byID map[string]*Grant
	// byHash only holds usable (unrevoked) grants.
	byHash     map[[32]byte]*Grant
	resBuckets map[string]*bucket
}

// New creates a store.
func New(cfg Config, set templates.Set) *Store {
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Store{cfg: cfg, set: set, byID: map[string]*Grant{}, byHash: map[[32]byte]*Grant{}, resBuckets: map[string]*bucket{}}
}

var (
	grantIDRe   = regexp.MustCompile(`^g-[0-9a-f]{12}$`)
	requestIDRe = regexp.MustCompile(`^req-[0-9a-f]{6,32}$`)
)

// ErrConflict is returned when a grant ID is re-registered with other content.
var ErrConflict = errors.New("grant id already registered with different content")

func parseHash(s string) ([32]byte, bool) {
	var h [32]byte
	b, err := hex.DecodeString(s)
	if err != nil || len(b) != 32 {
		return h, false
	}
	copy(h[:], b)
	return h, true
}

// Register validates a registration and stores the grant. It is idempotent
// for an identical registration of the same grant ID.
func (s *Store) Register(reg Registration) (*Grant, error) {
	if !grantIDRe.MatchString(reg.GrantID) {
		return nil, fmt.Errorf("invalid grant_id")
	}
	if !requestIDRe.MatchString(reg.RequestID) {
		return nil, fmt.Errorf("invalid request_id")
	}
	if !matrix.ValidName(reg.Requester) {
		return nil, fmt.Errorf("invalid requester")
	}
	secretHash, ok := parseHash(reg.SecretSHA256)
	if !ok {
		return nil, fmt.Errorf("invalid secret_sha256")
	}
	keyHash, ok := parseHash(reg.RequesterKeySHA256)
	if !ok {
		return nil, fmt.Errorf("invalid requester_key_sha256")
	}
	res, ok := s.set[reg.Resource]
	if !ok {
		return nil, fmt.Errorf("unknown resource %q", reg.Resource)
	}
	resolved, err := res.Resolve(reg.Templates)
	if err != nil {
		return nil, err
	}
	minTier, err := res.Tier(reg.Templates)
	if err != nil {
		return nil, err
	}
	if reg.Tier < minTier || reg.Tier > matrix.MaxTier {
		return nil, fmt.Errorf("tier %d is below the templates' tier %d", reg.Tier, minTier)
	}
	// The same matrix the broker applies: a requester/tier it denies is
	// refused, and a tier that needs a human must arrive approved.
	switch d := s.cfg.Matrix.Decide(reg.Requester, reg.Tier); {
	case d == matrix.Deny:
		return nil, fmt.Errorf("matrix denies %s at tier %d", reg.Requester, reg.Tier)
	case d == matrix.Approve && reg.Approval != ApprovalApproved:
		return nil, fmt.Errorf("matrix needs approval for %s at tier %d", reg.Requester, reg.Tier)
	case reg.Approval != ApprovalAuto && reg.Approval != ApprovalApproved:
		return nil, fmt.Errorf("invalid approval %q", reg.Approval)
	}
	now := s.cfg.Now()
	maxTTL, ok := s.cfg.MaxTTL[reg.Tier]
	if !ok {
		return nil, fmt.Errorf("no TTL for tier %d", reg.Tier)
	}
	if !reg.ExpiresAt.After(now) {
		return nil, fmt.Errorf("expires_at is not in the future")
	}
	if reg.ExpiresAt.Sub(now) > maxTTL+30*time.Second {
		return nil, fmt.Errorf("expires_at exceeds the tier %d maximum of %s", reg.Tier, maxTTL)
	}

	tmpls := append([]string(nil), reg.Templates...)
	sort.Strings(tmpls)

	s.mu.Lock()
	defer s.mu.Unlock()
	if old, ok := s.byID[reg.GrantID]; ok {
		if !old.revoked && old.secretHash == secretHash && old.keyHash == keyHash &&
			old.Resource == reg.Resource && old.ExpiresAt.Equal(reg.ExpiresAt) &&
			strings.Join(old.Templates, ",") == strings.Join(tmpls, ",") {
			return old, nil
		}
		return nil, ErrConflict
	}
	if _, dup := s.byHash[secretHash]; dup {
		return nil, ErrConflict
	}
	if len(s.byID) >= s.cfg.MaxGrants {
		s.sweepLocked(now)
		if len(s.byID) >= s.cfg.MaxGrants {
			return nil, fmt.Errorf("too many grants")
		}
	}
	g := &Grant{
		ID: reg.GrantID, RequestID: reg.RequestID, Requester: reg.Requester,
		Resource: reg.Resource, Templates: tmpls, Tier: reg.Tier, Approval: reg.Approval,
		IssuedAt: now, ExpiresAt: reg.ExpiresAt,
		Rules: resolved.Rules, MaxRequests: resolved.MaxRequests, SingleUse: resolved.SingleUse,
		secretHash: secretHash, keyHash: keyHash,
		bucket: bucket{tokens: s.cfg.GrantBurst, last: now},
	}
	s.byID[g.ID] = g
	s.byHash[secretHash] = g
	return g, nil
}

// Revoke revokes one grant. It reports whether the grant existed.
func (s *Store) Revoke(id string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.byID[id]
	if !ok {
		return false
	}
	g.revoked = true
	delete(s.byHash, g.secretHash)
	return true
}

// RevokeResource revokes every grant for a resource ("" = all resources).
func (s *Store) RevokeResource(resource string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, g := range s.byID {
		if !g.revoked && (resource == "" || g.Resource == resource) {
			g.revoked = true
			delete(s.byHash, g.secretHash)
			n++
		}
	}
	return n
}

// Lookup finds the grant for a presented secret and checks that the caller
// holds the grant's requester key and is calling the grant's resource. It
// does not consume anything; Reserve does.
func (s *Store) Lookup(secret, requesterKey, resource string) (*Grant, string) {
	if !strings.HasPrefix(secret, SecretPrefix) {
		return nil, ReasonNoGrant
	}
	h := sha256.Sum256([]byte(secret))
	s.mu.Lock()
	g, ok := s.byHash[h]
	s.mu.Unlock()
	if !ok {
		return nil, ReasonUnknown
	}
	kh := sha256.Sum256([]byte(requesterKey))
	if requesterKey == "" || subtle.ConstantTimeCompare(kh[:], g.keyHash[:]) != 1 {
		return g, ReasonRequester
	}
	if g.Resource != resource {
		return g, ReasonWrongRes
	}
	return g, ""
}

// Reserve is the final gate before a request is forwarded. It re-checks the
// grant's state against the current time and charges one request.
func (s *Store) Reserve(g *Grant) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.cfg.Now()
	cur, ok := s.byID[g.ID]
	if !ok || cur != g {
		return ReasonUnknown
	}
	switch {
	case g.revoked:
		return ReasonRevoked
	case !now.Before(g.ExpiresAt):
		return ReasonExpired
	case g.SingleUse && g.used > 0:
		return ReasonSpent
	case g.used >= g.MaxRequests:
		return ReasonBudget
	}
	if !g.bucket.take(now, s.cfg.GrantRate, s.cfg.GrantBurst) {
		return ReasonRate
	}
	rb, ok := s.resBuckets[g.Resource]
	if !ok {
		rb = &bucket{tokens: s.cfg.ResourceBurst, last: now}
		s.resBuckets[g.Resource] = rb
	}
	if !rb.take(now, s.cfg.ResourceRate, s.cfg.ResourceBurst) {
		g.bucket.tokens++ // give the grant's token back
		return ReasonResourceRate
	}
	g.used++
	return ""
}

// Refund returns a request to the budget after an upstream failure. A
// single-use grant is never refunded: it is spent by the first request that
// passes the rules, whatever the upstream answers.
func (s *Store) Refund(g *Grant) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !g.SingleUse && g.used > 0 {
		g.used--
	}
}

// Used returns how many requests a grant has been charged.
func (s *Store) Used(g *Grant) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return g.used
}

// Sweep drops grants that expired more than a minute ago and reports them.
func (s *Store) Sweep() []*Grant {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.sweepLocked(s.cfg.Now())
}

func (s *Store) sweepLocked(now time.Time) []*Grant {
	var out []*Grant
	for id, g := range s.byID {
		if now.Sub(g.ExpiresAt) > time.Minute {
			delete(s.byID, id)
			if s.byHash[g.secretHash] == g {
				delete(s.byHash, g.secretHash)
			}
			out = append(out, g)
		}
	}
	return out
}

// Active counts usable grants per resource.
func (s *Store) Active() map[string]int {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.cfg.Now()
	out := map[string]int{}
	for _, g := range s.byHash {
		if now.Before(g.ExpiresAt) {
			out[g.Resource]++
		}
	}
	return out
}

// bucket is a token bucket.
type bucket struct {
	tokens float64
	last   time.Time
}

func (b *bucket) take(now time.Time, rate, burst float64) bool {
	if el := now.Sub(b.last).Seconds(); el > 0 {
		b.tokens += el * rate
		if b.tokens > burst {
			b.tokens = burst
		}
		b.last = now
	}
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}
