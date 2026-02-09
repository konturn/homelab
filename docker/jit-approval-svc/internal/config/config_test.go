package config

import (
	"os"
	"testing"
	"time"
)

func TestTierFor(t *testing.T) {
	cfg := &Config{
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute, AutoApprove: true, Description: "Auto-approve services"},
			2: {TTL: 30 * time.Minute, AutoApprove: false, Description: "Infrastructure"},
			3: {TTL: 60 * time.Minute, AutoApprove: false, Description: "Critical"},
		},
	}

	tests := []struct {
		tier     int
		wantTTL  time.Duration
		wantAuto bool
		wantErr  bool
	}{
		{1, 15 * time.Minute, true, false},
		{2, 30 * time.Minute, false, false},
		{3, 60 * time.Minute, false, false},
		{0, 0, false, true},
		{4, 0, false, true},
		{-1, 0, false, true},
	}

	for _, tt := range tests {
		tc, err := cfg.TierFor(tt.tier)
		if tt.wantErr {
			if err == nil {
				t.Errorf("tier %d: expected error", tt.tier)
			}
			continue
		}
		if err != nil {
			t.Errorf("tier %d: unexpected error: %v", tt.tier, err)
			continue
		}
		if tc.TTL != tt.wantTTL {
			t.Errorf("tier %d: TTL = %v, want %v", tt.tier, tc.TTL, tt.wantTTL)
		}
		if tc.AutoApprove != tt.wantAuto {
			t.Errorf("tier %d: AutoApprove = %v, want %v", tt.tier, tc.AutoApprove, tt.wantAuto)
		}
	}
}

func TestIsRequesterAllowed(t *testing.T) {
	cfg := &Config{
		AllowedRequesters: []string{"prometheus", "backup-agent"},
	}

	tests := []struct {
		requester string
		want      bool
	}{
		{"prometheus", true},
		{"backup-agent", true},
		{"unknown", false},
		{"", false},
	}

	for _, tt := range tests {
		got := cfg.IsRequesterAllowed(tt.requester)
		if got != tt.want {
			t.Errorf("IsRequesterAllowed(%q) = %v, want %v", tt.requester, got, tt.want)
		}
	}
}

func TestValidate(t *testing.T) {
	base := Config{
		VaultAddr:             "https://vault.example.com",
		VaultRoleID:           "role-id",
		VaultSecretID:         "secret-id",
		TelegramBotToken:      "bot-token",
		TelegramWebhookSecret: "webhook-secret",
		JITAPIKey:             "test-api-key",
	}

	// Valid config
	if err := base.Validate(); err != nil {
		t.Errorf("expected valid config, got: %v", err)
	}

	// Missing VaultAddr
	c := base
	c.VaultAddr = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing VaultAddr")
	}

	// Missing VaultRoleID
	c = base
	c.VaultRoleID = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing VaultRoleID")
	}

	// Missing VaultSecretID
	c = base
	c.VaultSecretID = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing VaultSecretID")
	}

	// Missing TelegramBotToken
	c = base
	c.TelegramBotToken = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing TelegramBotToken")
	}

	// Missing TelegramWebhookSecret
	c = base
	c.TelegramWebhookSecret = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing TelegramWebhookSecret")
	}

	// Missing JITAPIKey
	c = base
	c.JITAPIKey = ""
	if err := c.Validate(); err == nil {
		t.Error("expected error for missing JITAPIKey")
	}
}

func TestBackendURLDefaults(t *testing.T) {
	// When env vars are not set, defaults should be used
	cfg := &Config{
		HAURL:       getEnvOrEmpty("HA_URL_TEST_UNSET", "https://homeassistant.lab.nkontur.com"),
		GrafanaURL:  getEnvOrEmpty("GRAFANA_URL_TEST_UNSET", "https://grafana.lab.nkontur.com"),
		InfluxDBURL: getEnvOrEmpty("INFLUXDB_URL_TEST_UNSET", "https://influxdb.lab.nkontur.com:8086"),
	}

	if cfg.HAURL != "https://homeassistant.lab.nkontur.com" {
		t.Errorf("expected default HA_URL, got %s", cfg.HAURL)
	}
	if cfg.GrafanaURL != "https://grafana.lab.nkontur.com" {
		t.Errorf("expected default GRAFANA_URL, got %s", cfg.GrafanaURL)
	}
	if cfg.InfluxDBURL != "https://influxdb.lab.nkontur.com:8086" {
		t.Errorf("expected default INFLUXDB_URL, got %s", cfg.InfluxDBURL)
	}
}

func TestBackendURLOverride(t *testing.T) {
	os.Setenv("HA_URL_TEST", "https://custom-ha.example.com")
	defer os.Unsetenv("HA_URL_TEST")

	val := getEnvOrEmpty("HA_URL_TEST", "https://homeassistant.lab.nkontur.com")
	if val != "https://custom-ha.example.com" {
		t.Errorf("expected custom HA URL, got %s", val)
	}
}

func TestBackendURLDisable(t *testing.T) {
	// Explicitly setting to empty string should disable the backend
	os.Setenv("HA_URL_TEST_EMPTY", "")
	defer os.Unsetenv("HA_URL_TEST_EMPTY")

	val := getEnvOrEmpty("HA_URL_TEST_EMPTY", "https://homeassistant.lab.nkontur.com")
	if val != "" {
		t.Errorf("expected empty (disabled), got %s", val)
	}
}
