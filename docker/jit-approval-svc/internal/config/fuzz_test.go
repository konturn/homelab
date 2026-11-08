package config

import (
	"testing"
	"time"
)

// FuzzTTLFor: the granted TTL is the per-resource override if there is one,
// else the tier default; unknown tiers are an error, never a zero or
// unbounded TTL.
func FuzzTTLFor(f *testing.F) {
	f.Add("gitlab-moltbot", 1)
	f.Add("ssh-nkontur", 2)
	f.Add("grafana", 1)
	f.Add("grafana", 0)
	f.Add("grafana", 99)
	f.Add("", -1)
	cfg := &Config{
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute, AutoApprove: true},
			2: {TTL: 30 * time.Minute},
			3: {TTL: 60 * time.Minute},
		},
		ResourceTTLOverrides: map[string]time.Duration{
			"ssh-nkontur":    8 * time.Hour,
			"gitlab-moltbot": time.Hour,
		},
	}
	f.Fuzz(func(t *testing.T, resource string, tier int) {
		ttl, err := cfg.TTLFor(resource, tier)
		if o, ok := cfg.ResourceTTLOverrides[resource]; ok {
			if err != nil || ttl != o {
				t.Fatalf("TTLFor(%q,%d) = %v,%v want override %v", resource, tier, ttl, err, o)
			}
			return
		}
		tc, ok := cfg.Tiers[tier]
		if !ok {
			if err == nil {
				t.Fatalf("TTLFor(%q, unknown tier %d) = %v, no error", resource, tier, ttl)
			}
			return
		}
		if err != nil || ttl != tc.TTL || ttl <= 0 {
			t.Fatalf("TTLFor(%q,%d) = %v,%v want %v", resource, tier, ttl, err, tc.TTL)
		}
	})
}
