package config

import (
	"os"
	"strings"
	"testing"
	"time"
)

func TestTierFor(t *testing.T) {
	cfg := &Config{
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute, Description: "Auto-approve services"},
			2: {TTL: 30 * time.Minute, Description: "Infrastructure"},
			3: {TTL: 60 * time.Minute, Description: "Critical"},
		},
	}

	tests := []struct {
		tier    int
		wantTTL time.Duration
		wantErr bool
	}{
		{1, 15 * time.Minute, false},
		{2, 30 * time.Minute, false},
		{3, 60 * time.Minute, false},
		{0, 0, true},
		{4, 0, true},
		{-1, 0, true},
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
	}
}

func TestTTLFor(t *testing.T) {
	cfg := &Config{
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute},
			2: {TTL: 30 * time.Minute},
			3: {TTL: 60 * time.Minute},
		},
		ResourceTTLOverrides: map[string]time.Duration{
			"ssh-nkontur":  8 * time.Hour,
			"ssh-konoahko": 8 * time.Hour,
		},
	}

	tests := []struct {
		name     string
		resource string
		tier     int
		wantTTL  time.Duration
		wantErr  bool
	}{
		{"override resource gets override TTL", "ssh-nkontur", 2, 8 * time.Hour, false},
		{"another override resource", "ssh-konoahko", 2, 8 * time.Hour, false},
		{"override ignores tier", "ssh-nkontur", 1, 8 * time.Hour, false},
		{"non-override resource gets tier TTL", "grafana", 2, 30 * time.Minute, false},
		{"non-override T1", "homeassistant", 1, 15 * time.Minute, false},
		{"non-override T3", "vault", 3, 60 * time.Minute, false},
		{"non-override unknown tier errors", "grafana", 99, 0, true},
		{"override resource ignores bad tier", "ssh-nkontur", 99, 8 * time.Hour, false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := cfg.TTLFor(tt.resource, tt.tier)
			if tt.wantErr {
				if err == nil {
					t.Errorf("expected error, got TTL=%v", got)
				}
				return
			}
			if err != nil {
				t.Errorf("unexpected error: %v", err)
				return
			}
			if got != tt.wantTTL {
				t.Errorf("TTLFor(%q, %d) = %v, want %v", tt.resource, tt.tier, got, tt.wantTTL)
			}
		})
	}
}

func TestTTLForNilOverrides(t *testing.T) {
	cfg := &Config{
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute},
		},
	}

	// nil map should fall through to tier default
	got, err := cfg.TTLFor("anything", 1)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	if got != 15*time.Minute {
		t.Errorf("expected 15m, got %v", got)
	}
}

func TestValidate(t *testing.T) {
	base := Config{
		VaultAddr:             "https://vault.example.com",
		VaultRoleID:           "role-id",
		VaultSecretID:         "secret-id",
		TelegramBotToken:      "bot-token",
		TelegramWebhookSecret: "webhook-secret",
		RequesterKeys:         []RequesterKey{{Name: "openclaw-noah", Hash: HashKey("noah-key")}},
		ApprovalMatrix:        DefaultApprovalMatrix(),
		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute}, 2: {TTL: 30 * time.Minute}, 3: {TTL: 30 * time.Minute},
		},
	}

	if err := base.Validate(); err != nil {
		t.Errorf("expected valid config, got: %v", err)
	}

	for name, mutate := range map[string]func(c *Config){
		"missing VaultAddr":             func(c *Config) { c.VaultAddr = "" },
		"missing VaultRoleID":           func(c *Config) { c.VaultRoleID = "" },
		"missing VaultSecretID":         func(c *Config) { c.VaultSecretID = "" },
		"missing TelegramBotToken":      func(c *Config) { c.TelegramBotToken = "" },
		"missing TelegramWebhookSecret": func(c *Config) { c.TelegramWebhookSecret = "" },
		"no credential at all":          func(c *Config) { c.RequesterKeys = nil },
		"tier 3 auto": func(c *Config) {
			c.ApprovalMatrix = map[string]map[int]Decision{"openclaw-noah": {1: DecisionAuto, 2: DecisionAuto, 3: DecisionAuto}}
		},
		"tier 3 auto-fyi": func(c *Config) {
			c.ApprovalMatrix = map[string]map[int]Decision{"openclaw-noah": {1: DecisionAuto, 2: DecisionAuto, 3: DecisionAutoFYI}}
		},
		"row without tier 2": func(c *Config) {
			c.ApprovalMatrix = map[string]map[int]Decision{"openclaw-noah": {1: DecisionAuto, 3: DecisionDeny}}
		},
		"missing tier TTL": func(c *Config) { delete(c.Tiers, 3) },
	} {
		c := base
		c.Tiers = map[int]TierConfig{}
		for k, v := range base.Tiers {
			c.Tiers[k] = v
		}
		mutate(&c)
		if err := c.Validate(); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

// The shared key is gone: a deployment that still sets it does not start.
func TestLoadRefusesSharedKey(t *testing.T) {
	for k, v := range map[string]string{
		"VAULT_ROLE_ID": "r", "VAULT_SECRET_ID": "s", "TELEGRAM_BOT_TOKEN": "t",
		"TELEGRAM_WEBHOOK_SECRET": "w", "JIT_REQUESTER_KEYS": "openclaw-noah:" + hashHex("k"),
	} {
		t.Setenv(k, v)
	}
	if _, err := Load(); err != nil {
		t.Fatalf("valid config: %v", err)
	}
	t.Setenv("JIT_API_KEY", "shared")
	if _, err := Load(); err == nil || !strings.Contains(err.Error(), "JIT_API_KEY") {
		t.Fatalf("JIT_API_KEY set: err %v", err)
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
