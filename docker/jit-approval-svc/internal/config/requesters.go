package config

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
)

// Decision is what the broker does with a request, given who asked and the
// request's tier (ApprovalMatrix).
type Decision string

const (
	// DecisionDeny refuses the request outright. Nothing is stored or sent.
	DecisionDeny Decision = "deny"
	// DecisionApprove sends the Telegram approve/deny buttons.
	DecisionApprove Decision = "approve"
	// DecisionAuto mints immediately, without telling anyone.
	DecisionAuto Decision = "auto"
	// DecisionAutoFYI mints immediately and sends one informational
	// Telegram line (no buttons) per grant.
	DecisionAutoFYI Decision = "auto-fyi"
)

// Mints reports whether the decision mints without a human approval.
func (d Decision) Mints() bool {
	return d == DecisionAuto || d == DecisionAutoFYI
}

func parseDecision(s string) (Decision, error) {
	switch d := Decision(strings.TrimSpace(s)); d {
	case DecisionDeny, DecisionApprove, DecisionAuto, DecisionAutoFYI:
		return d, nil
	}
	return "", fmt.Errorf("unknown decision %q (want deny, approve, auto or auto-fyi)", s)
}

// MaxTier is the highest tier. Tier MaxTier is never minted without a human
// approval, for any requester (Validate refuses such a matrix).
const MaxTier = 3

// DefaultApprovalMatrix is used when JIT_APPROVAL_MATRIX is not set. A
// requester that is not in the matrix is denied everything.
//
//   - openclaw-noah: the agent bound to Noah's own Telegram DMs.
//   - openclaw-ops: the agent that runs crons and the event-router hook, which
//     read untrusted input (alerts, mail, web pages, MR text).
func DefaultApprovalMatrix() map[string]map[int]Decision {
	return map[string]map[int]Decision{
		"openclaw-noah": {1: DecisionAuto, 2: DecisionAutoFYI, 3: DecisionApprove},
		"openclaw-ops":  {1: DecisionAuto, 2: DecisionApprove, 3: DecisionDeny},
	}
}

// ParseApprovalMatrix parses JIT_APPROVAL_MATRIX:
//
//	openclaw-noah=auto,auto-fyi,approve;openclaw-ops=auto,approve,deny
//
// Each requester lists its decision for tier 1, 2 and 3 in order.
func ParseApprovalMatrix(s string) (map[string]map[int]Decision, error) {
	out := map[string]map[int]Decision{}
	for _, row := range strings.Split(s, ";") {
		row = strings.TrimSpace(row)
		if row == "" {
			continue
		}
		name, cells, ok := strings.Cut(row, "=")
		name = strings.TrimSpace(name)
		if !ok || !validRequesterName(name) {
			return nil, fmt.Errorf("approval matrix row %q: want <requester>=<t1>,<t2>,<t3>", row)
		}
		if _, dup := out[name]; dup {
			return nil, fmt.Errorf("approval matrix: requester %q listed twice", name)
		}
		parts := strings.Split(cells, ",")
		if len(parts) != MaxTier {
			return nil, fmt.Errorf("approval matrix row %q: want %d decisions, got %d", row, MaxTier, len(parts))
		}
		out[name] = map[int]Decision{}
		for i, p := range parts {
			d, err := parseDecision(p)
			if err != nil {
				return nil, fmt.Errorf("approval matrix row %q: %w", row, err)
			}
			out[name][i+1] = d
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("approval matrix is empty")
	}
	return out, nil
}

// ValidateApprovalMatrix checks the invariants that hold for any
// configuration: every row has a known decision for every tier, and the top
// tier is never minted without approval.
func ValidateApprovalMatrix(m map[string]map[int]Decision) error {
	for name, row := range m {
		if !validRequesterName(name) {
			return fmt.Errorf("approval matrix: invalid requester name %q", name)
		}
		for tier := 1; tier <= MaxTier; tier++ {
			d, ok := row[tier]
			if !ok {
				return fmt.Errorf("approval matrix: %s has no decision for tier %d", name, tier)
			}
			if _, err := parseDecision(string(d)); err != nil {
				return fmt.Errorf("approval matrix: %s tier %d: %w", name, tier, err)
			}
			if tier == MaxTier && d.Mints() {
				return fmt.Errorf("approval matrix: %s tier %d is %q; tier %d always needs approval", name, tier, d, MaxTier)
			}
		}
		for tier := range row {
			if tier < 1 || tier > MaxTier {
				return fmt.Errorf("approval matrix: %s has a decision for unknown tier %d", name, tier)
			}
		}
	}
	return nil
}

// DecisionFor returns the matrix decision for a requester at a tier. Anything
// not in the matrix (an unknown requester or tier) is denied.
func (c *Config) DecisionFor(requester string, tier int) Decision {
	row, ok := c.ApprovalMatrix[requester]
	if !ok {
		return DecisionDeny
	}
	d, ok := row[tier]
	if !ok {
		return DecisionDeny
	}
	if tier >= MaxTier && d.Mints() {
		// Validate refuses this; keep the invariant even if a test or a
		// future caller builds a Config by hand.
		return DecisionApprove
	}
	return d
}

// RequesterKey is one requester's credential: the SHA-256 of its bearer key.
// The broker never holds the key itself.
type RequesterKey struct {
	Name string
	Hash [sha256.Size]byte
}

// ParseRequesterKeys parses JIT_REQUESTER_KEYS:
//
//	openclaw-noah:<sha256 hex>,openclaw-ops:<sha256 hex>
func ParseRequesterKeys(s string) ([]RequesterKey, error) {
	var out []RequesterKey
	seenName := map[string]bool{}
	seenHash := map[[sha256.Size]byte]bool{}
	for _, item := range strings.Split(s, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		name, hexHash, ok := strings.Cut(item, ":")
		name = strings.TrimSpace(name)
		if !ok || !validRequesterName(name) {
			return nil, fmt.Errorf("requester key %q: want <requester>:<sha256 hex>", redact(item))
		}
		raw, err := hex.DecodeString(strings.TrimSpace(hexHash))
		if err != nil || len(raw) != sha256.Size {
			return nil, fmt.Errorf("requester key for %s: hash is not 64 hex characters", name)
		}
		var h [sha256.Size]byte
		copy(h[:], raw)
		if seenName[name] {
			return nil, fmt.Errorf("requester %s has more than one key", name)
		}
		if seenHash[h] {
			return nil, fmt.Errorf("requester %s shares a key with another requester", name)
		}
		seenName[name], seenHash[h] = true, true
		out = append(out, RequesterKey{Name: name, Hash: h})
	}
	return out, nil
}

// HashKey returns the SHA-256 of a bearer key, as stored in JIT_REQUESTER_KEYS.
func HashKey(key string) [sha256.Size]byte {
	return sha256.Sum256([]byte(key))
}

// Authenticate maps a bearer key to the requester it belongs to. Every
// configured credential is compared in constant time, whether or not an
// earlier one matched. The requester name in a request body is never used
// to identify the caller.
func (c *Config) Authenticate(key string) (string, bool) {
	if key == "" {
		return "", false
	}
	h := HashKey(key)
	name, found := "", 0
	for _, rk := range c.RequesterKeys {
		if subtle.ConstantTimeCompare(h[:], rk.Hash[:]) == 1 {
			name, found = rk.Name, found+1
		}
	}
	if found != 1 {
		return "", false
	}
	return name, true
}

// Requesters lists the requesters that have a credential, sorted.
func (c *Config) Requesters() []string {
	var out []string
	for _, rk := range c.RequesterKeys {
		out = append(out, rk.Name)
	}
	sort.Strings(out)
	return out
}

func validRequesterName(s string) bool {
	if s == "" || len(s) > 64 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return false
		}
	}
	return true
}

// redact keeps a malformed entry out of the logs except for its name part.
func redact(item string) string {
	if name, _, ok := strings.Cut(item, ":"); ok {
		return name + ":…"
	}
	if len(item) > 8 {
		return item[:4] + "…"
	}
	return "…"
}
