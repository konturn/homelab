// Package templates loads the reviewed per-resource scope templates
// (docker/jit-proxy/templates/<resource>.yml, embedded in the binary).
//
// Each file names one resource's fixed upstream, how its credential is
// injected, an always-deny list and the named templates a grant can carry.
// A grant copies its templates' rules at registration, so editing a template
// never widens a live grant.
package templates

import (
	"bytes"
	"fmt"
	"io/fs"
	"net/url"
	"path"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/nkontur/jit-proxy/internal/rules"
)

// Auth types.
const (
	AuthHeader = "header" // Name: secret
	AuthQuery  = "query"  // ?Name=secret
	AuthBasic  = "basic"  // basic auth, username from UsernameField
	AuthDeluge = "deluge" // Deluge web JSON-RPC session (auth.login)
	AuthPihole = "pihole" // Pi-hole v6 session (POST /api/auth, X-FTL-SID)
)

// Auth says where the upstream credential comes from and how it is sent.
type Auth struct {
	Type          string `yaml:"type"`
	Name          string `yaml:"name"`
	VaultPath     string `yaml:"vault_path"`
	Field         string `yaml:"field"`
	UsernameField string `yaml:"username_field"`
}

// Template is a named set of allow rules.
type Template struct {
	Name        string        `yaml:"-"`
	Description string        `yaml:"description"`
	MinTier     int           `yaml:"min_tier"`
	SingleUse   bool          `yaml:"single_use"`
	MaxRequests int           `yaml:"max_requests"`
	Includes    []string      `yaml:"includes"`
	Allow       []*rules.Rule `yaml:"allow"`
}

// Resource is one proxied service.
type Resource struct {
	Name     string               `yaml:"resource"`
	MinTier  int                  `yaml:"min_tier"`
	Upstream string               `yaml:"upstream"`
	PlainOK  bool                 `yaml:"plain_http_on_internal"`
	Auth     Auth                 `yaml:"auth"`
	BodyMax  int64                `yaml:"body_max"`
	RespMax  int64                `yaml:"response_max"`
	Deny     []*rules.Rule        `yaml:"deny"`
	Tmpl     map[string]*Template `yaml:"templates"`

	UpstreamURL *url.URL `yaml:"-"`
}

// Defaults.
const (
	DefaultMaxRequests = 1000
	DefaultBodyMax     = 1 << 20
	DefaultRespMax     = 16 << 20
	HardBodyMax        = 8 << 20
	HardRespMax        = 32 << 20
)

// Set is every loaded resource by name.
type Set map[string]*Resource

// Load reads every *.yml in dir of fsys.
func Load(fsys fs.FS, dir string) (Set, error) {
	entries, err := fs.ReadDir(fsys, dir)
	if err != nil {
		return nil, err
	}
	set := Set{}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".yml") {
			continue
		}
		data, err := fs.ReadFile(fsys, path.Join(dir, e.Name()))
		if err != nil {
			return nil, err
		}
		r, err := Parse(data)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", e.Name(), err)
		}
		if r.Name+".yml" != e.Name() {
			return nil, fmt.Errorf("%s: resource %q must live in %s.yml", e.Name(), r.Name, r.Name)
		}
		set[r.Name] = r
	}
	if len(set) == 0 {
		return nil, fmt.Errorf("no templates in %s", dir)
	}
	return set, nil
}

// Parse parses and validates one resource file.
func Parse(data []byte) (*Resource, error) {
	var r Resource
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(&r); err != nil {
		return nil, err
	}
	if err := r.validate(); err != nil {
		return nil, err
	}
	return &r, nil
}

func validTier(t int) bool { return t >= 1 && t <= 3 }

func (r *Resource) validate() error {
	if r.Name == "" {
		return fmt.Errorf("resource name missing")
	}
	if !validTier(r.MinTier) {
		return fmt.Errorf("%s: min_tier must be 1..3", r.Name)
	}
	u, err := url.Parse(r.Upstream)
	if err != nil || u.Host == "" || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.User != nil {
		return fmt.Errorf("%s: upstream must be scheme://host[:port]", r.Name)
	}
	switch u.Scheme {
	case "https":
	case "http":
		// Plain HTTP only to a bare container name on the internal
		// network, and only when the file says so explicitly.
		if !r.PlainOK || strings.Contains(u.Hostname(), ".") {
			return fmt.Errorf("%s: plain http needs plain_http_on_internal and a bare container name", r.Name)
		}
	default:
		return fmt.Errorf("%s: upstream scheme %q", r.Name, u.Scheme)
	}
	u.Path = ""
	r.UpstreamURL = u

	a := r.Auth
	if a.VaultPath == "" || !strings.HasPrefix(a.VaultPath, "homelab/data/") || a.Field == "" {
		return fmt.Errorf("%s: auth needs vault_path under homelab/data/ and field", r.Name)
	}
	switch a.Type {
	case AuthHeader, AuthQuery:
		if a.Name == "" {
			return fmt.Errorf("%s: %s auth needs name", r.Name, a.Type)
		}
	case AuthBasic:
		if a.UsernameField == "" {
			return fmt.Errorf("%s: basic auth needs username_field", r.Name)
		}
	case AuthDeluge, AuthPihole:
	default:
		return fmt.Errorf("%s: unknown auth type %q", r.Name, a.Type)
	}

	if r.BodyMax <= 0 {
		r.BodyMax = DefaultBodyMax
	}
	if r.RespMax <= 0 {
		r.RespMax = DefaultRespMax
	}
	if r.BodyMax > HardBodyMax || r.RespMax > HardRespMax {
		return fmt.Errorf("%s: body/response cap above the hard maximum", r.Name)
	}
	for _, d := range r.Deny {
		if err := d.Compile(true); err != nil {
			return fmt.Errorf("%s: %w", r.Name, err)
		}
	}
	if len(r.Tmpl) == 0 {
		return fmt.Errorf("%s: no templates", r.Name)
	}
	for name, t := range r.Tmpl {
		t.Name = name
		if !validTemplateName(name) {
			return fmt.Errorf("%s: bad template name %q", r.Name, name)
		}
		if t.Description == "" {
			return fmt.Errorf("%s/%s: description missing", r.Name, name)
		}
		if !validTier(t.MinTier) {
			return fmt.Errorf("%s/%s: min_tier must be 1..3", r.Name, name)
		}
		if t.MaxRequests < 0 {
			return fmt.Errorf("%s/%s: max_requests < 0", r.Name, name)
		}
		if t.MaxRequests == 0 {
			t.MaxRequests = DefaultMaxRequests
		}
		if t.SingleUse {
			t.MaxRequests = 1
		}
		for _, inc := range t.Includes {
			it, ok := r.Tmpl[inc]
			if !ok || inc == name {
				return fmt.Errorf("%s/%s: includes unknown template %q", r.Name, name, inc)
			}
			if len(it.Includes) > 0 {
				return fmt.Errorf("%s/%s: includes may not nest (%s)", r.Name, name, inc)
			}
		}
		if len(t.Allow) == 0 && len(t.Includes) == 0 {
			return fmt.Errorf("%s/%s: no allow rules", r.Name, name)
		}
		for _, rule := range t.Allow {
			if err := rule.Compile(false); err != nil {
				return fmt.Errorf("%s/%s: %w", r.Name, name, err)
			}
			rule.Template = name
			if rule.BodyMax > HardBodyMax || rule.ResponseMax > HardRespMax {
				return fmt.Errorf("%s/%s: rule %s above the hard maximum", r.Name, name, rule.Desc())
			}
		}
	}
	return nil
}

func validTemplateName(s string) bool {
	if s == "" || len(s) > 32 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-') {
			return false
		}
	}
	return true
}

// Tier is the effective tier of a set of templates: the highest of the
// resource's minimum and each template's minimum.
func (r *Resource) Tier(names []string) (int, error) {
	tier := r.MinTier
	for _, n := range names {
		t, ok := r.Tmpl[n]
		if !ok {
			return 0, fmt.Errorf("unknown template %q for %s", n, r.Name)
		}
		if t.MinTier > tier {
			tier = t.MinTier
		}
		for _, inc := range t.Includes {
			if it := r.Tmpl[inc]; it.MinTier > tier {
				tier = it.MinTier
			}
		}
	}
	return tier, nil
}

// Grant is what a set of templates resolves to: the rules (copied, so later
// template edits never change them), the request budget and single-use flag.
type Grant struct {
	Rules       []*rules.Rule
	MaxRequests int
	SingleUse   bool
}

// Resolve resolves template names (each at most once) to a grant's rules.
func (r *Resource) Resolve(names []string) (*Grant, error) {
	if len(names) == 0 {
		return nil, fmt.Errorf("no templates requested")
	}
	seen := map[string]bool{}
	g := &Grant{MaxRequests: -1}
	for _, n := range names {
		if seen[n] {
			return nil, fmt.Errorf("template %q listed twice", n)
		}
		seen[n] = true
		t, ok := r.Tmpl[n]
		if !ok {
			return nil, fmt.Errorf("unknown template %q for %s", n, r.Name)
		}
		if g.MaxRequests < 0 || t.MaxRequests < g.MaxRequests {
			g.MaxRequests = t.MaxRequests
		}
		if t.SingleUse {
			g.SingleUse = true
		}
		add := func(tt *Template) {
			for _, rule := range tt.Allow {
				c := *rule
				g.Rules = append(g.Rules, &c)
			}
		}
		add(t)
		for _, inc := range t.Includes {
			add(r.Tmpl[inc])
		}
	}
	if g.SingleUse {
		g.MaxRequests = 1
	}
	return g, nil
}

// TemplateInfo is the public description of a template (the broker's catalog).
type TemplateInfo struct {
	Description string `json:"description"`
	MinTier     int    `json:"min_tier"`
	SingleUse   bool   `json:"single_use"`
}

// ResourceInfo is the public description of a resource.
type ResourceInfo struct {
	MinTier   int                     `json:"min_tier"`
	Templates map[string]TemplateInfo `json:"templates"`
}

// Catalog describes every resource and template (no secrets, no rules).
func (s Set) Catalog() map[string]ResourceInfo {
	out := map[string]ResourceInfo{}
	for name, r := range s {
		ri := ResourceInfo{MinTier: r.MinTier, Templates: map[string]TemplateInfo{}}
		for tn, t := range r.Tmpl {
			tier, _ := r.Tier([]string{tn})
			ri.Templates[tn] = TemplateInfo{Description: t.Description, MinTier: tier, SingleUse: t.SingleUse}
		}
		out[name] = ri
	}
	return out
}

// Names returns the resource names, sorted.
func (s Set) Names() []string {
	var out []string
	for n := range s {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}
