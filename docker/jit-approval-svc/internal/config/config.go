package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// TierConfig holds the configuration for a given tier level.
type TierConfig struct {
	TTL         time.Duration
	AutoApprove bool
	Description string
}

// Config holds all service configuration sourced from environment variables.
type Config struct {
	VaultAddr     string
	VaultRoleID   string
	VaultSecretID string

	TelegramBotToken      string
	TelegramChatID        int64
	TelegramWebhookSecret string
	TelegramWebhookURL    string

	JITAPIKey string

	ListenAddr     string
	RequestTimeout time.Duration

	AllowedRequesters []string

	Tiers map[int]TierConfig

	// Backend service URLs (optional, enables dynamic credential backends)
	HAURL       string
	GrafanaURL  string
	InfluxDBURL string
	GitLabURL   string

	// GitLab admin token for creating project access tokens (Maintainer-level)
	GitLabAdminToken string

	// GitLab default project ID for access token creation (default: "4")
	GitLabProjectID string

	// Tailscale API URL (enables dynamic OAuth token backend)
	TailscaleAPIURL string

	// Paperless-ngx URL (enables dynamic API token backend)
	PaperlessURL string

	// SSH Vault path for certificate signing (default: ssh-client-signer)
	SSHVaultPath string
}

// Load reads configuration from environment variables.
func Load() (*Config, error) {
	chatID, err := strconv.ParseInt(getEnv("TELEGRAM_CHAT_ID", "8531859108"), 10, 64)
	if err != nil {
		return nil, fmt.Errorf("invalid TELEGRAM_CHAT_ID: %w", err)
	}

	timeoutSec, err := strconv.Atoi(getEnv("REQUEST_TIMEOUT", "1800"))
	if err != nil {
		return nil, fmt.Errorf("invalid REQUEST_TIMEOUT: %w", err)
	}

	requesters := strings.Split(getEnv("ALLOWED_REQUESTERS", "prometheus"), ",")
	for i := range requesters {
		requesters[i] = strings.TrimSpace(requesters[i])
	}

	cfg := &Config{
		VaultAddr:     getEnv("VAULT_ADDR", "https://vault.lab.nkontur.com:8200"),
		VaultRoleID:   os.Getenv("VAULT_ROLE_ID"),
		VaultSecretID: os.Getenv("VAULT_SECRET_ID"),

		TelegramBotToken:      os.Getenv("TELEGRAM_BOT_TOKEN"),
		TelegramChatID:        chatID,
		TelegramWebhookSecret: os.Getenv("TELEGRAM_WEBHOOK_SECRET"),
		TelegramWebhookURL:    os.Getenv("TELEGRAM_WEBHOOK_URL"),

		JITAPIKey: os.Getenv("JIT_API_KEY"),

		ListenAddr:     getEnv("LISTEN_ADDR", ":8080"),
		RequestTimeout: time.Duration(timeoutSec) * time.Second,

		AllowedRequesters: requesters,

		Tiers: map[int]TierConfig{
			1: {TTL: 15 * time.Minute, AutoApprove: true, Description: "Auto-approve services"},
			2: {TTL: 30 * time.Minute, AutoApprove: false, Description: "Infrastructure"},
			3: {TTL: 60 * time.Minute, AutoApprove: false, Description: "Critical"},
		},

		// Backend URLs: empty string means fall back to static/Vault
		HAURL:       getEnvOrEmpty("HA_URL", "https://homeassistant.lab.nkontur.com"),
		GrafanaURL:  getEnvOrEmpty("GRAFANA_URL", "https://grafana.lab.nkontur.com"),
		InfluxDBURL: getEnvOrEmpty("INFLUXDB_URL", "https://influxdb.lab.nkontur.com"),
		GitLabURL:   getEnvOrEmpty("GITLAB_URL", "https://gitlab.lab.nkontur.com"),

		GitLabAdminToken: os.Getenv("GITLAB_ADMIN_TOKEN"),
		GitLabProjectID:  getEnvOrEmpty("GITLAB_PROJECT_ID", "4"),

		TailscaleAPIURL: getEnvOrEmpty("TAILSCALE_API_URL", "https://api.tailscale.com"),
		PaperlessURL:    getEnvOrEmpty("PAPERLESS_URL", ""),
		SSHVaultPath:    getEnv("SSH_VAULT_PATH", "ssh-client-signer"),
	}

	if err := cfg.Validate(); err != nil {
		return nil, err
	}

	return cfg, nil
}

// Validate checks that required configuration is present.
func (c *Config) Validate() error {
	if c.VaultAddr == "" {
		return fmt.Errorf("VAULT_ADDR is required")
	}
	if c.VaultRoleID == "" {
		return fmt.Errorf("VAULT_ROLE_ID is required")
	}
	if c.VaultSecretID == "" {
		return fmt.Errorf("VAULT_SECRET_ID is required")
	}
	if c.TelegramBotToken == "" {
		return fmt.Errorf("TELEGRAM_BOT_TOKEN is required")
	}
	if c.TelegramWebhookSecret == "" {
		return fmt.Errorf("TELEGRAM_WEBHOOK_SECRET is required")
	}
	if c.JITAPIKey == "" {
		return fmt.Errorf("JIT_API_KEY is required")
	}
	return nil
}

// TierFor returns the tier configuration for a given tier level.
// Returns an error if the tier is unknown.
func (c *Config) TierFor(tier int) (TierConfig, error) {
	tc, ok := c.Tiers[tier]
	if !ok {
		return TierConfig{}, fmt.Errorf("unknown tier: %d", tier)
	}
	return tc, nil
}

// IsRequesterAllowed checks if the given requester ID is in the allowlist.
func (c *Config) IsRequesterAllowed(requester string) bool {
	for _, r := range c.AllowedRequesters {
		if r == requester {
			return true
		}
	}
	return false
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// getEnvOrEmpty returns the env var value, or the default if the env var
// is not set. If the env var is explicitly set to empty, returns empty
// (which disables the corresponding dynamic backend).
func getEnvOrEmpty(key, defaultVal string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return defaultVal
}
