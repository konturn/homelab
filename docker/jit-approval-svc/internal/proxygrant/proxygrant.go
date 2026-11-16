// Package proxygrant issues credential-proxy grants (docker/jit-proxy,
// docs/design/jit-credential-proxy.md) for resources whose only credential
// is a long-lived static secret.
//
// The broker stays the only place that authenticates requesters and applies
// tiers, the approval matrix and rate limits; this package turns an approved
// request into a grant: a random jitp_ secret returned to the requester once,
// registered with the proxy by its SHA-256 together with the SHA-256 of the
// requester's own broker key, the templates, the tier, how it was approved
// and its expiry. Registration happens before the requester sees anything;
// if it fails, nothing is issued.
package proxygrant

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/backend"
	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// CredentialType is the credential_type a request names to get a grant.
const CredentialType = "proxy_grant"

// Approval values (the proxy checks them against the approval matrix).
const (
	ApprovalAuto     = "auto"
	ApprovalApproved = "approved"
)

// TemplateInfo and ResourceInfo mirror the proxy's /internal/catalog.
type TemplateInfo struct {
	Description string `json:"description"`
	MinTier     int    `json:"min_tier"`
	SingleUse   bool   `json:"single_use"`
}

// ResourceInfo describes one proxied resource.
type ResourceInfo struct {
	MinTier   int                     `json:"min_tier"`
	Templates map[string]TemplateInfo `json:"templates"`
}

// MintRequest is an approved request to turn into a grant.
type MintRequest struct {
	RequestID          string
	Requester          string
	RequesterKeySHA256 string
	Resource           string
	Templates          []string
	Tier               int
	Approval           string
	TTL                time.Duration
}

type registration struct {
	GrantID            string    `json:"grant_id"`
	SecretSHA256       string    `json:"secret_sha256"`
	RequestID          string    `json:"request_id"`
	Requester          string    `json:"requester"`
	RequesterKeySHA256 string    `json:"requester_key_sha256"`
	Resource           string    `json:"resource"`
	Templates          []string  `json:"templates"`
	Tier               int       `json:"tier"`
	Approval           string    `json:"approval"`
	ExpiresAt          time.Time `json:"expires_at"`
}

// Client talks to the proxy's control plane and remembers live grants.
type Client struct {
	CtlURL    string // http://<jit-proxy-ctl address>:8091
	CtlKey    string
	PublicURL string // the data-plane URL agents use
	HTTP      *http.Client
	Now       func() time.Time

	mu      sync.Mutex
	catalog map[string]ResourceInfo
	bootID  string
	grants  map[string]record // by grant ID; hashes only, never secrets
}

type record struct {
	reg       registration
	singleUse bool
}

// New returns a client.
func New(ctlURL, ctlKey, publicURL string) *Client {
	return &Client{
		CtlURL: strings.TrimRight(ctlURL, "/"), CtlKey: ctlKey, PublicURL: strings.TrimRight(publicURL, "/"),
		HTTP: &http.Client{Timeout: 5 * time.Second}, Now: time.Now,
		grants: map[string]record{},
	}
}

// Errors.
var (
	ErrUnavailable = errors.New("credential proxy unavailable")
	ErrNotFound    = errors.New("grant not found")
	ErrNotOwner    = errors.New("grant belongs to another requester")
)

func (c *Client) do(ctx context.Context, method, path string, body interface{}) (*http.Response, []byte, error) {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, nil, err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.CtlURL+path, rd)
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.CtlKey)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, nil, fmt.Errorf("%w: %s %s", ErrUnavailable, method, path)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp, data, nil
}

// Refresh loads the template catalog and the proxy's boot ID.
func (c *Client) Refresh(ctx context.Context) error {
	resp, data, err := c.do(ctx, http.MethodGet, "/internal/catalog", nil)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: catalog HTTP %d", ErrUnavailable, resp.StatusCode)
	}
	var out struct {
		BootID    string                  `json:"boot_id"`
		Resources map[string]ResourceInfo `json:"resources"`
	}
	if err := json.Unmarshal(data, &out); err != nil || len(out.Resources) == 0 {
		return fmt.Errorf("%w: bad catalog", ErrUnavailable)
	}
	c.mu.Lock()
	c.catalog = out.Resources
	c.mu.Unlock()
	return nil
}

func (c *Client) resource(ctx context.Context, name string) (ResourceInfo, bool, error) {
	c.mu.Lock()
	cat := c.catalog
	c.mu.Unlock()
	if cat == nil {
		if err := c.Refresh(ctx); err != nil {
			return ResourceInfo{}, false, err
		}
		c.mu.Lock()
		cat = c.catalog
		c.mu.Unlock()
	}
	ri, ok := cat[name]
	return ri, ok, nil
}

// IsProxied reports whether the proxy serves a resource. Before the catalog
// is known it answers from a short fetch; on failure, false.
func (c *Client) IsProxied(name string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_, ok, err := c.resource(ctx, name)
	return err == nil && ok
}

// Tier validates the requested templates and returns the tier the request
// must be decided at: the highest of the resource's and each template's.
func (c *Client) Tier(name string, templates []string) (int, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	ri, ok, err := c.resource(ctx, name)
	if err != nil {
		return 0, err
	}
	if !ok {
		return 0, fmt.Errorf("%s is not served by the credential proxy", name)
	}
	if len(templates) == 0 {
		return 0, fmt.Errorf("name at least one template for %s: %s", name, strings.Join(names(ri), ", "))
	}
	tier := ri.MinTier
	seen := map[string]bool{}
	for _, t := range templates {
		ti, ok := ri.Templates[t]
		if !ok {
			return 0, fmt.Errorf("unknown template %q for %s (have: %s)", t, name, strings.Join(names(ri), ", "))
		}
		if seen[t] {
			return 0, fmt.Errorf("template %q listed twice", t)
		}
		seen[t] = true
		if ti.MinTier > tier {
			tier = ti.MinTier
		}
	}
	return tier, nil
}

// Describe is a one-line summary of templates for the Telegram message.
func (c *Client) Describe(name string, templates []string) string {
	c.mu.Lock()
	defer c.mu.Unlock()
	ri := c.catalog[name]
	var parts []string
	for _, t := range templates {
		d := ri.Templates[t].Description
		if ri.Templates[t].SingleUse {
			d += ", single use"
		}
		parts = append(parts, fmt.Sprintf("%s (%s)", t, d))
	}
	return strings.Join(parts, "; ")
}

func names(ri ResourceInfo) []string {
	var out []string
	for n := range ri.Templates {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}

var keyHashRe = regexp.MustCompile(`^[0-9a-f]{64}$`)

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// Mint creates and registers a grant. The secret is returned in the
// credential and is not kept.
func (c *Client) Mint(m MintRequest) (*backend.Credential, error) {
	if m.Approval != ApprovalAuto && m.Approval != ApprovalApproved {
		return nil, fmt.Errorf("invalid approval %q", m.Approval)
	}
	if !keyHashRe.MatchString(m.RequesterKeySHA256) || m.Requester == "" {
		return nil, fmt.Errorf("grant needs the authenticated requester")
	}
	if m.TTL <= 0 {
		return nil, fmt.Errorf("invalid ttl")
	}
	tier, err := c.Tier(m.Resource, m.Templates)
	if err != nil {
		return nil, err
	}
	if m.Tier < tier {
		return nil, fmt.Errorf("tier %d is below the templates' tier %d", m.Tier, tier)
	}

	secretBytes := make([]byte, 32)
	if _, err := rand.Read(secretBytes); err != nil {
		return nil, err
	}
	secret := "jitp_" + base64.RawURLEncoding.EncodeToString(secretBytes)
	sum := sha256.Sum256([]byte(secret))
	tmpl := append([]string(nil), m.Templates...)
	sort.Strings(tmpl)
	reg := registration{
		GrantID: "g-" + randomHex(6), SecretSHA256: hex.EncodeToString(sum[:]),
		RequestID: m.RequestID, Requester: m.Requester, RequesterKeySHA256: m.RequesterKeySHA256,
		Resource: m.Resource, Templates: tmpl, Tier: m.Tier, Approval: m.Approval,
		ExpiresAt: c.Now().Add(m.TTL).UTC().Truncate(time.Second),
	}
	out, err := c.register(context.Background(), reg)
	if err != nil {
		logger.Error("grant_registration_failed", logger.Fields{
			"request_id": m.RequestID, "grant_id": reg.GrantID, "resource": m.Resource, "error": err.Error(),
		})
		return nil, err
	}
	c.mu.Lock()
	c.grants[reg.GrantID] = record{reg: reg, singleUse: out.SingleUse}
	c.mu.Unlock()
	logger.Info("grant_issued", logger.Fields{
		"request_id": m.RequestID, "grant_id": reg.GrantID, "requester": m.Requester, "resource": m.Resource,
		"templates": tmpl, "tier": m.Tier, "approval": m.Approval, "expires_at": reg.ExpiresAt.Format(time.RFC3339),
		"single_use": out.SingleUse, "max_requests": out.MaxRequests,
	})
	return &backend.Credential{
		Token:    secret,
		LeaseTTL: reg.ExpiresAt.Sub(c.Now()).Round(time.Second),
		Metadata: map[string]string{
			"backend":      "proxy",
			"type":         CredentialType,
			"grant_id":     reg.GrantID,
			"base_url":     c.PublicURL + "/r/" + m.Resource,
			"templates":    strings.Join(tmpl, ","),
			"single_use":   strconv.FormatBool(out.SingleUse),
			"max_requests": strconv.Itoa(out.MaxRequests),
			"expires_at":   reg.ExpiresAt.Format(time.RFC3339),
		},
	}, nil
}

type regResult struct {
	SingleUse   bool `json:"single_use"`
	MaxRequests int  `json:"max_requests"`
}

func (c *Client) register(ctx context.Context, reg registration) (*regResult, error) {
	resp, data, err := c.do(ctx, http.MethodPut, "/internal/grants/"+reg.GrantID, reg)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusCreated {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(data, &e)
		return nil, fmt.Errorf("proxy refused the grant (HTTP %d): %s", resp.StatusCode, e.Error)
	}
	var out regResult
	_ = json.Unmarshal(data, &out)
	return &out, nil
}

// Revoke revokes a grant on behalf of its requester.
func (c *Client) Revoke(ctx context.Context, id, requester string) error {
	c.mu.Lock()
	rec, ok := c.grants[id]
	c.mu.Unlock()
	if !ok {
		return ErrNotFound
	}
	if rec.reg.Requester != requester {
		return ErrNotOwner
	}
	return c.revoke(ctx, id, "requester")
}

func (c *Client) revoke(ctx context.Context, id, by string) error {
	resp, _, err := c.do(ctx, http.MethodDelete, "/internal/grants/"+id, nil)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusNoContent && resp.StatusCode != http.StatusNotFound {
		return fmt.Errorf("revoke: HTTP %d", resp.StatusCode)
	}
	c.mu.Lock()
	delete(c.grants, id)
	c.mu.Unlock()
	logger.Info("grant_revoked", logger.Fields{"grant_id": id, "by": by})
	return nil
}

var revokePathRe = regexp.MustCompile(`^/grants/(g-[0-9a-f]{12})/revoke$`)

// HandleRevoke serves POST /grants/<id>/revoke for the grant's requester.
func (c *Client) HandleRevoke(auth func(*http.Request) (string, bool)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		write := func(status int, msg string) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(map[string]string{"status": msg})
		}
		if r.Method != http.MethodPost {
			write(http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		requester, ok := auth(r)
		if !ok {
			write(http.StatusUnauthorized, "unauthorized")
			return
		}
		m := revokePathRe.FindStringSubmatch(r.URL.Path)
		if m == nil {
			write(http.StatusNotFound, "not found")
			return
		}
		switch err := c.Revoke(r.Context(), m[1], requester); {
		case err == nil:
			write(http.StatusOK, "revoked")
		case errors.Is(err, ErrNotFound), errors.Is(err, ErrNotOwner):
			// The same answer for someone else's grant as for none.
			if errors.Is(err, ErrNotOwner) {
				logger.Warn("grant_revoke_other_requester", logger.Fields{"grant_id": m[1], "requester": requester})
			}
			write(http.StatusNotFound, "grant not found")
		default:
			write(http.StatusBadGateway, "credential proxy unavailable")
		}
	}
}

// Run keeps the catalog fresh, re-registers live grants when the proxy
// restarts (new boot ID; the proxy keeps grants in memory only) and revokes
// grants once they expire.
func (c *Client) Run(ctx context.Context, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		c.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Tick runs one round of Run.
func (c *Client) Tick(ctx context.Context) {
	resp, data, err := c.do(ctx, http.MethodGet, "/health", nil)
	if err != nil || resp == nil {
		return
	}
	var h struct {
		BootID string `json:"boot_id"`
	}
	if json.Unmarshal(data, &h) != nil || h.BootID == "" {
		return
	}
	c.mu.Lock()
	changed := h.BootID != c.bootID
	prev := c.bootID
	c.bootID = h.BootID
	var live []registration
	var expired []string
	now := c.Now()
	for id, rec := range c.grants {
		switch {
		case !now.Before(rec.reg.ExpiresAt):
			expired = append(expired, id)
		case rec.singleUse:
			// Never re-pushed: the restarted proxy would not know it
			// was spent. The requester asks for a new one.
		default:
			live = append(live, rec.reg)
		}
	}
	c.mu.Unlock()

	for _, id := range expired {
		if err := c.revoke(ctx, id, "expiry"); err == nil {
			logger.Info("grant_expired", logger.Fields{"grant_id": id})
		}
	}
	if !changed {
		return
	}
	_ = c.Refresh(ctx)
	if prev == "" {
		return // first contact since the broker started: nothing to re-push
	}
	n := 0
	for _, reg := range live {
		if _, err := c.register(ctx, reg); err != nil {
			logger.Error("grant_reregister_failed", logger.Fields{"grant_id": reg.GrantID, "error": err.Error()})
			continue
		}
		n++
	}
	logger.Info("grants_reregistered", logger.Fields{"boot_id": h.BootID, "count": n})
}
