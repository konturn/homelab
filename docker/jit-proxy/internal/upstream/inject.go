// Package upstream attaches the real credential to a request bound for a
// proxied service. Session-based services (Deluge, Pi-hole) are logged in by
// the proxy; the session never leaves it.
package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"

	"github.com/nkontur/jit-proxy/internal/secrets"
	"github.com/nkontur/jit-proxy/internal/templates"
)

// Injector adds a resource's credential to upstream requests.
type Injector interface {
	// Apply adds the credential to req (built by the proxy, never the agent's).
	Apply(ctx context.Context, req *http.Request) error
	// Stale reports whether a response means the credential or session is no
	// longer accepted. The caller then calls Reset, refreshes and retries once.
	Stale(status int, body []byte) bool
	// Reset drops any session so the next Apply logs in again.
	Reset()
}

// CredFunc returns the current credential of a resource.
type CredFunc func() (secrets.Credential, bool)

// SessionSink is told about live session values so they are redacted too.
type SessionSink func(owner string, values ...string)

// New returns the injector for a resource.
func New(r *templates.Resource, cred CredFunc, client *http.Client, sink SessionSink) (Injector, error) {
	base := r.UpstreamURL.String()
	switch r.Auth.Type {
	case templates.AuthHeader:
		return &headerAuth{name: r.Auth.Name, cred: cred}, nil
	case templates.AuthQuery:
		return &queryAuth{name: r.Auth.Name, cred: cred}, nil
	case templates.AuthBasic:
		return &basicAuth{cred: cred}, nil
	case templates.AuthDeluge:
		return &delugeAuth{base: base, cred: cred, client: client, sink: sink, owner: r.Name + "/session"}, nil
	case templates.AuthPihole:
		return &piholeAuth{base: base, cred: cred, client: client, sink: sink, owner: r.Name + "/session"}, nil
	}
	return nil, fmt.Errorf("unknown auth type %q", r.Auth.Type)
}

func needCred(cred CredFunc) (secrets.Credential, error) {
	c, ok := cred()
	if !ok || c.Secret == "" {
		return c, fmt.Errorf("credential not loaded")
	}
	return c, nil
}

type headerAuth struct {
	name string
	cred CredFunc
}

func (h *headerAuth) Apply(_ context.Context, req *http.Request) error {
	c, err := needCred(h.cred)
	if err != nil {
		return err
	}
	req.Header.Set(h.name, c.Secret)
	return nil
}
func (h *headerAuth) Stale(status int, _ []byte) bool { return status == http.StatusUnauthorized }
func (h *headerAuth) Reset()                          {}

type queryAuth struct {
	name string
	cred CredFunc
}

func (q *queryAuth) Apply(_ context.Context, req *http.Request) error {
	c, err := needCred(q.cred)
	if err != nil {
		return err
	}
	v := req.URL.Query()
	v.Set(q.name, c.Secret)
	req.URL.RawQuery = v.Encode()
	return nil
}
func (q *queryAuth) Stale(status int, _ []byte) bool { return status == http.StatusUnauthorized }
func (q *queryAuth) Reset()                          {}

type basicAuth struct{ cred CredFunc }

func (b *basicAuth) Apply(_ context.Context, req *http.Request) error {
	c, err := needCred(b.cred)
	if err != nil {
		return err
	}
	req.SetBasicAuth(c.Username, c.Secret)
	return nil
}
func (b *basicAuth) Stale(status int, _ []byte) bool { return status == http.StatusUnauthorized }
func (b *basicAuth) Reset()                          {}

// rpc posts one JSON-RPC call and decodes the result.
func rpc(ctx context.Context, client *http.Client, endpoint string, cookie *http.Cookie, method string, params []interface{}) (json.RawMessage, *http.Response, error) {
	body, _ := json.Marshal(map[string]interface{}{"method": method, "params": params, "id": 1})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if cookie != nil {
		req.AddCookie(cookie)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out struct {
		Result json.RawMessage `json:"result"`
		Error  *struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, resp, fmt.Errorf("%s: HTTP %d, not JSON-RPC", method, resp.StatusCode)
	}
	if out.Error != nil {
		return nil, resp, fmt.Errorf("%s: upstream error", method)
	}
	return out.Result, resp, nil
}

// delugeAuth holds a Deluge web session: auth.login with the web password,
// then connect the web UI to its daemon if it is not connected.
type delugeAuth struct {
	base   string
	cred   CredFunc
	client *http.Client
	sink   SessionSink
	owner  string

	mu     sync.Mutex
	cookie *http.Cookie
}

func (d *delugeAuth) login(ctx context.Context) error {
	c, err := needCred(d.cred)
	if err != nil {
		return err
	}
	endpoint := d.base + "/json"
	res, resp, err := rpc(ctx, d.client, endpoint, nil, "auth.login", []interface{}{c.Secret})
	if err != nil {
		return fmt.Errorf("deluge login failed")
	}
	if string(res) != "true" {
		return fmt.Errorf("deluge login refused")
	}
	var sess *http.Cookie
	for _, ck := range resp.Cookies() {
		if ck.Name == "_session_id" {
			sess = &http.Cookie{Name: ck.Name, Value: ck.Value}
		}
	}
	if sess == nil {
		return fmt.Errorf("deluge login: no session cookie")
	}
	d.sink(d.owner, sess.Value)
	connected, _, err := rpc(ctx, d.client, endpoint, sess, "web.connected", []interface{}{})
	if err != nil {
		return fmt.Errorf("deluge web.connected failed")
	}
	if string(connected) != "true" {
		hostsRaw, _, err := rpc(ctx, d.client, endpoint, sess, "web.get_hosts", []interface{}{})
		if err != nil {
			return fmt.Errorf("deluge web.get_hosts failed")
		}
		var hosts [][]interface{}
		if err := json.Unmarshal(hostsRaw, &hosts); err != nil || len(hosts) == 0 || len(hosts[0]) == 0 {
			return fmt.Errorf("deluge: no daemon host configured")
		}
		if _, _, err := rpc(ctx, d.client, endpoint, sess, "web.connect", []interface{}{hosts[0][0]}); err != nil {
			return fmt.Errorf("deluge web.connect failed")
		}
	}
	d.cookie = sess
	return nil
}

func (d *delugeAuth) Apply(ctx context.Context, req *http.Request) error {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.cookie == nil {
		if err := d.login(ctx); err != nil {
			return err
		}
	}
	req.AddCookie(d.cookie)
	return nil
}

// Deluge answers {"error": {"message": "Not authenticated", "code": 1}}
// with HTTP 200 when the session is gone.
func (d *delugeAuth) Stale(status int, body []byte) bool {
	if status == http.StatusUnauthorized {
		return true
	}
	var out struct {
		Error *struct {
			Code int `json:"code"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &out) == nil && out.Error != nil && out.Error.Code == 1 {
		return true
	}
	return false
}

func (d *delugeAuth) Reset() {
	d.mu.Lock()
	d.cookie = nil
	d.mu.Unlock()
}

// piholeAuth holds a Pi-hole v6 session from the app password. Pi-hole has
// a small session limit, so the proxy keeps one session and reuses it until
// Pi-hole answers 401.
type piholeAuth struct {
	base   string
	cred   CredFunc
	client *http.Client
	sink   SessionSink
	owner  string

	mu  sync.Mutex
	sid string
}

func (p *piholeAuth) login(ctx context.Context) error {
	c, err := needCred(p.cred)
	if err != nil {
		return err
	}
	body, _ := json.Marshal(map[string]string{"password": c.Secret})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, p.base+"/api/auth", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := p.client.Do(req)
	if err != nil {
		return fmt.Errorf("pihole login failed")
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	var out struct {
		Session struct {
			Valid bool   `json:"valid"`
			SID   string `json:"sid"`
		} `json:"session"`
	}
	if resp.StatusCode != http.StatusOK || json.Unmarshal(data, &out) != nil || !out.Session.Valid || out.Session.SID == "" {
		return fmt.Errorf("pihole login refused (HTTP %d)", resp.StatusCode)
	}
	p.sid = out.Session.SID
	p.sink(p.owner, p.sid)
	return nil
}

func (p *piholeAuth) Apply(ctx context.Context, req *http.Request) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.sid == "" {
		if err := p.login(ctx); err != nil {
			return err
		}
	}
	req.Header.Set("X-FTL-SID", p.sid)
	return nil
}

func (p *piholeAuth) Stale(status int, _ []byte) bool { return status == http.StatusUnauthorized }

func (p *piholeAuth) Reset() {
	p.mu.Lock()
	p.sid = ""
	p.mu.Unlock()
}

// Endpoint joins the upstream base and a canonical escaped path and query.
func Endpoint(base *url.URL, escapedPath, rawQuery string) string {
	u := *base
	u.RawPath = ""
	u.Path = ""
	s := u.String() + escapedPath
	if rawQuery != "" {
		s += "?" + rawQuery
	}
	return s
}
