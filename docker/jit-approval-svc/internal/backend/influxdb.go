package backend

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// InfluxDBBackend mints ephemeral InfluxDB authorization tokens via the InfluxDB v2 API.
type InfluxDBBackend struct {
	baseURL     string
	vaultReader VaultSecretReader
	vaultPath   string
	http        *http.Client
}

// NewInfluxDBBackend creates an InfluxDB dynamic backend.
func NewInfluxDBBackend(baseURL string, vaultReader VaultSecretReader) *InfluxDBBackend {
	return &InfluxDBBackend{
		baseURL:     strings.TrimRight(baseURL, "/"),
		vaultReader: vaultReader,
		vaultPath:   "homelab/data/docker/influxdb",
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// MintCredential creates a read-only InfluxDB authorization token scoped to the org.
// It also schedules a goroutine to delete the token after TTL expiry.
func (b *InfluxDBBackend) MintCredential(resource string, tier int, ttl time.Duration) (*Credential, error) {
	secrets, err := b.vaultReader.ReadSecret(b.vaultPath)
	if err != nil {
		return nil, fmt.Errorf("read vault secret: %w", err)
	}

	adminToken, ok := secrets["admin_token"]
	if !ok || adminToken == "" {
		return nil, fmt.Errorf("missing admin_token in vault path %s", b.vaultPath)
	}
	orgID, ok := secrets["org_id"]
	if !ok || orgID == "" {
		return nil, fmt.Errorf("missing org_id in vault path %s", b.vaultPath)
	}

	// Create read-only authorization scoped to the org
	payload := map[string]interface{}{
		"orgID":       orgID,
		"description": fmt.Sprintf("jit-%d", time.Now().Unix()),
		"permissions": []map[string]interface{}{
			{
				"action": "read",
				"resource": map[string]interface{}{
					"type":  "buckets",
					"orgID": orgID,
				},
			},
			{
				"action": "read",
				"resource": map[string]interface{}{
					"type":  "orgs",
					"id":    orgID,
				},
			},
		},
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return nil, fmt.Errorf("marshal auth request: %w", err)
	}

	req, err := http.NewRequest(http.MethodPost, b.baseURL+"/api/v2/authorizations", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}
	req.Header.Set("Authorization", "Token "+adminToken)
	req.Header.Set("Content-Type", "application/json")

	resp, err := b.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("post influxdb authorization: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		return nil, fmt.Errorf("influxdb auth endpoint returned %d: %s", resp.StatusCode, string(respBody))
	}

	var authResp struct {
		ID    string `json:"id"`
		Token string `json:"token"`
	}
	if err := json.Unmarshal(respBody, &authResp); err != nil {
		return nil, fmt.Errorf("decode influxdb auth response: %w", err)
	}

	if authResp.Token == "" {
		return nil, fmt.Errorf("influxdb returned empty token")
	}

	logger.Info("backend_credential_minted", logger.Fields{
		"backend":  "influxdb",
		"resource": resource,
		"tier":     tier,
		"ttl":      ttl.String(),
	})

	// Schedule cleanup goroutine to delete the token after TTL
	go b.scheduleCleanup(authResp.ID, adminToken, ttl)

	return &Credential{
		Token:    authResp.Token,
		LeaseTTL: ttl,
		Metadata: map[string]string{
			"type":    "authorization",
			"backend": "influxdb",
			"org_id":  orgID,
		},
	}, nil
}

// scheduleCleanup deletes the InfluxDB authorization after the TTL expires.
func (b *InfluxDBBackend) scheduleCleanup(authID, adminToken string, ttl time.Duration) {
	timer := time.NewTimer(ttl)
	defer timer.Stop()
	<-timer.C

	logger.Info("influxdb_cleanup_start", logger.Fields{
		"auth_id": authID,
	})

	req, err := http.NewRequest(http.MethodDelete, b.baseURL+"/api/v2/authorizations/"+authID, nil)
	if err != nil {
		logger.Error("influxdb_cleanup_request_error", logger.Fields{
			"auth_id": authID,
			"error":   err.Error(),
		})
		return
	}
	req.Header.Set("Authorization", "Token "+adminToken)

	resp, err := b.http.Do(req)
	if err != nil {
		logger.Error("influxdb_cleanup_failed", logger.Fields{
			"auth_id": authID,
			"error":   err.Error(),
		})
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		logger.Error("influxdb_cleanup_bad_status", logger.Fields{
			"auth_id": authID,
			"status":  resp.StatusCode,
			"body":    string(body),
		})
		return
	}

	logger.Info("influxdb_cleanup_success", logger.Fields{
		"auth_id": authID,
	})
}

// Health checks if InfluxDB is reachable.
func (b *InfluxDBBackend) Health() error {
	resp, err := b.http.Get(b.baseURL + "/health")
	if err != nil {
		return fmt.Errorf("influxdb health check: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("influxdb health returned %d", resp.StatusCode)
	}
	return nil
}
