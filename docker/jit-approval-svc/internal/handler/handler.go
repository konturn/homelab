package handler

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/actions"
	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/logger"
	"github.com/nkontur/jit-approval-svc/internal/ratelimit"
	"github.com/nkontur/jit-approval-svc/internal/store"
	"github.com/nkontur/jit-approval-svc/internal/telegram"
	"github.com/nkontur/jit-approval-svc/internal/vault"
)

// Handler holds all dependencies for HTTP request handling.
type Handler struct {
	cfg               *config.Config
	store             *store.Store
	vault             *vault.Client
	telegram          *telegram.Client
	backends          *backend.Registry
	limiter           *ratelimit.Limiter
	lastWebhookRefresh time.Time
	actions           *actions.Service
	// fyi sends an informational Telegram line (no buttons), for
	// auto-fyi grants. nil disables it.
	fyi func(text string) error
}

// SetActions enables routing of "act:" button callbacks to the action-prompt
// service (see internal/actions).
func (h *Handler) SetActions(a *actions.Service) {
	h.actions = a
}

// New creates a new Handler.
func New(cfg *config.Config, s *store.Store, v *vault.Client, tg *telegram.Client, backends *backend.Registry) *Handler {
	h := &Handler{
		cfg:      cfg,
		store:    s,
		vault:    v,
		telegram: tg,
		backends: backends,
		limiter:  ratelimit.NewFromEnv(),
	}
	if tg != nil {
		h.fyi = func(text string) error {
			_, err := tg.SendWithButtons(text, nil)
			return err
		}
	}
	return h
}

// authenticate identifies the caller from its X-JIT-API-Key. The requester
// is whoever owns that key (config.Authenticate), never a field of the
// request body.
func (h *Handler) authenticate(r *http.Request) (string, bool) {
	return h.cfg.Authenticate(r.Header.Get("X-JIT-API-Key"))
}

// maxBodyBytes caps request bodies on /request and /telegram/webhook (the
// actions socket API uses 16 KiB). Real requests are well under 4 KiB.
const maxBodyBytes = 64 << 10

// decodeBody decodes a size-limited JSON body, writing the error response
// (413 when too large, else 400 with badMsg) and returning false on failure.
func decodeBody(w http.ResponseWriter, r *http.Request, v interface{}, badMsg string) bool {
	r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		var tooBig *http.MaxBytesError
		if errors.As(err, &tooBig) {
			writeError(w, http.StatusRequestEntityTooLarge, "request body too large")
			return false
		}
		writeError(w, http.StatusBadRequest, badMsg)
		return false
	}
	return true
}

// --- Request types ---

// VaultPathRequest mirrors store.VaultPathRequest for JSON deserialization.
type VaultPathRequest struct {
	Path         string   `json:"path"`
	Capabilities []string `json:"capabilities"`
}

// CreateRequestBody is the JSON body for POST /request.
type CreateRequestBody struct {
	// Requester is optional and only checked: the caller is identified by
	// its key, and a request naming anyone else is refused.
	Requester  string             `json:"requester"`
	Resource   string             `json:"resource"`
	Tier       int                `json:"tier"`
	Reason     string             `json:"reason"`
	Scopes     []string           `json:"scopes,omitempty"`
	VaultPaths []VaultPathRequest `json:"vault_paths,omitempty"`
	SSHHost    string             `json:"ssh_host,omitempty"`
	ProjectID  string             `json:"project_id,omitempty"`
	TTL        string             `json:"ttl,omitempty"` // Optional: requested TTL (e.g. "1h", "30m"). Capped at resource/tier max.
}

// CreateRequestResponse is the JSON response for POST /request.
type CreateRequestResponse struct {
	RequestID  string              `json:"request_id"`
	Status     string              `json:"status"`
	Credential *CredentialResponse `json:"credential,omitempty"`
	Error      string              `json:"error,omitempty"`
	Message    string              `json:"message,omitempty"`
	Backend    string              `json:"backend,omitempty"`
}

// StatusResponse is the JSON response for GET /status/:id.
type StatusResponse struct {
	RequestID  string              `json:"request_id"`
	Status     string              `json:"status"`
	Credential *CredentialResponse `json:"credential,omitempty"`
}

// CredentialResponse is the credential data returned in status responses.
type CredentialResponse struct {
	Token    string            `json:"token,omitempty"`
	LeaseTTL string           `json:"lease_ttl,omitempty"`
	LeaseID  string            `json:"lease_id,omitempty"`
	Metadata map[string]string `json:"metadata,omitempty"`
}

// HealthResponse is the JSON response for GET /health.
type HealthResponse struct {
	Status   string `json:"status"`
	Vault    string `json:"vault"`
	Uptime   string `json:"uptime"`
	Requests int    `json:"requests_in_store"`
}

// --- Handlers ---

// HandleRequest handles POST /request.
func (h *Handler) HandleRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	requester, ok := h.authenticate(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	var body CreateRequestBody
	if !decodeBody(w, r, &body, "invalid request body") {
		return
	}

	// The body may name the requester, but only its own name.
	if body.Requester != "" && body.Requester != requester {
		logger.Warn("request_rejected_requester_mismatch", logger.Fields{
			"requester": requester,
			"claimed":   body.Requester,
			"resource":  body.Resource,
		})
		writeError(w, http.StatusForbidden, fmt.Sprintf("this key belongs to %s, not %q", requester, body.Requester))
		return
	}
	body.Requester = requester

	// Validate tier
	if _, err := h.cfg.TierFor(body.Tier); err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	// A request below the resource's tier is raised to it: the tier, not
	// the caller's guess, decides approval and TTL.
	if minTier := vault.MinTierForResource(body.Resource); minTier > 0 && body.Tier < minTier {
		logger.Info("request_tier_raised", logger.Fields{
			"requester":      requester,
			"resource":       body.Resource,
			"requested_tier": body.Tier,
			"tier":           minTier,
		})
		body.Tier = minTier
	}
	tierCfg, err := h.cfg.TierFor(body.Tier)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}

	decision := h.cfg.DecisionFor(requester, body.Tier)
	if decision == config.DecisionDeny {
		logger.Warn("request_denied_by_policy", logger.Fields{
			"requester": requester,
			"resource":  body.Resource,
			"tier":      body.Tier,
		})
		writeError(w, http.StatusForbidden, fmt.Sprintf(
			"%s may not request tier %d resources (%s); ask Noah to have his own agent do it",
			requester, body.Tier, body.Resource,
		))
		return
	}

	// Validate required fields
	if body.Resource == "" {
		writeError(w, http.StatusBadRequest, "resource is required")
		return
	}
	if body.Reason == "" {
		writeError(w, http.StatusBadRequest, "reason is required")
		return
	}

	// Parse the requested TTL before anything is recorded, so a rejected
	// request leaves no pending entry and uses no rate-limit slot.
	var requestedTTL time.Duration
	if body.TTL != "" {
		parsed, err := time.ParseDuration(body.TTL)
		if err != nil {
			writeError(w, http.StatusBadRequest, fmt.Sprintf("invalid ttl %q: %s", body.TTL, err.Error()))
			return
		}
		if parsed <= 0 {
			writeError(w, http.StatusBadRequest, "ttl must be positive")
			return
		}
		requestedTTL = parsed
	}

	// A project override becomes part of a GitLab API path: only a numeric
	// ID or namespace/project path, and it is shown to the approver.
	if body.ProjectID != "" {
		if err := backend.ValidateGitLabProjectID(body.ProjectID); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}
	if body.Resource == "gitlab" {
		if err := backend.ValidateGitLabProjectScopes(body.Scopes); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}

	// Rate limit per resource per requester (JIT_RATE_LIMIT_MAX per
	// JIT_RATE_LIMIT_WINDOW_MIN; see internal/ratelimit and docker-compose.yml)
	if ok, retryAfter := h.limiter.Allow(body.Resource, body.Requester); !ok {
		logger.Warn("request_rate_limited", logger.Fields{
			"requester":     body.Requester,
			"resource":      body.Resource,
			"retry_after_s": int(retryAfter.Seconds()) + 1,
		})
		w.Header().Set("Retry-After", fmt.Sprintf("%d", int(retryAfter.Seconds())+1))
		writeError(w, http.StatusTooManyRequests, ratelimit.Message(body.Resource, body.Requester, retryAfter))
		return
	}

	// Validate vault_paths for vault resource
	if body.Resource == "vault" {
		if len(body.VaultPaths) == 0 {
			writeError(w, http.StatusBadRequest, "vault_paths is required when resource is vault")
			return
		}
		// Convert to backend type for validation
		bPaths := make([]backend.VaultPathRequest, len(body.VaultPaths))
		for i, p := range body.VaultPaths {
			bPaths[i] = backend.VaultPathRequest{Path: p.Path, Capabilities: p.Capabilities}
		}
		if err := backend.ValidateVaultPaths(bPaths); err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
	}

	// Default scopes to ["api"] for GitLab if not specified
	scopes := body.Scopes
	if len(scopes) == 0 {
		scopes = []string{"api"}
	}

	// Create request in store
	req, err := h.store.Create(body.Requester, body.Resource, body.Tier, body.Reason, scopes)
	if err != nil {
		logger.Error("store_create_failed", logger.Fields{
			"error": err.Error(),
		})
		writeError(w, http.StatusServiceUnavailable, "service at capacity, try again later")
		return
	}

	// Attach SSH host if present
	if body.SSHHost != "" {
		req.SSHHost = body.SSHHost
	}

	// Attach project ID override if present
	if body.ProjectID != "" {
		req.ProjectID = body.ProjectID
	}

	req.RequestedTTL = requestedTTL

	// Attach vault paths if present
	if len(body.VaultPaths) > 0 {
		storePaths := make([]store.VaultPathRequest, len(body.VaultPaths))
		for i, p := range body.VaultPaths {
			storePaths[i] = store.VaultPathRequest{Path: p.Path, Capabilities: p.Capabilities}
		}
		req.VaultPaths = storePaths
	}

	logger.Info("request_received", logger.Fields{
		"request_id": req.ID,
		"requester":  body.Requester,
		"resource":   body.Resource,
		"tier":       body.Tier,
		"reason":     body.Reason,
		"scopes":     scopes,
		"ssh_host":   body.SSHHost,
		"decision":   string(decision),
	})

	// Mint now (auto, auto-fyi) or ask Noah (approve)
	var credResp *CredentialResponse
	var respErr, respMsg, respBackend string
	if decision.Mints() {
		cred, mintErr := h.autoApprove(req, tierCfg)
		if mintErr != nil {
			// T1 auto-approve failed due to upstream error — fail fast
			_ = h.store.SetError(req.ID)
			req.Status = store.StatusError
			respErr = "upstream_unreachable"
			respMsg = fmt.Sprintf("Failed to mint token: %s", mintErr.Error())
			respBackend = body.Resource
		} else if cred != nil {
			credResp = &CredentialResponse{
				Token:    cred.Token,
				LeaseTTL: cred.LeaseTTL.String(),
				LeaseID:  cred.LeaseID,
				Metadata: cred.Metadata,
			}
			if decision == config.DecisionAutoFYI {
				h.sendFYI(req, cred.LeaseTTL)
			}
		}
	} else {
		// Send Telegram approval message
		h.sendApprovalMessage(req, tierCfg)
	}

	writeJSON(w, http.StatusCreated, CreateRequestResponse{
		RequestID:  req.ID,
		Status:     string(req.Status),
		Credential: credResp,
		Error:      respErr,
		Message:    respMsg,
		Backend:    respBackend,
	})
}

// HandleStatus handles GET /status/:id.
func (h *Handler) HandleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	requester, ok := h.authenticate(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	// Extract request ID from path: /status/{id}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/status/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(w, http.StatusBadRequest, "request_id is required")
		return
	}
	requestID := parts[0]

	// A request is visible only to the requester that made it, so one
	// agent cannot claim a credential another agent was granted.
	req := h.store.Get(requestID)
	if req == nil || req.Requester != requester {
		if req != nil {
			logger.Warn("status_rejected_other_requester", logger.Fields{
				"request_id": requestID,
				"requester":  requester,
			})
		}
		writeError(w, http.StatusNotFound, "request not found")
		return
	}

	resp := StatusResponse{
		RequestID: req.ID,
		Status:    string(req.Status),
	}

	// If approved, try to claim the credential (one-time delivery)
	if req.Status == store.StatusApproved {
		cred, err := h.store.Claim(req.ID)
		if err != nil {
			logger.Error("claim_error", logger.Fields{
				"request_id": req.ID,
				"error":      err.Error(),
			})
		}
		if cred != nil {
			resp.Status = string(store.StatusApproved)
			resp.Credential = &CredentialResponse{
				Token:    cred.Token,
				LeaseTTL: cred.LeaseTTL.String(),
				LeaseID:  cred.LeaseID,
				Metadata: cred.Metadata,
			}

			logger.Info("credential_claimed", logger.Fields{
				"request_id": req.ID,
			})
		}
	}

	writeJSON(w, http.StatusOK, resp)
}

// HandleHealth handles GET /health.
// Unauthenticated callers get minimal info; full details require API key.
func (h *Handler) HandleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	// Check if caller provided a valid API key for full details
	_, authenticated := h.authenticate(r)

	if !authenticated {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
		return
	}

	vaultStatus := "ok"
	if err := h.vault.Health(); err != nil {
		vaultStatus = fmt.Sprintf("error: %s", err.Error())
	}

	status := "ok"
	if vaultStatus != "ok" {
		status = "degraded"
	}

	writeJSON(w, http.StatusOK, HealthResponse{
		Status:   status,
		Vault:    vaultStatus,
		Requests: h.store.Count(),
	})
}

// HandleWebhookRefresh handles POST /webhook/refresh.
// Called by DDNS or other scripts after a public IP change to re-register
// the Telegram webhook with the new IP address.
// Rate limited to once per 5 minutes. Requires API key auth.
func (h *Handler) HandleWebhookRefresh(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	// Require API key
	if _, ok := h.authenticate(r); !ok {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	// Rate limit: once per 5 minutes
	const cooldown = 5 * time.Minute
	now := time.Now()
	if !h.lastWebhookRefresh.IsZero() && now.Sub(h.lastWebhookRefresh) < cooldown {
		retryAfter := h.lastWebhookRefresh.Add(cooldown).Sub(now)
		logger.Warn("webhook_refresh_rate_limited", logger.Fields{
			"retry_after_s": int(retryAfter.Seconds()),
		})
		writeJSON(w, http.StatusTooManyRequests, map[string]interface{}{
			"error":        "rate limited",
			"retry_after_s": int(retryAfter.Seconds()) + 1,
		})
		return
	}

	if h.cfg.TelegramWebhookURL == "" {
		writeError(w, http.StatusBadRequest, "no webhook URL configured")
		return
	}

	if err := h.telegram.SetWebhook(h.cfg.TelegramWebhookURL, h.cfg.TelegramWebhookSecret); err != nil {
		logger.Error("webhook_refresh_failed", logger.Fields{
			"error": err.Error(),
		})
		writeError(w, http.StatusInternalServerError, "webhook refresh failed: "+err.Error())
		return
	}

	h.lastWebhookRefresh = now
	logger.Info("webhook_refreshed", logger.Fields{
		"url": h.cfg.TelegramWebhookURL,
	})

	writeJSON(w, http.StatusOK, map[string]string{
		"status": "ok",
		"message": "webhook re-registered with current IP",
	})
}

// HandleTelegramWebhook handles POST /telegram/webhook.
func (h *Handler) HandleTelegramWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	// Verify webhook secret (always required)
	secretHeader := r.Header.Get("X-Telegram-Bot-Api-Secret-Token")
	if subtle.ConstantTimeCompare([]byte(secretHeader), []byte(h.cfg.TelegramWebhookSecret)) != 1 {
		logger.Warn("webhook_unauthorized", logger.Fields{
			"remote_addr": r.RemoteAddr,
		})
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	var update TelegramUpdate
	if !decodeBody(w, r, &update, "invalid update payload") {
		return
	}

	// Only process callback queries
	if update.CallbackQuery == nil {
		w.WriteHeader(http.StatusOK)
		return
	}

	cb := update.CallbackQuery

	// Verify the callback is from the authorized chat
	if cb.From.ID != h.cfg.TelegramChatID {
		logger.Warn("webhook_callback_unauthorized_user", logger.Fields{
			"user_id": cb.From.ID,
		})
		w.WriteHeader(http.StatusOK)
		return
	}

	h.processCallback(cb)
	w.WriteHeader(http.StatusOK)
}

// --- Telegram callback types ---

// TelegramUpdate represents an incoming Telegram webhook update.
type TelegramUpdate struct {
	UpdateID      int            `json:"update_id"`
	CallbackQuery *CallbackQuery `json:"callback_query,omitempty"`
}

// CallbackQuery represents a Telegram callback query from inline buttons.
type CallbackQuery struct {
	ID      string       `json:"id"`
	From    TelegramUser `json:"from"`
	Message *struct {
		MessageID int `json:"message_id"`
	} `json:"message,omitempty"`
	Data string `json:"data"`
}

// TelegramUser represents a Telegram user.
type TelegramUser struct {
	ID int64 `json:"id"`
}

// --- Internal methods ---

// mintCredential mints a credential with the resource's backend: its dynamic
// backend when one is registered, otherwise the static backend (a Vault token
// limited to that resource's own secret). There is no fallback: if a dynamic
// backend fails, its error is logged and returned.
func (h *Handler) mintCredential(req *store.Request, ttl time.Duration) (*store.Credential, error) {
	b := h.backends.For(req.Resource)
	isDynamic := h.backends.IsDynamic(req.Resource)

	opts := backend.MintOptions{Scopes: req.Scopes, RequestID: req.ID, ProjectID: req.ProjectID}
	// Convert vault paths for dynamic vault backend
	if len(req.VaultPaths) > 0 {
		bPaths := make([]backend.VaultPathRequest, len(req.VaultPaths))
		for i, p := range req.VaultPaths {
			bPaths[i] = backend.VaultPathRequest{Path: p.Path, Capabilities: p.Capabilities}
		}
		opts.VaultPaths = bPaths
	}
	cred, err := b.MintCredential(req.Resource, req.Tier, ttl, opts)
	if err != nil {
		if isDynamic {
			logger.Error("dynamic_backend_failed", logger.Fields{
				"request_id": req.ID,
				"resource":   req.Resource,
				"error":      err.Error(),
			})
		}
		return nil, err
	}

	storeCred := &store.Credential{
		Token:    cred.Token,
		LeaseTTL: cred.LeaseTTL,
		LeaseID:  cred.Metadata["lease_id"],
		Metadata: cred.Metadata,
	}

	// Add SSH host to metadata if present on the request
	if req.SSHHost != "" && storeCred.Metadata != nil {
		storeCred.Metadata["host"] = req.SSHHost
	}

	return storeCred, nil
}

// autoApprove immediately approves a request the approval matrix lets
// through without a human (auto, auto-fyi) by minting a credential.
// Returns the minted credential on success, or an error if minting failed.
func (h *Handler) autoApprove(req *store.Request, tierCfg config.TierConfig) (*store.Credential, error) {
	ttl, maxTTL, err := h.effectiveTTL(req)
	if err != nil {
		return nil, err
	}

	logger.Info("auto_approve", logger.Fields{
		"request_id":    req.ID,
		"tier":          req.Tier,
		"resource":      req.Resource,
		"ttl":           ttl.String(),
		"max_ttl":       maxTTL.String(),
		"requested_ttl": req.RequestedTTL.String(),
	})

	cred, err := h.mintCredential(req, ttl)
	if err != nil {
		logger.Error("auto_approve_mint_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return nil, err
	}

	if err := h.store.Approve(req.ID, cred, ttl); err != nil {
		logger.Error("auto_approve_store_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return nil, err
	}

	logger.Info("approved", logger.Fields{
		"request_id":  req.ID,
		"approver":    "auto",
		"ttl_granted": ttl.String(),
		"backend":     cred.Metadata["backend"],
	})

	return cred, nil
}

// sendFYI tells Noah about an auto-fyi grant: one informational line, no
// buttons. A failed send is logged; the grant stands.
func (h *Handler) sendFYI(req *store.Request, ttl time.Duration) {
	if h.fyi == nil {
		return
	}
	text := fmt.Sprintf("ℹ️ JIT FYI: %s got <b>%s</b> (tier %d, %s): %s",
		html.EscapeString(req.Requester), html.EscapeString(req.Resource), req.Tier, ttl,
		html.EscapeString(truncate(displayReason(req), 300)))
	if err := h.fyi(text); err != nil {
		logger.Error("fyi_send_failed", logger.Fields{"request_id": req.ID, "error": err.Error()})
		return
	}
	logger.Info("fyi_sent", logger.Fields{"request_id": req.ID})
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// sendApprovalMessage sends a Telegram message with approve/deny buttons.
func (h *Handler) sendApprovalMessage(req *store.Request, tierCfg config.TierConfig) {
	if h.telegram == nil {
		logger.Error("telegram_client_nil", logger.Fields{
			"request_id": req.ID,
		})
		return
	}

	// Resolve the effective TTL (respects client-requested + per-resource overrides)
	ttl, maxTTL, err := h.effectiveTTL(req)
	if err != nil {
		ttl = tierCfg.TTL // fallback to tier default
		maxTTL = ttl
	}

	// Convert vault paths for Telegram display
	var tgVaultPaths []telegram.VaultPathInfo
	for _, vp := range req.VaultPaths {
		tgVaultPaths = append(tgVaultPaths, telegram.VaultPathInfo{
			Path:         vp.Path,
			Capabilities: vp.Capabilities,
		})
	}

	reason := displayReason(req)

	// Show requested TTL vs max if they differ
	ttlStr := ttl.String()
	if ttl != maxTTL {
		ttlStr = fmt.Sprintf("%s (requested, max: %s)", ttl.String(), maxTTL.String())
	}

	msgID, err := h.telegram.SendApprovalMessage(
		req.ID,
		req.Resource,
		req.Tier,
		reason,
		req.Requester,
		ttlStr,
		req.Scopes,
		tgVaultPaths,
	)
	if err != nil {
		logger.Error("telegram_send_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	h.store.SetTelegramMessageID(req.ID, msgID)

	logger.Info("approval_sent", logger.Fields{
		"request_id":          req.ID,
		"telegram_message_id": msgID,
	})

	// Start timeout goroutine
	go h.watchTimeout(req.ID)
}

// processCallback handles an approve or deny callback from Telegram.
func (h *Handler) processCallback(cb *CallbackQuery) {
	data := cb.Data

	// Action prompts (internal/actions): "act:<action>:<prompt-id>"
	if h.actions != nil && h.actions.HandleCallback(cb.ID, data) {
		return
	}

	// Parse callback data: "jit:approve:req-xxx" or "jit:deny:req-xxx"
	parts := strings.SplitN(data, ":", 3)
	if len(parts) != 3 || parts[0] != "jit" {
		logger.Warn("invalid_callback_data", logger.Fields{
			"data": data,
		})
		return
	}

	action := parts[1]
	requestID := parts[2]

	req := h.store.Get(requestID)
	if req == nil {
		logger.Warn("callback_request_not_found", logger.Fields{
			"request_id": requestID,
		})
		return
	}

	if req.Status != store.StatusPending {
		logger.Warn("callback_request_not_pending", logger.Fields{
			"request_id": requestID,
			"status":     string(req.Status),
		})
		return
	}

	switch action {
	case "approve":
		h.handleApprove(req)
	case "deny":
		h.handleDeny(req)
	default:
		logger.Warn("unknown_callback_action", logger.Fields{
			"action":     action,
			"request_id": requestID,
		})
	}
}

// handleApprove processes an approval callback.
func (h *Handler) handleApprove(req *store.Request) {
	tierCfg, err := h.cfg.TierFor(req.Tier)
	if err != nil {
		logger.Error("approve_tier_error", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	ttl, _, err := h.effectiveTTL(req)
	if err != nil {
		logger.Error("approve_ttl_error", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	cred, err := h.mintCredential(req, ttl)
	if err != nil {
		logger.Error("approve_mint_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		_ = h.store.SetError(req.ID)
		if req.TelegramMessageID != 0 {
			_ = h.telegram.EditMessageError(req.TelegramMessageID, req.Resource, err.Error())
		}
		return
	}

	if err := h.store.Approve(req.ID, cred, ttl); err != nil {
		logger.Error("approve_store_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		_ = h.store.SetError(req.ID)
		if req.TelegramMessageID != 0 {
			_ = h.telegram.EditMessageError(req.TelegramMessageID, req.Resource, err.Error())
		}
		return
	}

	logger.Info("approved", logger.Fields{
		"request_id":  req.ID,
		"approver":    fmt.Sprintf("telegram:%d", h.cfg.TelegramChatID),
		"ttl_granted": ttl.String(),
		"backend":     cred.Metadata["backend"],
	})

	// Edit Telegram message to reflect approval
	if req.TelegramMessageID != 0 {
		if err := h.telegram.EditMessageApproved(req.TelegramMessageID, h.buildDisplayInfo(req, tierCfg)); err != nil {
			logger.Error("telegram_edit_failed", logger.Fields{
				"request_id": req.ID,
				"error":      err.Error(),
			})
		}
	}
}

// handleDeny processes a deny callback.
func (h *Handler) handleDeny(req *store.Request) {
	if err := h.store.Deny(req.ID); err != nil {
		logger.Error("deny_store_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	logger.Info("denied", logger.Fields{
		"request_id": req.ID,
		"approver":   fmt.Sprintf("telegram:%d", h.cfg.TelegramChatID),
	})

	// Edit Telegram message to reflect denial
	if req.TelegramMessageID != 0 {
		tierCfg, _ := h.cfg.TierFor(req.Tier)
		if err := h.telegram.EditMessageDenied(req.TelegramMessageID, h.buildDisplayInfo(req, tierCfg)); err != nil {
			logger.Error("telegram_edit_failed", logger.Fields{
				"request_id": req.ID,
				"error":      err.Error(),
			})
		}
	}
}

// watchTimeout waits for the request timeout and marks the request as timed out.
func (h *Handler) watchTimeout(requestID string) {
	timer := time.NewTimer(h.cfg.RequestTimeout)
	defer timer.Stop()

	<-timer.C

	req := h.store.Get(requestID)
	if req == nil {
		return
	}

	if req.Status != store.StatusPending {
		return // Already resolved
	}

	if err := h.store.Timeout(requestID); err != nil {
		logger.Error("timeout_store_failed", logger.Fields{
			"request_id": requestID,
			"error":      err.Error(),
		})
		return
	}

	logger.Info("timeout", logger.Fields{
		"request_id":      requestID,
		"timeout_seconds": h.cfg.RequestTimeout.Seconds(),
	})

	// Edit Telegram message to show timeout
	if req.TelegramMessageID != 0 {
		tierCfg, _ := h.cfg.TierFor(req.Tier)
		if err := h.telegram.EditMessageTimeout(req.TelegramMessageID, h.buildDisplayInfo(req, tierCfg)); err != nil {
			logger.Error("telegram_edit_failed", logger.Fields{
				"request_id": requestID,
				"error":      err.Error(),
			})
		}
	}
}

// buildDisplayInfo creates a RequestDisplayInfo from a store request and tier config.
func (h *Handler) buildDisplayInfo(req *store.Request, tierCfg config.TierConfig) telegram.RequestDisplayInfo {
	var tgVaultPaths []telegram.VaultPathInfo
	for _, vp := range req.VaultPaths {
		tgVaultPaths = append(tgVaultPaths, telegram.VaultPathInfo{
			Path:         vp.Path,
			Capabilities: vp.Capabilities,
		})
	}

	reason := displayReason(req)

	// Use resolved TTL (respects client-requested + per-resource overrides)
	ttl, maxTTL, err := h.effectiveTTL(req)
	if err != nil {
		ttl = tierCfg.TTL
		maxTTL = ttl
	}

	ttlStr := ttl.String()
	if ttl != maxTTL {
		ttlStr = fmt.Sprintf("%s (requested, max: %s)", ttl.String(), maxTTL.String())
	}

	return telegram.RequestDisplayInfo{
		RequestID:  req.ID,
		Resource:   req.Resource,
		Tier:       req.Tier,
		Reason:     reason,
		Requester:  req.Requester,
		TTL:        ttlStr,
		Scopes:     req.Scopes,
		VaultPaths: tgVaultPaths,
	}
}

// displayReason is the reason shown in Telegram, with the SSH host and any
// GitLab project override appended so the approver sees what is granted.
func displayReason(req *store.Request) string {
	reason := req.Reason
	if req.SSHHost != "" {
		reason = fmt.Sprintf("%s (host: %s)", reason, req.SSHHost)
	}
	if req.ProjectID != "" {
		reason = fmt.Sprintf("%s (project: %s)", reason, req.ProjectID)
	}
	return reason
}

// effectiveTTL resolves the TTL for a request: starts with the resource/tier
// max, then caps to the client-requested TTL if it's smaller. Returns the
// effective TTL and the max TTL (for display purposes).
func (h *Handler) effectiveTTL(req *store.Request) (effective time.Duration, max time.Duration, err error) {
	max, err = h.cfg.TTLFor(req.Resource, req.Tier)
	if err != nil {
		return 0, 0, err
	}
	effective = max
	if req.RequestedTTL > 0 && req.RequestedTTL < max {
		effective = req.RequestedTTL
	}
	return effective, max, nil
}

// --- Utility ---

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(map[string]string{"error": message})
}
