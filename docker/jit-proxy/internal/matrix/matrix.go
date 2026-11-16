// Package matrix is the requester x tier approval matrix of the JIT broker
// (docker/jit-approval-svc/internal/config/requesters.go), parsed from the
// same JIT_APPROVAL_MATRIX value. The proxy checks every grant against it at
// registration, so a grant the broker should not have issued (an unknown
// requester, a denied tier, an auto-approved grant where the matrix asks for a
// human) is refused here too.
package matrix

import (
	"fmt"
	"strings"
)

// Decision is what the matrix says for a requester at a tier.
type Decision string

const (
	Deny    Decision = "deny"
	Approve Decision = "approve"
	Auto    Decision = "auto"
	AutoFYI Decision = "auto-fyi"
)

// Mints reports whether the decision issues without a human approval.
func (d Decision) Mints() bool { return d == Auto || d == AutoFYI }

// MaxTier is the highest tier. It is never issued without approval.
const MaxTier = 3

// Matrix maps requester -> tier -> decision.
type Matrix map[string]map[int]Decision

// Default matches the broker's DefaultApprovalMatrix.
func Default() Matrix {
	return Matrix{
		"openclaw-noah": {1: Auto, 2: AutoFYI, 3: Approve},
		"openclaw-ops":  {1: Auto, 2: Approve, 3: Deny},
	}
}

func parseDecision(s string) (Decision, error) {
	switch d := Decision(strings.TrimSpace(s)); d {
	case Deny, Approve, Auto, AutoFYI:
		return d, nil
	}
	return "", fmt.Errorf("unknown decision %q", s)
}

// ValidName reports whether s is a valid requester name.
func ValidName(s string) bool {
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

// Parse parses "name=t1,t2,t3;name=..." (the broker's format).
func Parse(s string) (Matrix, error) {
	out := Matrix{}
	for _, row := range strings.Split(s, ";") {
		row = strings.TrimSpace(row)
		if row == "" {
			continue
		}
		name, cells, ok := strings.Cut(row, "=")
		name = strings.TrimSpace(name)
		if !ok || !ValidName(name) {
			return nil, fmt.Errorf("approval matrix row %q: want <requester>=<t1>,<t2>,<t3>", row)
		}
		if _, dup := out[name]; dup {
			return nil, fmt.Errorf("approval matrix: requester %q listed twice", name)
		}
		parts := strings.Split(cells, ",")
		if len(parts) != MaxTier {
			return nil, fmt.Errorf("approval matrix row %q: want %d decisions", row, MaxTier)
		}
		out[name] = map[int]Decision{}
		for i, p := range parts {
			d, err := parseDecision(p)
			if err != nil {
				return nil, fmt.Errorf("approval matrix row %q: %w", row, err)
			}
			if i+1 == MaxTier && d.Mints() {
				return nil, fmt.Errorf("approval matrix: %s tier %d must need approval", name, MaxTier)
			}
			out[name][i+1] = d
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("approval matrix is empty")
	}
	return out, nil
}

// Decide returns the decision for a requester at a tier; anything unknown is
// denied, and the top tier never mints without approval.
func (m Matrix) Decide(requester string, tier int) Decision {
	row, ok := m[requester]
	if !ok {
		return Deny
	}
	d, ok := row[tier]
	if !ok {
		return Deny
	}
	if tier >= MaxTier && d.Mints() {
		return Approve
	}
	return d
}
