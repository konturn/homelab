package backend

import (
	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// Registry maps resources to their credential backends.
type Registry struct {
	backends map[string]Backend
	fallback Backend
}

// NewRegistry creates a backend registry with the static fallback.
// Dynamic backends are added for resources where the service URL is configured.
func NewRegistry(vaultMinter VaultTokenMinter, vaultReader VaultSecretReader, haURL, grafanaURL, influxdbURL string) *Registry {
	static := NewStaticBackend(vaultMinter)

	r := &Registry{
		backends: make(map[string]Backend),
		fallback: static,
	}

	// Register dynamic backends when URLs are configured
	if haURL != "" {
		b := NewHomeAssistantBackend(haURL, vaultReader)
		r.backends["homeassistant"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "homeassistant",
			"backend":  "dynamic/homeassistant",
		})
	}

	if grafanaURL != "" {
		b := NewGrafanaBackend(grafanaURL, vaultReader)
		r.backends["grafana"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "grafana",
			"backend":  "dynamic/grafana",
		})
	}

	if influxdbURL != "" {
		b := NewInfluxDBBackend(influxdbURL, vaultReader)
		r.backends["influxdb"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "influxdb",
			"backend":  "dynamic/influxdb",
		})
	}

	return r
}

// For returns the backend for a given resource.
// If no dynamic backend is registered, returns the static fallback.
func (r *Registry) For(resource string) Backend {
	if b, ok := r.backends[resource]; ok {
		return b
	}
	return r.fallback
}

// IsDynamic returns true if the resource has a dynamic backend registered.
func (r *Registry) IsDynamic(resource string) bool {
	_, ok := r.backends[resource]
	return ok
}
