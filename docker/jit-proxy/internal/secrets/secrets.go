// Package secrets reads the proxied services' credentials from Vault with the
// proxy's own AppRole (policy jit-proxy: read on exactly those KV paths) and
// keeps them in memory only. Nothing is written to disk.
package secrets

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Reader reads KV v2 secrets.
type Reader interface {
	Read(ctx context.Context, path string) (map[string]string, error)
}

// Vault is a minimal AppRole + KV v2 client (no SDK: fewer dependencies in
// the component that parses untrusted bytes).
type Vault struct {
	Addr     string
	RoleID   string
	SecretID string
	Client   *http.Client

	mu      sync.Mutex
	token   string
	expires time.Time
}

func (v *Vault) login(ctx context.Context) error {
	body, _ := json.Marshal(map[string]string{"role_id": v.RoleID, "secret_id": v.SecretID})
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, v.Addr+"/v1/auth/approle/login", bytes.NewReader(body))
	if err != nil {
		return err
	}
	resp, err := v.Client.Do(req)
	if err != nil {
		return fmt.Errorf("vault login: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("vault login: HTTP %d", resp.StatusCode)
	}
	var out struct {
		Auth struct {
			ClientToken   string `json:"client_token"`
			LeaseDuration int    `json:"lease_duration"`
		} `json:"auth"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil || out.Auth.ClientToken == "" {
		return fmt.Errorf("vault login: no token")
	}
	v.token = out.Auth.ClientToken
	life := time.Duration(out.Auth.LeaseDuration) * time.Second
	if life <= 0 {
		life = 10 * time.Minute
	}
	v.expires = time.Now().Add(life * 3 / 4)
	return nil
}

// Read reads a KV v2 path ("homelab/data/...") and returns its string fields.
func (v *Vault) Read(ctx context.Context, path string) (map[string]string, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	for attempt := 0; attempt < 2; attempt++ {
		if v.token == "" || time.Now().After(v.expires) {
			if err := v.login(ctx); err != nil {
				return nil, err
			}
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.Addr+"/v1/"+path, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("X-Vault-Token", v.token)
		resp, err := v.Client.Do(req)
		if err != nil {
			return nil, fmt.Errorf("vault read %s: %w", path, err)
		}
		data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		resp.Body.Close()
		if resp.StatusCode == http.StatusForbidden && attempt == 0 {
			v.token = "" // token expired or revoked: log in again once
			continue
		}
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("vault read %s: HTTP %d", path, resp.StatusCode)
		}
		var out struct {
			Data struct {
				Data map[string]interface{} `json:"data"`
			} `json:"data"`
		}
		if err := json.Unmarshal(data, &out); err != nil {
			return nil, fmt.Errorf("vault read %s: bad response", path)
		}
		fields := map[string]string{}
		for k, val := range out.Data.Data {
			if s, ok := val.(string); ok {
				fields[k] = s
			}
		}
		return fields, nil
	}
	return nil, fmt.Errorf("vault read %s: forbidden", path)
}

// Credential is one resource's upstream credential.
type Credential struct {
	Secret   string
	Username string
}

// Spec says where a resource's credential lives.
type Spec struct {
	VaultPath     string
	Field         string
	UsernameField string
}

// Cache holds the credentials of every proxied resource.
type Cache struct {
	reader  Reader
	specs   map[string]Spec
	onLoad  func(resource string, c Credential)
	minGap  time.Duration
	mu      sync.RWMutex
	creds   map[string]Credential
	lastRef map[string]time.Time
}

// NewCache creates a cache; onLoad is called whenever a resource's
// credential is (re)loaded (the redactor learns the new value there).
func NewCache(r Reader, specs map[string]Spec, onLoad func(string, Credential)) *Cache {
	return &Cache{reader: r, specs: specs, onLoad: onLoad, minGap: time.Minute,
		creds: map[string]Credential{}, lastRef: map[string]time.Time{}}
}

// LoadAll reads every resource's credential. It returns the first error but
// keeps going, so one missing path does not block the others.
func (c *Cache) LoadAll(ctx context.Context) error {
	var first error
	for res := range c.specs {
		if err := c.load(ctx, res); err != nil && first == nil {
			first = err
		}
	}
	return first
}

func (c *Cache) load(ctx context.Context, res string) error {
	spec := c.specs[res]
	fields, err := c.reader.Read(ctx, spec.VaultPath)
	if err != nil {
		return fmt.Errorf("%s: %w", res, err)
	}
	cred := Credential{Secret: fields[spec.Field]}
	if spec.UsernameField != "" {
		cred.Username = fields[spec.UsernameField]
	}
	if strings.TrimSpace(cred.Secret) == "" {
		return fmt.Errorf("%s: field %q is empty", res, spec.Field)
	}
	c.mu.Lock()
	c.creds[res] = cred
	c.lastRef[res] = time.Now()
	c.mu.Unlock()
	if c.onLoad != nil {
		c.onLoad(res, cred)
	}
	return nil
}

// Get returns a resource's credential.
func (c *Cache) Get(res string) (Credential, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	cr, ok := c.creds[res]
	return cr, ok
}

// Refresh re-reads one resource's credential, at most once per minute (the
// signal during a rotation is an upstream 401). It reports whether it read.
func (c *Cache) Refresh(ctx context.Context, res string) bool {
	c.mu.RLock()
	last := c.lastRef[res]
	c.mu.RUnlock()
	if time.Since(last) < c.minGap {
		return false
	}
	return c.load(ctx, res) == nil
}

// Loaded lists the resources with a credential.
func (c *Cache) Loaded() []string {
	c.mu.RLock()
	defer c.mu.RUnlock()
	var out []string
	for r := range c.creds {
		out = append(out, r)
	}
	return out
}
