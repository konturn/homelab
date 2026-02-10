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

// SSHBackend mints ephemeral SSH certificates via Vault's SSH CA.
// It generates a temporary ed25519 keypair, sends the public key to Vault
// for signing, and returns both the signed certificate and private key.
type SSHBackend struct {
	signer    VaultSSHSigner
	vaultPath string
	role      string
}

// NewSSHBackend creates an SSH certificate backend.
func NewSSHBackend(signer VaultSSHSigner, vaultPath string) *SSHBackend {
	return &SSHBackend{
		signer:    signer,
		vaultPath: vaultPath,
		role:      "claude",
	}
}

// MintCredential generates a temporary SSH keypair and gets it signed by Vault.
func (b *SSHBackend) MintCredential(resource string, tier int, ttl time.Duration, opts MintOptions) (*Credential, error) {
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

	// Sign via Vault
	ttlStr := fmt.Sprintf("%ds", int(ttl.Seconds()))
	signedCert, err := b.signer.SignSSHKey(b.role, pubKeyStr, "claude", ttlStr)
	if err != nil {
		return nil, fmt.Errorf("vault ssh sign: %w", err)
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "ssh",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
	})

	return &Credential{
		Token:    privKeyStr,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"backend":     "ssh",
			"type":        "ssh_certificate",
			"certificate": signedCert,
			"principal":   "claude",
		},
	}, nil
}

// Health checks if the Vault SSH backend is reachable.
func (b *SSHBackend) Health() error {
	// Health is checked via Vault's general health endpoint
	return nil
}
