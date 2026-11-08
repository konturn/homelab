package vault

import (
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
)

// staticTier1 and staticTier2 are the static-backend resources by tier.
var staticTier1 = []string{"plex", "radarr", "sonarr", "ombi", "nzbget", "deluge", "prowlarr", "tautulli", "mqtt"}
var staticTier2 = []string{"pihole", "ipmi"}

func TestPoliciesForResource_StaticGetsOwnPolicy(t *testing.T) {
	for tier, list := range map[int][]string{1: staticTier1, 2: staticTier2} {
		for _, res := range list {
			got := policiesForResource(res, tier)
			want := []string{"jit-static-" + res}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("%s tier %d: got %v, want %v", res, tier, got, want)
			}
		}
	}
}

// A token for one static resource must not carry any policy that grants
// another resource's path: its only policy is its own, and that policy's
// paths belong to no other resource.
func TestPoliciesForResource_IsolatedPerResource(t *testing.T) {
	owner := map[string]string{}
	for res, paths := range staticResourcePaths {
		if len(paths) == 0 {
			t.Errorf("%s: no paths", res)
		}
		for _, p := range paths {
			if other, dup := owner[p]; dup {
				t.Errorf("path %s granted to both %s and %s", p, other, res)
			}
			owner[p] = res
			if strings.ContainsAny(p, "*+") {
				t.Errorf("%s: path %s is a glob; static grants must be exact", res, p)
			}
		}
	}
	for res := range staticResourcePaths {
		pols := policiesForResource(res, resourceTier[res])
		if len(pols) != 1 || pols[0] != StaticPolicyPrefix+res {
			t.Fatalf("%s: unexpected policies %v", res, pols)
		}
		for other := range staticResourcePaths {
			if other != res && pols[0] == StaticPolicyPrefix+other {
				t.Errorf("%s token gets %s's policy", res, other)
			}
		}
		for _, p := range pols {
			if p == "jit-tier1-services" || p == "jit-tier2-infra" {
				t.Errorf("%s: tier-wide policy %s attached", res, p)
			}
		}
	}
}

// Resources with a dynamic backend never get a static Vault token, even if
// their backend is unconfigured and the registry falls back to static.
func TestPoliciesForResource_DynamicResourcesRefused(t *testing.T) {
	dynamic := []string{"grafana", "influxdb", "homeassistant", "gitlab", "gitlab-moltbot", "paperless",
		"tailscale", "gmail-read", "gmail-send", "vault", "ssh-router", "ssh-macmini"}
	for _, res := range dynamic {
		if _, static := staticResourcePaths[res]; static {
			t.Errorf("%s has a dynamic backend but is in staticResourcePaths", res)
		}
		if got := policiesForResource(res, resourceTier[res]); got != nil {
			t.Errorf("%s: expected no static policy, got %v", res, got)
		}
	}
}

// Every static resource is a known resource with a tier.
func TestStaticResourcesHaveTier(t *testing.T) {
	for res := range staticResourcePaths {
		if _, ok := resourceTier[res]; !ok {
			t.Errorf("%s in staticResourcePaths but not in resourceTier", res)
		}
	}
}

// staticResourcesFromTerraform parses local.jit_static_resources from
// terraform/vault/policies.tf.
func staticResourcesFromTerraform(t *testing.T) map[string][]string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "terraform", "vault", "policies.tf"))
	if err != nil {
		t.Skipf("policies.tf not available: %v", err)
	}
	block := regexp.MustCompile(`(?s)jit_static_resources\s*=\s*\{(.*?)\n  \}`).FindSubmatch(src)
	if block == nil {
		t.Fatal("local.jit_static_resources not found in policies.tf")
	}
	out := map[string][]string{}
	entry := regexp.MustCompile(`(?m)^\s*([a-z0-9-]+)\s*=\s*\[([^\]]*)\]`)
	quoted := regexp.MustCompile(`"([^"]+)"`)
	for _, m := range entry.FindAllSubmatch(block[1], -1) {
		var paths []string
		for _, q := range quoted.FindAllSubmatch(m[2], -1) {
			paths = append(paths, string(q[1]))
		}
		out[string(m[1])] = paths
	}
	return out
}

func TestStaticResourcesMatchTerraform(t *testing.T) {
	tf := staticResourcesFromTerraform(t)
	if !reflect.DeepEqual(tf, staticResourcePaths) {
		t.Errorf("staticResourcePaths and local.jit_static_resources differ:\n go: %v\n tf: %v", staticResourcePaths, tf)
	}
}

func TestPoliciesForResource_RemovedResources(t *testing.T) {
	removedResources := []string{"cameras"}

	for _, res := range removedResources {
		for tier := 0; tier <= 3; tier++ {
			policies := policiesForResource(res, tier)
			if policies != nil {
				t.Errorf("%s tier %d: expected nil for removed resource, got %v", res, tier, policies)
			}
		}
	}
}

func TestPoliciesForResource_UnknownResource(t *testing.T) {
	policies := policiesForResource("nonexistent", 0)
	if policies != nil {
		t.Errorf("expected nil for unknown resource, got %v", policies)
	}
}

func TestPoliciesForResource_WrongTier(t *testing.T) {
	// grafana is tier 1, requesting tier 2 should fail
	policies := policiesForResource("grafana", 2)
	if policies != nil {
		t.Errorf("expected nil for wrong tier, got %v", policies)
	}

	// radarr is tier 1, requesting tier 0 should fail
	policies = policiesForResource("radarr", 0)
	if policies != nil {
		t.Errorf("expected nil for wrong tier, got %v", policies)
	}
}

func TestMinTierForResource(t *testing.T) {
	if got := MinTierForResource("ssh-router"); got != 2 {
		t.Errorf("SSH min tier: expected 2, got %d", got)
	}
	if got := MinTierForResource("grafana"); got != 1 {
		t.Errorf("grafana min tier: expected 1, got %d", got)
	}
	if got := MinTierForResource("gitlab-moltbot"); got != 1 {
		t.Errorf("gitlab-moltbot tier = %d, want 1", got)
	}
	if got := MinTierForResource("gitlab"); got != 2 {
		t.Errorf("gitlab tier = %d, want 2", got)
	}
	if got := MinTierForResource("unknown"); got != 0 {
		t.Errorf("unknown min tier: expected 0, got %d", got)
	}
	// An agent identity other than the reviewed one needs approval.
	for _, res := range []string{"gitlab-root", "gitlab-alice", "gitlab-moltbot2", "gitlab-"} {
		if got := MinTierForResource(res); got < 2 {
			t.Errorf("%s min tier = %d, want >= 2", res, got)
		}
	}
}

func TestResourceTierConsistency(t *testing.T) {
	for resource, tier := range resourceTier {
		if tier < 1 || tier > 3 {
			t.Errorf("resource %q maps to invalid tier %d", resource, tier)
		}
	}
}
