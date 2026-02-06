package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/config"
	"github.com/nkontur/jit-approval-svc/internal/logger"
	"github.com/nkontur/jit-approval-svc/internal/store"
	"github.com/nkontur/jit-approval-svc/internal/telegram"
	"github.com/nkontur/jit-approval-svc/internal/vault"
)

// Handler holds all dependencies for HTTP request handling.
type Handler struct {
	cfg      *config.Config
	store    *store.Store
	vault    *vault.Client
	telegram *telegram.Client
}

// New creates a new Handler.
func New(cfg *config.Config, s *store.Store, v *vault.Client, tg *telegram.Client) *Handler {
	return &Handler{
		cfg:      cfg,
		store:    s,
		vault:    v,
		telegram: tg,
	}
}

// --- Request types ---

// CreateRequestBody is the JSON body for POST /request.
type CreateRequestBody struct {
	Requester string `json:"requester"`
	Resource  string `json:"resource"`
	Tier      int    `json:"tier"`
	Reason    string `json:"reason"`
}

// CreateRequestResponse is the JSON response for POST /request.
type CreateRequestResponse struct {
	RequestID string `json:"request_id"`
	Status    string `json:"status"`
}

// StatusResponse is the JSON response for GET /status/:id.
type StatusResponse struct {
	RequestID  string               `json:"request_id"`
	Status     string               `json:"status"`
	Credential *CredentialResponse  `json:"credential,omitempty"`
}

// CredentialResponse is the credential data returned in status responses.
type CredentialResponse struct {
	Token    string `json:"token,omitempty"`
	LeaseTTL string `json:"lease_ttl,omitempty"`
	LeaseID  string `json:"lease_id,omitempty"`
}

// HealthResponse is the JSON response for GET /health.
type HealthResponse struct {
	Status    string `json:"status"`
	Vault     string `json:"vault"`
	Uptime    string `json:"uptime"`
	Requests  int    `json:"requests_in_store"`
}

// --- Handlers ---

// HandleRequest handles POST /request.
func (h *Handler) HandleRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	var body CreateRequestBody
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	// Validate requester
	if !h.cfg.IsRequesterAllowed(body.Requester) {
		logger.Warn("request_rejected_unauthorized", logger.Fields{
			"requester": body.Requester,
			"resource":  body.Resource,
		})
		writeError(w, http.StatusForbidden, "requester not allowed")
		return
	}

	// Validate tier
	tierCfg, err := h.cfg.TierFor(body.Tier)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
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

	// Create request in store
	req := h.store.Create(body.Requester, body.Resource, body.Tier, body.Reason)

	logger.Info("request_received", logger.Fields{
		"request_id": req.ID,
		"requester":  body.Requester,
		"resource":   body.Resource,
		"tier":       body.Tier,
		"reason":     body.Reason,
	})

	// Auto-approve for tier 0-1
	if tierCfg.AutoApprove {
		h.autoApprove(req, tierCfg)
	} else {
		// Send Telegram approval message
		h.sendApprovalMessage(req, tierCfg)
	}

	writeJSON(w, http.StatusCreated, CreateRequestResponse{
		RequestID: req.ID,
		Status:    string(req.Status),
	})
}

// HandleStatus handles GET /status/:id.
func (h *Handler) HandleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	// Extract request ID from path: /status/{id}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/status/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		writeError(w, http.StatusBadRequest, "request_id is required")
		return
	}
	requestID := parts[0]

	req := h.store.Get(requestID)
	if req == nil {
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
			}

			logger.Info("credential_claimed", logger.Fields{
				"request_id": req.ID,
			})
		}
	}

	writeJSON(w, http.StatusOK, resp)
}

// HandleHealth handles GET /health.
func (h *Handler) HandleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
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

// HandleTelegramWebhook handles POST /telegram/webhook.
func (h *Handler) HandleTelegramWebhook(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}

	// Verify webhook secret
	if h.cfg.TelegramWebhookSecret != "" {
		secretHeader := r.Header.Get("X-Telegram-Bot-Api-Secret-Token")
		if secretHeader != h.cfg.TelegramWebhookSecret {
			logger.Warn("webhook_unauthorized", logger.Fields{
				"remote_addr": r.RemoteAddr,
			})
			writeError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
	}

	var update TelegramUpdate
	if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
		writeError(w, http.StatusBadRequest, "invalid update payload")
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

// autoApprove immediately approves a tier 0-1 request by minting a credential.
func (h *Handler) autoApprove(req *store.Request, tierCfg config.TierConfig) {
	logger.Info("auto_approve", logger.Fields{
		"request_id": req.ID,
		"tier":       req.Tier,
		"resource":   req.Resource,
	})

	token, leaseID, err := h.vault.MintToken(req.Resource, req.Tier, tierCfg.TTL)
	if err != nil {
		logger.Error("auto_approve_mint_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	cred := &store.Credential{
		Token:    token,
		LeaseTTL: tierCfg.TTL,
		LeaseID:  leaseID,
	}

	if err := h.store.Approve(req.ID, cred, tierCfg.TTL); err != nil {
		logger.Error("auto_approve_store_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
	}

	logger.Info("approved", logger.Fields{
		"request_id":  req.ID,
		"approver":    "auto",
		"ttl_granted": tierCfg.TTL.String(),
	})
}

// sendApprovalMessage sends a Telegram message with approve/deny buttons.
func (h *Handler) sendApprovalMessage(req *store.Request, tierCfg config.TierConfig) {
	if h.telegram == nil {
		logger.Error("telegram_client_nil", logger.Fields{
			"request_id": req.ID,
		})
		return
	}

	msgID, err := h.telegram.SendApprovalMessage(
		req.ID,
		req.Resource,
		req.Tier,
		req.Reason,
		req.Requester,
		tierCfg.TTL.String(),
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
		"request_id":         req.ID,
		"telegram_message_id": msgID,
	})

	// Start timeout goroutine
	go h.watchTimeout(req.ID)
}

// processCallback handles an approve or deny callback from Telegram.
func (h *Handler) processCallback(cb *CallbackQuery) {
	data := cb.Data

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

	token, leaseID, err := h.vault.MintToken(req.Resource, req.Tier, tierCfg.TTL)
	if err != nil {
		logger.Error("approve_mint_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	cred := &store.Credential{
		Token:    token,
		LeaseTTL: tierCfg.TTL,
		LeaseID:  leaseID,
	}

	if err := h.store.Approve(req.ID, cred, tierCfg.TTL); err != nil {
		logger.Error("approve_store_failed", logger.Fields{
			"request_id": req.ID,
			"error":      err.Error(),
		})
		return
	}

	logger.Info("approved", logger.Fields{
		"request_id":  req.ID,
		"approver":    fmt.Sprintf("telegram:%d", h.cfg.TelegramChatID),
		"ttl_granted": tierCfg.TTL.String(),
	})

	// Edit Telegram message to reflect approval
	if req.TelegramMessageID != 0 {
		if err := h.telegram.EditMessageApproved(req.TelegramMessageID, req.ID, req.Resource); err != nil {
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
		if err := h.telegram.EditMessageDenied(req.TelegramMessageID, req.ID, req.Resource); err != nil {
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
		if err := h.telegram.EditMessageTimeout(req.TelegramMessageID, requestID, req.Resource); err != nil {
			logger.Error("telegram_edit_failed", logger.Fields{
				"request_id": requestID,
				"error":      err.Error(),
			})
		}
	}
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
