package vault

import (
	"strings"
	"testing"
)

// FuzzPoliciesForResource: the static fallback (a plain Vault token) gets a
// policy only for a resource listed in resourceTier, byte for byte, at
// exactly its tier. Unknown names, case/whitespace/unicode variants and
// tier mismatches get no policy, so the mint fails closed.
//
//	go test ./internal/vault -run '^$' -fuzz '^FuzzPoliciesForResource$' -fuzztime 1m
func FuzzPoliciesForResource(f *testing.F) {
	for res, tier := range resourceTier {
		f.Add(res, tier)
		f.Add(strings.ToUpper(res), tier)
		f.Add(res+" ", tier)
		f.Add(res, tier+1)
		f.Add(res, tier-1)
	}
	f.Add("gitlab-mоltbot", 1)
	f.Add("", 1)
	f.Add("unknown", 2)
	f.Fuzz(func(t *testing.T, resource string, tier int) {
		p := policiesForResource(resource, tier)
		want, known := resourceTier[resource]
		if !known || tier != want {
			if p != nil {
				t.Fatalf("policiesForResource(%q, %d) = %v, want none", resource, tier, p)
			}
			return
		}
		if len(p) != 1 || p[0] != tierPolicy[tier] {
			t.Fatalf("policiesForResource(%q, %d) = %v, want [%s]", resource, tier, p, tierPolicy[tier])
		}
		if MinTierForResource(resource) != tier {
			t.Fatalf("MinTierForResource(%q) = %d, want %d", resource, MinTierForResource(resource), tier)
		}
	})
}

// TestPrivilegedResourcesNeverTier1 pins the resources that must always need
// a human approval: anything that can change infrastructure, read other
// people's mail as send, or mint further credentials.
func TestPrivilegedResourcesNeverTier1(t *testing.T) {
	for _, res := range []string{
		"gitlab", "vault", "homeassistant", "tailscale", "pihole", "ipmi", "paperless", "gmail-send",
		"ssh-router", "ssh-satellite", "ssh-zwave", "ssh-nkontur", "ssh-konoahko", "ssh-konturn",
		"ssh-router-elevated", "ssh-zwave-elevated", "ssh-satellite-elevated", "ssh-macmini-elevated",
	} {
		if got := MinTierForResource(res); got < 2 {
			t.Errorf("%s: minimum tier %d, want >= 2", res, got)
		}
		if p := policiesForResource(res, 1); p != nil {
			t.Errorf("%s: tier 1 policy %v", res, p)
		}
	}
	// Every *-elevated resource needs approval, including future ones.
	for res, tier := range resourceTier {
		if strings.HasSuffix(res, "-elevated") && tier < 2 {
			t.Errorf("%s is tier %d", res, tier)
		}
	}
}
