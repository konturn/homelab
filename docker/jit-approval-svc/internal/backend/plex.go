package backend

import (
	"encoding/xml"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// PlexBackend mints transient Plex tokens via the Plex server API.
type PlexBackend struct {
	baseURL     string
	vaultReader VaultSecretReader
	vaultPath   string
	http        *http.Client
}

// mediaContainer represents the XML response from Plex's security/token endpoint.
type mediaContainer struct {
	XMLName xml.Name `xml:"MediaContainer"`
	Token   string   `xml:"token,attr"`
}

// NewPlexBackend creates a Plex dynamic backend.
func NewPlexBackend(baseURL string, vaultReader VaultSecretReader) *PlexBackend {
	return &PlexBackend{
		baseURL:     strings.TrimRight(baseURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/plex",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// MintCredential obtains a transient Plex token.
func (b *PlexBackend) MintCredential(resource string, tier int, ttl time.Duration) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	serverToken, ok := secrets["token"]
	if !ok || serverToken == "" {
		return nil, fmt.Errorf("missing token in vault path %s", b.vaultPath)
	}

	// GET transient token
	url := fmt.Sprintf("%s/security/token?X-Plex-Token=%s", b.baseURL, serverToken)
	resp, err := b.http.Get(url)
	if err != nil {
		return nil, fmt.Errorf("get plex transient token: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("plex token endpoint returned %d: %s", resp.StatusCode, string(body))
	}

	var container mediaContainer
	if err := xml.Unmarshal(body, &container); err != nil {
		return nil, fmt.Errorf("parse plex XML response: %w", err)
	}

	if container.Token == "" {
		return nil, fmt.Errorf("plex returned empty transient token")
	}

	// Plex transient tokens last 48h, destroyed on restart
	leaseTTL := 48 * time.Hour
	if ttl < leaseTTL {
		leaseTTL = ttl
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "plex",
		"resource": resource,
		"tier":     tier,
		"ttl":      leaseTTL.String(),
	})

	return &Credential{
		Token:    container.Token,
		LeaseTTL: leaseTTL,
		Metadata: map[string]string{
			"type":    "transient",
			"backend": "plex",
		},
	}, nil
}

// Health checks if Plex is reachable.
func (b *PlexBackend) Health() error {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return fmt.Errorf("read vault secret for health: %w", err)
	}

	serverToken, ok := secrets["token"]
	if !ok || serverToken == "" {
		return fmt.Errorf("missing token for health check")
	}

	url := fmt.Sprintf("%s/identity?X-Plex-Token=%s", b.baseURL, serverToken)
	resp, err := b.http.Get(url)
	if err != nil {
		return fmt.Errorf("plex health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("plex health returned %d", resp.StatusCode)
	}
	return nil
}
