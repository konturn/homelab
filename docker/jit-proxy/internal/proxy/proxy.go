// Package proxy is the agent-facing data plane: /r/<resource>/<path>.
//
// For every request it: normalises the target (rules.ParseTarget), finds the
// grant by its secret and checks the caller's requester key, applies the
// resource's always-deny list and then the grant's allow rules, charges the
// grant (grants.Reserve), builds a fresh upstream request with an allowlist
// of headers and the injected credential, and scrubs the response. Nothing
// the agent sends is forwarded except the method, the canonical path, the
// validated query, the body, Accept and Content-Type.
package proxy

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/nkontur/jit-proxy/internal/grants"
	"github.com/nkontur/jit-proxy/internal/logger"
	"github.com/nkontur/jit-proxy/internal/redact"
	"github.com/nkontur/jit-proxy/internal/rules"
	"github.com/nkontur/jit-proxy/internal/templates"
	"github.com/nkontur/jit-proxy/internal/upstream"
)

// RequesterKeyHeader carries the caller's broker key; the grant is only
// usable together with the key of the requester it was issued to.
const RequesterKeyHeader = "X-JIT-API-Key"

// Refresher re-reads a resource's credential (rate limited by the cache).
type Refresher interface {
	Refresh(ctx context.Context, resource string) bool
}

// Proxy is the data-plane handler.
type Proxy struct {
	Set       templates.Set
	Grants    *grants.Store
	Injectors map[string]upstream.Injector
	Creds     Refresher
	Scrub     *redact.Scrubber
	Client    *http.Client
	ClientNet *net.IPNet // if set, only callers from this network
	Health    func() (bool, map[string]interface{})
	Stats     *Stats
	Timeout   time.Duration // whole upstream exchange
	// Inflight bounds concurrent upstream exchanges (responses are buffered
	// for scrubbing, so this bounds memory). nil = unbounded (tests).
	Inflight chan struct{}
}

// NewClient returns the upstream HTTP client: verified TLS, no environment
// proxies, no redirects followed, bounded timeouts.
func NewClient() *http.Client {
	tr := &http.Transport{
		Proxy:                 nil,
		DialContext:           (&net.Dialer{Timeout: 5 * time.Second}).DialContext,
		TLSHandshakeTimeout:   5 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
		MaxIdleConnsPerHost:   4,
		IdleConnTimeout:       60 * time.Second,
	}
	return &http.Client{
		Transport: tr,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

type audit struct {
	start    time.Time
	fields   logger.Fields
	decision string
}

func (p *Proxy) deny(w http.ResponseWriter, a *audit, status int, reason, detail string) {
	a.decision = "deny"
	a.fields["reason"] = reason
	a.fields["status"] = status
	writeJSON(w, status, map[string]string{"error": reason, "detail": p.Scrub.KnownOnly(detail)})
	p.finish(a)
}

func (p *Proxy) finish(a *audit) {
	a.fields["decision"] = a.decision
	a.fields["duration_ms"] = time.Since(a.start).Milliseconds()
	if p.Stats != nil {
		p.Stats.Record(a.fields)
	}
	if a.decision == "allow" {
		logger.Info("proxy_request", a.fields)
	} else {
		logger.Warn("proxy_request", a.fields)
	}
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	a := &audit{start: time.Now(), fields: logger.Fields{"method": r.Method}}

	if p.ClientNet != nil {
		host, _, _ := net.SplitHostPort(r.RemoteAddr)
		if ip := net.ParseIP(host); ip == nil || !p.ClientNet.Contains(ip) {
			p.deny(w, a, http.StatusForbidden, "client_network", "")
			return
		}
	}
	if r.ProtoMajor != 1 {
		p.deny(w, a, http.StatusHTTPVersionNotSupported, "protocol", "")
		return
	}
	if r.Header.Get("Upgrade") != "" || headerHasToken(r.Header, "Connection", "upgrade") {
		p.deny(w, a, http.StatusBadRequest, "upgrade", "protocol upgrades are not proxied")
		return
	}
	switch r.RequestURI {
	case "/health":
		ok, info := p.Health()
		status := http.StatusOK
		if !ok {
			status = http.StatusServiceUnavailable
		}
		writeJSON(w, status, info)
		return
	case "/summary":
		writeJSON(w, http.StatusOK, p.Stats.Summary(p.Grants.Active()))
		return
	}
	if !rules.Methods[r.Method] {
		p.deny(w, a, http.StatusMethodNotAllowed, "method", "")
		return
	}

	t, err := rules.ParseTarget(r.RequestURI)
	if err != nil {
		p.deny(w, a, http.StatusBadRequest, "normalisation", err.Error())
		return
	}
	a.fields["resource"] = t.Resource
	a.fields["path"] = t.Path
	a.fields["query_keys"] = t.QueryKeys()
	res, ok := p.Set[t.Resource]
	if !ok {
		p.deny(w, a, http.StatusNotFound, "unknown_resource", "")
		return
	}
	for k := range t.Query {
		if rules.IsCredentialParam(k) {
			p.deny(w, a, http.StatusBadRequest, "credential_param", "credentials are injected by the proxy; do not send "+k)
			return
		}
	}

	secret := bearer(r.Header.Get("Authorization"))
	g, reason := p.Grants.Lookup(secret, r.Header.Get(RequesterKeyHeader), t.Resource)
	if g != nil {
		a.fields["grant_id"] = g.ID
		a.fields["request_id"] = g.RequestID
		a.fields["requester"] = g.Requester
		a.fields["templates"] = g.Templates
	}
	switch reason {
	case "":
	case grants.ReasonNoGrant, grants.ReasonUnknown:
		p.deny(w, a, http.StatusUnauthorized, reason, "send Authorization: Bearer <grant> from jitcurl / the broker")
		return
	default:
		p.deny(w, a, http.StatusForbidden, reason, "")
		return
	}

	maxBody := res.BodyMax
	for _, rule := range g.Rules {
		if rule.BodyMax > maxBody {
			maxBody = rule.BodyMax
		}
	}
	body, err := readBody(r.Body, maxBody)
	if err != nil {
		p.deny(w, a, http.StatusRequestEntityTooLarge, "body_too_large", "")
		return
	}
	req := &rules.Request{Method: r.Method, Target: t, Body: body}

	for _, d := range res.Deny {
		if d.MatchDeny(r.Method, t.Segments) {
			a.fields["rule"] = d.Desc()
			p.deny(w, a, http.StatusForbidden, "denylist", fmt.Sprintf("%s %s is never allowed for %s", r.Method, t.Path, t.Resource))
			return
		}
	}
	var matched *rules.Rule
	for _, rule := range g.Rules {
		if rule.Match(req, res.BodyMax) {
			matched = rule
			break
		}
	}
	if matched == nil {
		p.deny(w, a, http.StatusForbidden, "no_rule", fmt.Sprintf("no rule in templates %v allows %s %s with these parameters",
			g.Templates, r.Method, t.Path))
		return
	}
	a.fields["rule"] = matched.Desc()
	a.fields["template"] = matched.Template

	switch reason := p.Grants.Reserve(g); reason {
	case "":
	case grants.ReasonRate, grants.ReasonResourceRate:
		w.Header().Set("Retry-After", "1")
		p.deny(w, a, http.StatusTooManyRequests, reason, "")
		return
	default:
		p.deny(w, a, http.StatusForbidden, reason, "")
		return
	}

	if p.Inflight != nil {
		select {
		case p.Inflight <- struct{}{}:
			defer func() { <-p.Inflight }()
		default:
			p.Grants.Refund(g)
			w.Header().Set("Retry-After", "1")
			p.deny(w, a, http.StatusServiceUnavailable, "busy", "")
			return
		}
	}

	respMax := res.RespMax
	if matched.ResponseMax > 0 {
		respMax = matched.ResponseMax
	}
	result, err := p.forward(r.Context(), res, t, r, body, respMax)
	if err != nil {
		var fe *forwardError
		status, reason := http.StatusBadGateway, "upstream_error"
		if errors.As(err, &fe) {
			status, reason = fe.status, fe.reason
		}
		if fe == nil || fe.refund {
			p.Grants.Refund(g)
		}
		p.deny(w, a, status, reason, "")
		return
	}

	scrubbed, n := p.Scrub.Scrub(result.body)
	if n > 0 {
		logger.Warn("secret_echo_redacted", logger.Fields{
			"grant_id": g.ID, "resource": t.Resource, "path": t.Path, "redactions": n,
		})
	}
	a.decision = "allow"
	a.fields["status"] = result.status
	a.fields["bytes"] = len(scrubbed)
	a.fields["upstream_ms"] = result.upstreamMS
	a.fields["redactions"] = n

	h := w.Header()
	if result.contentType != "" {
		h.Set("Content-Type", p.Scrub.KnownOnly(result.contentType))
	}
	if result.retryAfter != "" {
		h.Set("Retry-After", result.retryAfter)
	}
	if loc := sanitizeLocation(res, result.location); loc != "" {
		h.Set("Location", p.Scrub.ScrubString(loc))
	}
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("Cache-Control", "no-store")
	h.Set("X-JIT-Grant-Remaining", strconv.Itoa(g.MaxRequests-p.Grants.Used(g)))
	w.WriteHeader(result.status)
	_, _ = w.Write(scrubbed)
	p.finish(a)
}

type result struct {
	status      int
	body        []byte
	contentType string
	location    string
	retryAfter  string
	upstreamMS  int64
}

type forwardError struct {
	status int
	reason string
	refund bool
}

func (e *forwardError) Error() string { return e.reason }

func (p *Proxy) forward(ctx context.Context, res *templates.Resource, t *rules.Target, in *http.Request, body []byte, respMax int64) (*result, error) {
	inj := p.Injectors[res.Name]
	if inj == nil {
		return nil, &forwardError{http.StatusServiceUnavailable, "credential_unavailable", true}
	}
	timeout := p.Timeout
	if timeout <= 0 {
		timeout = 120 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	endpoint := upstream.Endpoint(res.UpstreamURL, t.EscapedPath(), t.Query.Encode())
	for attempt := 0; attempt < 2; attempt++ {
		out, err := http.NewRequestWithContext(ctx, in.Method, endpoint, bytes.NewReader(body))
		if err != nil {
			return nil, &forwardError{http.StatusBadGateway, "upstream_error", true}
		}
		if acc := in.Header.Get("Accept"); acc != "" && len(acc) < 256 {
			out.Header.Set("Accept", acc)
		}
		if len(body) > 0 {
			ct := in.Header.Get("Content-Type")
			if ct == "" || len(ct) > 128 {
				ct = "application/json"
			}
			out.Header.Set("Content-Type", ct)
		}
		out.Header.Set("User-Agent", "jit-proxy/1")
		if err := inj.Apply(ctx, out); err != nil {
			logger.Error("credential_unavailable", logger.Fields{"resource": res.Name, "error": p.Scrub.KnownOnly(err.Error())})
			return nil, &forwardError{http.StatusServiceUnavailable, "credential_unavailable", true}
		}
		start := time.Now()
		resp, err := p.Client.Do(out)
		if err != nil {
			if in.Method == http.MethodGet && attempt == 0 && ctx.Err() == nil {
				continue
			}
			if ctx.Err() != nil {
				return nil, &forwardError{http.StatusGatewayTimeout, "upstream_timeout", true}
			}
			return nil, &forwardError{http.StatusBadGateway, "upstream_unreachable", true}
		}
		data, err := readBody(resp.Body, respMax)
		resp.Body.Close()
		if err != nil {
			return nil, &forwardError{http.StatusBadGateway, "response_too_large", false}
		}
		if inj.Stale(resp.StatusCode, data) {
			inj.Reset()
			if attempt == 0 {
				p.Creds.Refresh(ctx, res.Name)
				continue
			}
			logger.Error("upstream_auth_failed", logger.Fields{"resource": res.Name, "status": resp.StatusCode})
			return nil, &forwardError{http.StatusBadGateway, "upstream_auth_failed", false}
		}
		ct := resp.Header.Get("Content-Type")
		if len(data) > 0 && !textual(ct) {
			return nil, &forwardError{http.StatusBadGateway, "unsupported_content_type", false}
		}
		ra := resp.Header.Get("Retry-After")
		if _, err := strconv.Atoi(ra); err != nil {
			ra = ""
		}
		return &result{
			status: resp.StatusCode, body: data, contentType: ct,
			location: resp.Header.Get("Location"), retryAfter: ra,
			upstreamMS: time.Since(start).Milliseconds(),
		}, nil
	}
	return nil, &forwardError{http.StatusBadGateway, "upstream_unreachable", true}
}

var errTooLarge = errors.New("too large")

func readBody(r io.Reader, max int64) ([]byte, error) {
	if r == nil {
		return nil, nil
	}
	data, err := io.ReadAll(io.LimitReader(r, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > max {
		return nil, errTooLarge
	}
	return data, nil
}

// textual reports whether a response type gets scrubbed as text. Binary
// types (images, media, archives) are never proxied: no template needs them.
func textual(ct string) bool {
	mt, _, err := mime.ParseMediaType(ct)
	if err != nil {
		return false
	}
	switch {
	case strings.HasPrefix(mt, "text/"):
		return true
	case mt == "application/json", mt == "application/xml", mt == "application/javascript",
		strings.HasSuffix(mt, "+json"), strings.HasSuffix(mt, "+xml"):
		return true
	}
	return false
}

// sanitizeLocation keeps a redirect target only if it stays on the
// resource's upstream, rewritten to the proxy path, without its query.
func sanitizeLocation(res *templates.Resource, loc string) string {
	if loc == "" {
		return ""
	}
	u, err := url.Parse(loc)
	if err != nil {
		return ""
	}
	if u.IsAbs() || u.Host != "" {
		if !strings.EqualFold(u.Host, res.UpstreamURL.Host) {
			return ""
		}
	}
	if !strings.HasPrefix(u.Path, "/") {
		return ""
	}
	return "/r/" + res.Name + u.EscapedPath()
}

func bearer(h string) string {
	const p = "Bearer "
	if len(h) > len(p) && strings.EqualFold(h[:len(p)], p) {
		return strings.TrimSpace(h[len(p):])
	}
	return ""
}

func headerHasToken(h http.Header, name, token string) bool {
	for _, v := range h.Values(name) {
		for _, part := range strings.Split(v, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
