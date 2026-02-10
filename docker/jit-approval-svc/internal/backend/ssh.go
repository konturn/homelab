package backend

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/pem"
	"fmt"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
	"golang.org/x/crypto/ssh"
)

// VaultSSHSigner signs SSH public keys via Vault's SSH secrets engine.
type VaultSSHSigner interface {
	SignSSHKey(role string, publicKey string, validPrincipals string, ttl string) (string, error)
}

// sshResourceConfig maps JIT resource names to Vault SSH role and principal.
var sshResourceConfig = map[string]struct {
	role      string
	principal string
}{
	"ssh":           {role: "claude", principal: "claude"},
	"ssh-satellite": {role: "satellite", principal: "claude-satellite"},
	"ssh-zwave":     {role: "zwave", principal: "claude-zwave"},
	"ssh-nkontur":        {role: "nkontur-ws", principal: "claude-nkontur"},
	"ssh-konoahko":        {role: "konoahko-ws", principal: "claude-konoahko"},
	"ssh-konturn":        {role: "konturn-ws", principal: "claude-konturn"},
}

// SSHBackend mints ephemeral SSH certificates via Vault's SSH CA.
// It generates a temporary ed25519 keypair, sends the public key to Vault
// for signing, and returns both the signed certificate and private key.
type SSHBackend struct {
	signer    VaultSSHSigner
	vaultPath string
}

// NewSSHBackend creates an SSH certificate backend.
func NewSSHBackend(signer VaultSSHSigner, vaultPath string) *SSHBackend {
	return &SSHBackend{
		signer:    signer,
		vaultPath: vaultPath,
	}
}

// MintCredential generates a temporary SSH keypair and gets it signed by Vault.
func (b *SSHBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
	// Look up role and principal for this resource
	cfg, ok := sshResourceConfig[resource]
	if !ok {
		return nil, fmt.Errorf("unknown SSH resource: %s", resource)
	}

	// Generate ephemeral ed25519 keypair
	pubKey, privKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("generate ed25519 key: %w", err)
	}

	// Convert to SSH public key format
	sshPubKey, err := ssh.NewPublicKey(pubKey)
	if err != nil {
		return nil, fmt.Errorf("convert to ssh public key: %w", err)
	}
	pubKeyStr := string(ssh.MarshalAuthorizedKey(sshPubKey))

	// Convert private key to PEM format
	privKeyPEM, err := ssh.MarshalPrivateKey(privKey, "")
	if err != nil {
		return nil, fmt.Errorf("marshal private key: %w", err)
	}
	privKeyStr := string(pem.EncodeToMemory(privKeyPEM))

	// Sign via Vault using the resource-specific role and principal
	ttlStr := fmt.Sprintf("%ds", int(ttl.Seconds()))
	signedCert, err := b.signer.SignSSHKey(cfg.role, pubKeyStr, cfg.principal, ttlStr)
	if err != nil {
		return nil, fmt.Errorf("vault ssh sign: %w", err)
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":   "ssh",
		"resource":  resource,
		"role":      cfg.role,
		"principal": cfg.principal,
		"tier":      tier,
		"ttl":       ttl.String(),
	})

	return &Credential{
		Token:    privKeyStr,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"backend":     "ssh",
			"type":        "ssh_certificate",
			"certificate": signedCert,
			"principal":   cfg.principal,
		},
	}, nil
}

// Health checks if the Vault SSH backend is reachable.
func (b *SSHBackend) Health() error {
	// Health is checked via Vault's general health endpoint
	return nil
}
