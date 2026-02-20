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
func NewRegistry(vaultMinter VaultTokenMinter, vaultReader VaultSecretReader, haURL, grafanaURL, influxdbURL, gitlabURL, gitlabAdminToken, gitlabProjectID, tailscaleAPIURL, paperlessURL string, vaultPolicyMgr VaultPolicyManager, sshSigner VaultSSHSigner, sshVaultPath, googleTokenURL string) *Registry {
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

	if gitlabURL != "" && gitlabAdminToken != "" {
		b := NewGitLabBackend(gitlabURL, gitlabAdminToken, gitlabProjectID)
		r.backends["gitlab"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "gitlab",
			"backend":  "dynamic/gitlab",
		})
	}

	if paperlessURL != "" {
		b := NewPaperlessBackend(paperlessURL, vaultReader)
		r.backends["paperless"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "paperless",
			"backend":  "dynamic/paperless",
		})
	}

	if tailscaleAPIURL != "" {
		b := NewTailscaleBackend(tailscaleAPIURL, vaultReader)
		r.backends["tailscale"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "tailscale",
			"backend":  "dynamic/tailscale",
		})
	}

	if sshSigner != nil && sshVaultPath != "" {
		sshBackend := NewSSHBackend(sshSigner, sshVaultPath)
		for _, res := range []string{"ssh", "ssh-satellite", "ssh-zwave", "ssh-nkontur", "ssh-konoahko", "ssh-konturn", "ssh-macmini"} {
			r.backends[res] = sshBackend
			logger.Info("backend_registered", logger.Fields{
				"resource": res,
				"backend":  "dynamic/ssh",
			})
		}
	}

	if googleTokenURL != "" {
		readBackend := NewGmailBackend(googleTokenURL, vaultReader, GmailScopeRead)
		r.backends["gmail-read"] = readBackend
		logger.Info("backend_registered", logger.Fields{
			"resource": "gmail-read",
			"backend":  "dynamic/gmail",
		})

		sendBackend := NewGmailBackend(googleTokenURL, vaultReader, GmailScopeSend)
		r.backends["gmail-send"] = sendBackend
		logger.Info("backend_registered", logger.Fields{
			"resource": "gmail-send",
			"backend":  "dynamic/gmail",
		})
	}

	if vaultPolicyMgr != nil {
		b := NewVaultDynamicBackend(vaultMinter, vaultPolicyMgr)
		r.backends["vault"] = b
		logger.Info("backend_registered", logger.Fields{
			"resource": "vault",
			"backend":  "dynamic/vault",
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
