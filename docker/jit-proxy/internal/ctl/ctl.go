// Package ctl is the control plane the broker uses to register and revoke
// grants. It listens only on the jit-proxy-ctl bridge (two members: the
// broker and the proxy) and additionally requires a bearer key.
package ctl

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/nkontur/jit-proxy/internal/grants"
	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/templates"
)

// Handler serves /internal/* and /health on the control listener.
type Handler struct {
	KeyHash [32]byte // SHA-256 of the control key
	Grants  *grants.Store
	Set     templates.Set
	BootID  string
	Health  func() (bool, map[string]interface{})
	OnEvent func(event string)
}

var grantPathRe = regexp.MustCompile(`^/internal/grants/(g-[0-9a-f]{12})$`)

func (h *Handler) authorized(r *http.Request) bool {
	const p = "Bearer "
	a := r.Header.Get("Authorization")
	if !strings.HasPrefix(a, p) {
		return false
	}
	sum := sha256.Sum256([]byte(a[len(p):]))
	return subtle.ConstantTimeCompare(sum[:], h.KeyHash[:]) == 1
}

func (h *Handler) event(e string) {
	if h.OnEvent != nil {
		h.OnEvent(e)
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/health" && r.Method == http.MethodGet {
		ok, info := h.Health()
		info["boot_id"] = h.BootID
		status := http.StatusOK
		if !ok {
			status = http.StatusServiceUnavailable
		}
		writeJSON(w, status, info)
		return
	}
	if !h.authorized(r) {
		logger.Warn("ctl_unauthorized", logger.Fields{"path": r.URL.Path, "remote": r.RemoteAddr})
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		return
	}
	switch {
	case r.URL.Path == "/internal/catalog" && r.Method == http.MethodGet:
		writeJSON(w, http.StatusOK, map[string]interface{}{"boot_id": h.BootID, "resources": h.Set.Catalog()})
	case r.URL.Path == "/internal/grants" && r.Method == http.MethodDelete:
		res := r.URL.Query().Get("resource")
		if res == "" && r.URL.Query().Get("all") != "1" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "resource or all=1 required"})
			return
		}
		n := h.Grants.RevokeResource(res)
		logger.Warn("grants_revoked_bulk", logger.Fields{"resource": res, "count": n})
		writeJSON(w, http.StatusOK, map[string]int{"revoked": n})
	default:
		m := grantPathRe.FindStringSubmatch(r.URL.Path)
		if m == nil {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		switch r.Method {
		case http.MethodPut:
			h.register(w, r, m[1])
		case http.MethodDelete:
			if h.Grants.Revoke(m[1]) {
				logger.Info("grant_revoked", logger.Fields{"grant_id": m[1]})
				h.event("grant_revoked")
				w.WriteHeader(http.StatusNoContent)
				return
			}
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "unknown grant"})
		default:
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		}
	}
}

func (h *Handler) register(w http.ResponseWriter, r *http.Request, id string) {
	data, err := io.ReadAll(io.LimitReader(r.Body, 16<<10+1))
	if err != nil || len(data) > 16<<10 {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "body too large"})
		return
	}
	var reg grants.Registration
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&reg); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid registration"})
		return
	}
	if reg.GrantID != id {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "grant_id does not match the path"})
		return
	}
	g, err := h.Grants.Register(reg)
	if err != nil {
		status := http.StatusUnprocessableEntity
		if errors.Is(err, grants.ErrConflict) {
			status = http.StatusConflict
		}
		logger.Warn("grant_rejected", logger.Fields{
			"grant_id": reg.GrantID, "request_id": reg.RequestID, "requester": reg.Requester,
			"resource": reg.Resource, "templates": reg.Templates, "tier": reg.Tier, "error": err.Error(),
		})
		writeJSON(w, status, map[string]string{"error": err.Error()})
		return
	}
	logger.Info("grant_registered", logger.Fields{
		"grant_id": g.ID, "request_id": g.RequestID, "requester": g.Requester, "resource": g.Resource,
		"templates": g.Templates, "tier": g.Tier, "approval": g.Approval,
		"expires_at": g.ExpiresAt.UTC().Format(time.RFC3339), "max_requests": g.MaxRequests, "single_use": g.SingleUse,
	})
	h.event("grant_registered")
	writeJSON(w, http.StatusCreated, map[string]interface{}{
		"grant_id": g.ID, "tier": g.Tier, "single_use": g.SingleUse, "max_requests": g.MaxRequests,
		"expires_at": g.ExpiresAt.UTC().Format(time.RFC3339),
	})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
