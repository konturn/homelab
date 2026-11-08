package backend

import (
	"fmt"
	"strings"
	"testing"
	"time"
)

// mockPolicyManager implements VaultPolicyManager for tests.
type mockPolicyManager struct {
	policies map[string]string
	putErr   error
	delErr   error
}

func newMockPolicyManager() *mockPolicyManager {
	return &mockPolicyManager{policies: make(map[string]string)}
}

func (m *mockPolicyManager) PutPolicy(name, rules string) error {
	if m.putErr != nil {
		return m.putErr
	}
	m.policies[name] = rules
	return nil
}

func (m *mockPolicyManager) DeletePolicy(name string) error {
	if m.delErr != nil {
		return m.delErr
	}
	delete(m.policies, name)
	return nil
}

func (m *mockPolicyManager) ListPolicies() ([]string, error) {
	names := make([]string, 0, len(m.policies))
	for n := range m.policies {
		names = append(names, n)
	}
	return names, nil
}

func (m *mockPolicyManager) GetPolicy(name string) (string, error) {
	rules, ok := m.policies[name]
	if !ok {
		return "", fmt.Errorf("policy %s not found", name)
	}
	return rules, nil
}

// TestMintCredential_RecordsExpiry: the generated policy carries its token's
// expiry, which is what the restart cleanup goes by.
func TestMintCredential_RecordsExpiry(t *testing.T) {
	pm := newMockPolicyManager()
	b := NewVaultDynamicBackend(&mockVaultMinter{token: "hvs.x", leaseID: "acc"}, pm)
	before := time.Now()
	_, err := b.MintCredential("vault", 2, 30*time.Minute, MintOptions{
		RequestID:  "req-expiry",
		VaultPaths: []VaultPathRequest{{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	exp, ok := PolicyExpiry(pm.policies["jit-vault-req-expiry"])
	if !ok {
		t.Fatalf("no expiry marker in policy:\n%s", pm.policies["jit-vault-req-expiry"])
	}
	want := before.Add(30 * time.Minute)
	if exp.Before(want.Add(-time.Second)) || exp.After(want.Add(time.Minute)) {
		t.Errorf("expiry %v, want about %v", exp, want)
	}
}

// TestCleanupExpiredPolicies: after a restart, only per-request policies
// whose token expired more than the grace period ago are deleted. Live
// grants, ones still within the grace, ones without a recorded expiry and
// every other policy are kept.
func TestCleanupExpiredPolicies(t *testing.T) {
	now := time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC)
	grace := PolicyCleanupGrace
	body := BuildPolicyHCL([]VaultPathRequest{{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}}})
	pm := newMockPolicyManager()
	pm.policies = map[string]string{
		"jit-vault-req-old":       withExpiry(body, now.Add(-time.Hour)),
		"jit-vault-req-justpast":  withExpiry(body, now.Add(-grace-time.Second)),
		"jit-vault-req-ingrace":   withExpiry(body, now.Add(-grace+time.Second)),
		"jit-vault-req-live":      withExpiry(body, now.Add(20*time.Minute)),
		"jit-vault-req-nomarker":  body,
		"jit-vault-req-badmarker": policyExpiryMarker + "yesterday\n" + body,
		"jit-static-plex":         withExpiry(body, now.Add(-time.Hour)),
		"ci-deploy":               "path \"homelab/data/*\" {}",
	}

	deleted, err := CleanupExpiredPolicies(pm, now, grace, time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 2 {
		t.Errorf("deleted %d, want 2", deleted)
	}
	for _, gone := range []string{"jit-vault-req-old", "jit-vault-req-justpast"} {
		if _, ok := pm.policies[gone]; ok {
			t.Errorf("%s not deleted", gone)
		}
	}
	for _, kept := range []string{"jit-vault-req-ingrace", "jit-vault-req-live", "jit-vault-req-nomarker",
		"jit-vault-req-badmarker", "jit-static-plex", "ci-deploy"} {
		if _, ok := pm.policies[kept]; !ok {
			t.Errorf("%s deleted", kept)
		}
	}
}

// TestCleanupExpiredPolicies_Unmarked: policies without an expiry marker
// (older broker versions) are kept until unmarkedDeadAfter, then deleted;
// marked live ones are still kept.
func TestCleanupExpiredPolicies_Unmarked(t *testing.T) {
	now := time.Date(2026, 11, 7, 12, 0, 0, 0, time.UTC)
	body := BuildPolicyHCL([]VaultPathRequest{{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}}})
	fresh := func() *mockPolicyManager {
		pm := newMockPolicyManager()
		pm.policies = map[string]string{
			"jit-vault-req-legacy": body,
			"jit-vault-req-live":   withExpiry(body, now.Add(50*time.Minute)),
			"jit-static-plex":      body,
		}
		return pm
	}

	pm := fresh()
	if n, _ := CleanupExpiredPolicies(pm, now, PolicyCleanupGrace, now.Add(time.Second)); n != 0 {
		t.Errorf("before unmarkedDeadAfter: deleted %d, want 0", n)
	}

	pm = fresh()
	if n, _ := CleanupExpiredPolicies(pm, now, PolicyCleanupGrace, now); n != 1 {
		t.Errorf("at unmarkedDeadAfter: deleted %d, want 1", n)
	}
	if _, ok := pm.policies["jit-vault-req-legacy"]; ok {
		t.Error("legacy policy not deleted")
	}
	for _, kept := range []string{"jit-vault-req-live", "jit-static-plex"} {
		if _, ok := pm.policies[kept]; !ok {
			t.Errorf("%s deleted", kept)
		}
	}
}

// TestCleanupExpiredPolicies_ListError: a listing error deletes nothing.
func TestCleanupExpiredPolicies_ListError(t *testing.T) {
	deleted, err := CleanupExpiredPolicies(&failingLister{}, time.Now(), PolicyCleanupGrace, time.Now())
	if err == nil || deleted != 0 {
		t.Fatalf("deleted %d, err %v; want 0 and an error", deleted, err)
	}
}

type failingLister struct{ mockPolicyManager }

func (*failingLister) ListPolicies() ([]string, error) { return nil, fmt.Errorf("permission denied") }

// --- ValidateVaultPaths tests ---

func TestValidateVaultPaths_Valid(t *testing.T) {
	paths := []VaultPathRequest{
		{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}},
		{Path: "homelab/data/infrastructure/unifi", Capabilities: []string{"read", "update"}},
	}
	if err := ValidateVaultPaths(paths); err != nil {
		t.Fatalf("expected valid, got: %v", err)
	}
}

func TestValidateVaultPaths_Empty(t *testing.T) {
	err := ValidateVaultPaths(nil)
	if err == nil {
		t.Fatal("expected error for empty paths")
	}
}

func TestValidateVaultPaths_TooMany(t *testing.T) {
	paths := make([]VaultPathRequest, 11)
	for i := range paths {
		paths[i] = VaultPathRequest{Path: "homelab/data/test", Capabilities: []string{"read"}}
	}
	err := ValidateVaultPaths(paths)
	if err == nil || !strings.Contains(err.Error(), "too many") {
		t.Fatalf("expected 'too many' error, got: %v", err)
	}
}

func TestValidateVaultPaths_BadPrefix(t *testing.T) {
	tests := []struct {
		name string
		path string
	}{
		{"auth path", "auth/token/create"},
		{"sys path", "sys/policies/acl/test"},
		{"secret path", "secret/data/test"},
		{"no prefix", "docker/nginx"},
		{"empty", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []VaultPathRequest{{Path: tt.path, Capabilities: []string{"read"}}}
			err := ValidateVaultPaths(paths)
			if err == nil {
				t.Fatalf("expected error for path %q", tt.path)
			}
		})
	}
}

func TestValidateVaultPaths_PathTraversal(t *testing.T) {
	tests := []struct {
		name    string
		path    string
		errText string
	}{
		{"dotdot in middle", "homelab/data/../secret/admin", "illegal traversal"},
		{"dotdot at start", "../etc/passwd", "illegal traversal"},
		{"dotdot at end", "homelab/data/test/..", "illegal traversal"},
		{"encoded traversal", "homelab/data/..%2fsecret", "invalid characters"}, // % blocked by regex before traversal check
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []VaultPathRequest{{Path: tt.path, Capabilities: []string{"read"}}}
			err := ValidateVaultPaths(paths)
			if err == nil {
				t.Fatalf("expected error for path traversal in %q", tt.path)
			}
			if !strings.Contains(err.Error(), tt.errText) {
				t.Fatalf("expected %q error, got: %v", tt.errText, err)
			}
		})
	}
}

func TestValidateVaultPaths_BadCapabilities(t *testing.T) {
	tests := []struct {
		name string
		caps []string
	}{
		{"delete", []string{"delete"}},
		{"sudo", []string{"sudo"}},
		{"mixed bad", []string{"read", "delete"}},
		{"empty caps", []string{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []VaultPathRequest{{Path: "homelab/data/test", Capabilities: tt.caps}}
			err := ValidateVaultPaths(paths)
			if err == nil {
				t.Fatalf("expected error for capabilities %v", tt.caps)
			}
		})
	}
}

func TestValidateVaultPaths_AllAllowedCaps(t *testing.T) {
	paths := []VaultPathRequest{
		{Path: "homelab/data/test", Capabilities: []string{"read", "list", "create", "update"}},
	}
	if err := ValidateVaultPaths(paths); err != nil {
		t.Fatalf("all allowed caps should pass: %v", err)
	}
}

// --- BuildPolicyHCL tests ---

func TestBuildPolicyHCL_SinglePath(t *testing.T) {
	paths := []VaultPathRequest{
		{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}},
	}
	hcl := BuildPolicyHCL(paths)

	if !strings.Contains(hcl, `path "homelab/data/docker/nginx"`) {
		t.Errorf("expected path in HCL, got:\n%s", hcl)
	}
	if !strings.Contains(hcl, `"read"`) {
		t.Errorf("expected read capability in HCL, got:\n%s", hcl)
	}
}

func TestBuildPolicyHCL_MultiplePaths(t *testing.T) {
	paths := []VaultPathRequest{
		{Path: "homelab/data/docker/a", Capabilities: []string{"read", "update"}},
		{Path: "homelab/data/docker/b", Capabilities: []string{"list"}},
	}
	hcl := BuildPolicyHCL(paths)

	if !strings.Contains(hcl, `path "homelab/data/docker/a"`) {
		t.Error("missing path a")
	}
	if !strings.Contains(hcl, `path "homelab/data/docker/b"`) {
		t.Error("missing path b")
	}
	if !strings.Contains(hcl, `"read", "update"`) {
		t.Error("missing capabilities for path a")
	}
}

// --- MintCredential tests ---

func TestVaultDynamicBackend_MintCredential(t *testing.T) {
	minter := &mockVaultMinter{token: "hvs.dynamic-token", leaseID: "acc-dyn"}
	pm := newMockPolicyManager()
	b := NewVaultDynamicBackend(minter, pm)

	opts := MintOptions{
		RequestID: "req-abc123",
		VaultPaths: []VaultPathRequest{
			{Path: "homelab/data/docker/nginx", Capabilities: []string{"read"}},
		},
	}

	cred, err := b.MintCredential("vault", 2, 30*time.Minute, opts)
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}
	if cred.Token != "hvs.dynamic-token" {
		t.Errorf("expected token hvs.dynamic-token, got %s", cred.Token)
	}
	if cred.Metadata["backend"] != "vault_dynamic" {
		t.Errorf("expected backend vault_dynamic, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["policy_name"] != "jit-vault-req-abc123" {
		t.Errorf("expected policy jit-vault-req-abc123, got %s", cred.Metadata["policy_name"])
	}

	// Verify policy was created
	if _, ok := pm.policies["jit-vault-req-abc123"]; !ok {
		t.Error("expected temporary policy to be created")
	}
}

func TestVaultDynamicBackend_MintCredential_NoPaths(t *testing.T) {
	minter := &mockVaultMinter{token: "hvs.x", leaseID: "acc"}
	pm := newMockPolicyManager()
	b := NewVaultDynamicBackend(minter, pm)

	_, err := b.MintCredential("vault", 2, 30*time.Minute, MintOptions{RequestID: "req-1"})
	if err == nil {
		t.Fatal("expected error for missing vault_paths")
	}
}

func TestVaultDynamicBackend_MintCredential_NoRequestID(t *testing.T) {
	minter := &mockVaultMinter{token: "hvs.x", leaseID: "acc"}
	pm := newMockPolicyManager()
	b := NewVaultDynamicBackend(minter, pm)

	opts := MintOptions{
		VaultPaths: []VaultPathRequest{{Path: "homelab/data/test", Capabilities: []string{"read"}}},
	}
	_, err := b.MintCredential("vault", 2, 30*time.Minute, opts)
	if err == nil {
		t.Fatal("expected error for missing request_id")
	}
}

func TestVaultDynamicBackend_MintCredential_PolicyCreateFails(t *testing.T) {
	minter := &mockVaultMinter{token: "hvs.x", leaseID: "acc"}
	pm := newMockPolicyManager()
	pm.putErr = fmt.Errorf("vault unavailable")
	b := NewVaultDynamicBackend(minter, pm)

	opts := MintOptions{
		RequestID:  "req-fail",
		VaultPaths: []VaultPathRequest{{Path: "homelab/data/test", Capabilities: []string{"read"}}},
	}
	_, err := b.MintCredential("vault", 2, 30*time.Minute, opts)
	if err == nil {
		t.Fatal("expected error when policy creation fails")
	}
}

func TestVaultDynamicBackend_MintCredential_TokenMintFails(t *testing.T) {
	minter := &mockVaultMinter{err: fmt.Errorf("token mint failed")}
	pm := newMockPolicyManager()
	b := NewVaultDynamicBackend(minter, pm)

	opts := MintOptions{
		RequestID:  "req-mintfail",
		VaultPaths: []VaultPathRequest{{Path: "homelab/data/test", Capabilities: []string{"read"}}},
	}
	_, err := b.MintCredential("vault", 2, 30*time.Minute, opts)
	if err == nil {
		t.Fatal("expected error when token minting fails")
	}

	// Policy should be cleaned up on mint failure
	if _, ok := pm.policies["jit-vault-req-mintfail"]; ok {
		t.Error("expected policy to be cleaned up after mint failure")
	}
}

func TestValidateVaultPaths_InvalidCharacters(t *testing.T) {
	tests := []struct {
		name string
		path string
	}{
		{"HCL close brace", "homelab/data/foo}bar"},
		{"HCL open brace", "homelab/data/foo{bar"},
		{"HCL double quote", `homelab/data/foo"bar`},
		{"HCL single quote", "homelab/data/foo'bar"},
		{"HCL injection full", `homelab/data/foo}path "bar" { capabilities = ["sudo"]`},
		{"newline injection", "homelab/data/foo\nbar"},
		{"carriage return", "homelab/data/foo\rbar"},
		{"space in path", "homelab/data/foo bar"},
		{"semicolon", "homelab/data/foo;bar"},
		{"backtick", "homelab/data/foo\x60bar"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []VaultPathRequest{{Path: tt.path, Capabilities: []string{"read"}}}
			err := ValidateVaultPaths(paths)
			if err == nil {
				t.Fatalf("expected error for path %q", tt.path)
			}
			if !strings.Contains(err.Error(), "invalid characters") {
				t.Fatalf("expected 'invalid characters' error, got: %v", err)
			}
		})
	}
}

func TestValidateVaultPaths_WildcardAllowed(t *testing.T) {
	tests := []struct {
		name string
		path string
	}{
		{"wildcard all", "homelab/data/*"},
		{"wildcard subpath", "homelab/data/docker/*"},
		{"wildcard nested", "homelab/data/docker/*/config"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			paths := []VaultPathRequest{{Path: tt.path, Capabilities: []string{"read"}}}
			if err := ValidateVaultPaths(paths); err != nil {
				t.Fatalf("wildcard path %q should be allowed: %v", tt.path, err)
			}
		})
	}
}

func TestValidateVaultPaths_ValidDeepPath(t *testing.T) {
	paths := []VaultPathRequest{
		{Path: "homelab/data/docker/plex", Capabilities: []string{"read"}},
	}
	if err := ValidateVaultPaths(paths); err != nil {
		t.Fatalf("expected valid deep path to pass: %v", err)
	}
}

// --- Broker-only secrets ---

func TestValidateVaultPaths_BrokerOnlyRejected(t *testing.T) {
	for _, p := range BrokerOnlyPaths {
		for _, caps := range [][]string{{"read"}, {"update"}, {"list"}} {
			err := ValidateVaultPaths([]VaultPathRequest{{Path: p, Capabilities: caps}})
			if err == nil || !strings.Contains(err.Error(), "reserved for the broker") {
				t.Errorf("%s %v: expected reserved-path error, got %v", p, caps, err)
			}
		}
	}
}

// A wildcard request covering a broker-only path still cannot read it: the
// generated policy carries an exact-path deny for each one.
func TestBuildPolicyHCL_DeniesBrokerOnlyPaths(t *testing.T) {
	paths := []VaultPathRequest{{Path: "homelab/data/docker/*", Capabilities: []string{"read", "list"}}}
	if err := ValidateVaultPaths(paths); err != nil {
		t.Fatalf("wildcard request should validate: %v", err)
	}
	hcl := BuildPolicyHCL(paths)
	for _, p := range BrokerOnlyPaths {
		want := fmt.Sprintf("path %q {\n  capabilities = [\"deny\"]\n}", p)
		if !strings.Contains(hcl, want) {
			t.Errorf("missing deny for %s in:\n%s", p, hcl)
		}
	}
}

// Every Vault path a dynamic backend reads its admin credentials from is in
// BrokerOnlyPaths.
func TestBrokerOnlyPaths_CoverDynamicBackendSecrets(t *testing.T) {
	backendPaths := map[string]string{
		"grafana":       NewGrafanaBackend("http://x", nil).vaultPath,
		"influxdb":      NewInfluxDBBackend("http://x", nil).vaultPath,
		"homeassistant": NewHomeAssistantBackend("http://x", nil).vaultPath,
		"paperless":     NewPaperlessBackend("http://x", nil).vaultPath,
		"tailscale":     NewTailscaleBackend("http://x", nil).vaultPath,
		"gmail":         NewGmailBackend("http://x", nil, GmailScopeRead).vaultPath,
	}
	for name, p := range backendPaths {
		if !isBrokerOnly(p) {
			t.Errorf("%s backend reads %s, which is not in BrokerOnlyPaths", name, p)
		}
	}
}

// The broker-only block only ever denies, and only the listed paths.
func TestBrokerOnlyDenyHCL_OnlyDenies(t *testing.T) {
	var paths int
	for _, line := range strings.Split(brokerOnlyDenyHCL(), "\n") {
		switch {
		case line == "", line == "}", strings.HasPrefix(line, "#"):
		case strings.HasPrefix(line, "path "):
			p := strings.TrimSuffix(strings.TrimPrefix(line, "path "), " {")
			if !isBrokerOnly(strings.Trim(p, `"`)) {
				t.Errorf("unexpected path line %q", line)
			}
			paths++
		case line == `  capabilities = ["deny"]`:
		default:
			t.Errorf("unexpected line %q", line)
		}
	}
	if paths != len(BrokerOnlyPaths) {
		t.Errorf("%d deny stanzas, want %d", paths, len(BrokerOnlyPaths))
	}
}
