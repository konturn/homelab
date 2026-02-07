package vault

import (
	"testing"
)

func TestPoliciesForResource_Tier0(t *testing.T) {
	tier0Resources := []string{"grafana", "influxdb"}

	for _, res := range tier0Resources {
		policies := policiesForResource(res, 0)
		if len(policies) != 1 {
			t.Errorf("%s tier 0: expected 1 policy, got %d", res, len(policies))
			continue
		}
		if policies[0] != "jit-tier0-monitoring" {
			t.Errorf("%s tier 0: expected jit-tier0-monitoring, got %s", res, policies[0])
		}
	}
}

func TestPoliciesForResource_Tier1(t *testing.T) {
	tier1Resources := []string{"plex", "radarr", "sonarr", "ombi", "nzbget", "deluge", "paperless"}

	for _, res := range tier1Resources {
		policies := policiesForResource(res, 1)
		if len(policies) != 1 {
			t.Errorf("%s tier 1: expected 1 policy, got %d", res, len(policies))
			continue
		}
		if policies[0] != "jit-tier1-services" {
			t.Errorf("%s tier 1: expected jit-tier1-services, got %s", res, policies[0])
		}
	}
}

func TestPoliciesForResource_Tier2(t *testing.T) {
	tier2Resources := []string{"gitlab", "homeassistant"}

	for _, res := range tier2Resources {
		policies := policiesForResource(res, 2)
		if len(policies) != 1 {
			t.Errorf("%s tier 2: expected 1 policy, got %d", res, len(policies))
			continue
		}
		if policies[0] != "jit-tier2-infra" {
			t.Errorf("%s tier 2: expected jit-tier2-infra, got %s", res, policies[0])
		}
	}
}

func TestPoliciesForResource_RemovedResources(t *testing.T) {
	// mqtt and cameras have been removed
	removedResources := []string{"mqtt", "cameras"}

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
	// grafana is tier 0, requesting tier 1 should fail
	policies := policiesForResource("grafana", 1)
	if policies != nil {
		t.Errorf("expected nil for wrong tier, got %v", policies)
	}

	// radarr is tier 1, requesting tier 0 should fail
	policies = policiesForResource("radarr", 0)
	if policies != nil {
		t.Errorf("expected nil for wrong tier, got %v", policies)
	}

}

func TestResourceTierConsistency(t *testing.T) {
	// Every resource's tier must have a corresponding policy
	for resource, tier := range resourceTier {
		if _, ok := tierPolicy[tier]; !ok {
			t.Errorf("resource %q maps to tier %d which has no policy", resource, tier)
		}
	}
}

