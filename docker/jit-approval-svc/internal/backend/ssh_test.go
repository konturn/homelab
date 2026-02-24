package backend

import (
	"fmt"
	"testing"
	"time"
)

// mockVaultSSHSigner implements VaultSSHSigner for tests.
type mockVaultSSHSigner struct {
	signedKey string
	err       error
}

func (m *mockVaultSSHSigner) SignSSHKey(role string, publicKey string, validPrincipals string, ttl string) (string, error) {
	if m.err != nil {
		return "", m.err
	}
	return m.signedKey, nil
}

func TestSSHBackend_MintCredential(t *testing.T) {
	signer := &mockVaultSSHSigner{
		signedKey: "ssh-ed25519-cert-v01@openssh.com AAAAMockSignedCert",
	}

	b := NewSSHBackend(signer, "ssh-client-signer")
	cred, err := b.MintCredential("ssh-router", 1, 15*time.Minute, MintOptions{})
	if err != nil {
		t.Fatalf("MintCredential failed: %v", err)
	}

	if cred.Token == "" {
		t.Error("expected non-empty private key in token")
	}
	if cred.LeaseTTL != 15*time.Minute {
		t.Errorf("expected TTL 15m, got %s", cred.LeaseTTL)
	}
	if cred.Metadata["backend"] != "ssh-router" {
		t.Errorf("expected backend ssh, got %s", cred.Metadata["backend"])
	}
	if cred.Metadata["type"] != "ssh_certificate" {
		t.Errorf("expected type ssh_certificate, got %s", cred.Metadata["type"])
	}
	if cred.Metadata["certificate"] != "ssh-ed25519-cert-v01@openssh.com AAAAMockSignedCert" {
		t.Errorf("expected signed cert in metadata, got %s", cred.Metadata["certificate"])
	}
	if cred.Metadata["principal"] != "claude" {
		t.Errorf("expected principal claude, got %s", cred.Metadata["principal"])
	}
}

func TestSSHBackend_MintCredential_PerHostRoles(t *testing.T) {
	tests := []struct {
		resource  string
		principal string
	}{
		{"ssh-satellite", "claude"},
		{"ssh-zwave", "claude"},
		{"ssh-nkontur", "nkontur"},
		{"ssh-konoahko", "konoahko"},
		{"ssh-konturn", "konturn"},
	}

	for _, tt := range tests {
		t.Run(tt.resource, func(t *testing.T) {
			signer := &mockVaultSSHSigner{
				signedKey: "ssh-ed25519-cert-v01@openssh.com AAAAMockSignedCert",
			}
			b := NewSSHBackend(signer, "ssh-client-signer")
			cred, err := b.MintCredential(tt.resource, 2, 30*time.Minute, MintOptions{})
			if err != nil {
				t.Fatalf("MintCredential(%s) failed: %v", tt.resource, err)
			}
			if cred.Metadata["principal"] != tt.principal {
				t.Errorf("expected principal %s, got %s", tt.principal, cred.Metadata["principal"])
			}
		})
	}
}

func TestSSHBackend_MintCredential_UnknownResource(t *testing.T) {
	signer := &mockVaultSSHSigner{
		signedKey: "cert",
	}
	b := NewSSHBackend(signer, "ssh-client-signer")
	_, err := b.MintCredential("ssh-unknown", 2, 30*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error for unknown SSH resource")
	}
}

func TestSSHBackend_MintCredential_SignError(t *testing.T) {
	signer := &mockVaultSSHSigner{
		err: fmt.Errorf("vault ssh sign failed"),
	}

	b := NewSSHBackend(signer, "ssh-client-signer")
	_, err := b.MintCredential("ssh-router", 1, 15*time.Minute, MintOptions{})
	if err == nil {
		t.Fatal("expected error from vault ssh sign failure")
	}
}

func TestSSHBackend_Health(t *testing.T) {
	signer := &mockVaultSSHSigner{}
	b := NewSSHBackend(signer, "ssh-client-signer")
	if err := b.Health(); err != nil {
		t.Errorf("Health() should return nil, got: %v", err)
	}
}
