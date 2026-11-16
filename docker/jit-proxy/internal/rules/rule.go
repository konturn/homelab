package rules

import (
	"fmt"
	"regexp"
	"strings"
)

// Methods the proxy ever forwards. Anything else (CONNECT, TRACE, OPTIONS,
// lower-case spellings, extension methods) is refused before matching.
var Methods = map[string]bool{"GET": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true}

// RPC constrains a JSON-RPC body: the method must be listed, and each
// constrained positional param must pass its spec.
type RPC struct {
	Methods []string          `yaml:"methods"`
	Params  map[int]ValueSpec `yaml:"params"`
}

// Rule is one allow or deny rule. Deny rules use only Method and Path.
type Rule struct {
	Method       string               `yaml:"method"`
	Path         string               `yaml:"path"`
	Query        map[string]ValueSpec `yaml:"query"`
	Require      []string             `yaml:"require"`
	JSON         map[string]ValueSpec `yaml:"json"`
	JSONOptional []string             `yaml:"json_optional"`
	RPC          *RPC                 `yaml:"rpc"`
	BodyMax      int64                `yaml:"body_max"`
	ResponseMax  int64                `yaml:"response_max"`

	// Set by Compile.
	Template string `yaml:"-"`
	segs     []seg
}

// Desc is the rule as it appears in audit logs.
func (r *Rule) Desc() string {
	m := r.Method
	if m == "" {
		m = "*"
	}
	d := m + " " + r.Path
	if r.RPC != nil {
		d += " rpc:" + strings.Join(r.RPC.Methods, "|")
	}
	return d
}

type segKind int

const (
	segLit segKind = iota
	segAny
	segInt
	segHex
	segEnum
	segRest
)

type seg struct {
	kind segKind
	lit  string
	enum []string
}

var paramRe = regexp.MustCompile(`^\{[a-zA-Z_][a-zA-Z0-9_]*(:([a-z]+|[A-Za-z0-9_.-]+(\|[A-Za-z0-9_.-]+)+))?\}$`)

// Compile validates the rule and prepares its path pattern.
func (r *Rule) Compile(deny bool) error {
	if r.Method != "" && !Methods[r.Method] {
		return fmt.Errorf("rule %q: unsupported method %q", r.Path, r.Method)
	}
	if !deny && r.Method == "" {
		return fmt.Errorf("rule %q: allow rules need a method", r.Path)
	}
	if !strings.HasPrefix(r.Path, "/") {
		return fmt.Errorf("rule %q: path must start with /", r.Path)
	}
	r.segs = nil
	if r.Path != "/" {
		parts := strings.Split(r.Path[1:], "/")
		for i, p := range parts {
			switch {
			case p == "":
				return fmt.Errorf("rule %q: empty segment", r.Path)
			case p == "**":
				if i != len(parts)-1 {
					return fmt.Errorf("rule %q: ** must be last", r.Path)
				}
				if !deny {
					return fmt.Errorf("rule %q: ** is only allowed in deny rules", r.Path)
				}
				r.segs = append(r.segs, seg{kind: segRest})
			case p == "*":
				r.segs = append(r.segs, seg{kind: segAny})
			case strings.HasPrefix(p, "{"):
				if !paramRe.MatchString(p) {
					return fmt.Errorf("rule %q: bad parameter %q", r.Path, p)
				}
				inner := p[1 : len(p)-1]
				_, typ, _ := strings.Cut(inner, ":")
				switch {
				case typ == "":
					r.segs = append(r.segs, seg{kind: segAny})
				case typ == "int":
					r.segs = append(r.segs, seg{kind: segInt})
				case typ == "hex":
					r.segs = append(r.segs, seg{kind: segHex})
				case strings.Contains(typ, "|"):
					r.segs = append(r.segs, seg{kind: segEnum, enum: strings.Split(typ, "|")})
				default:
					return fmt.Errorf("rule %q: unknown parameter type %q", r.Path, typ)
				}
			default:
				if strings.ContainsAny(p, "{}*%?#;\\") {
					return fmt.Errorf("rule %q: bad literal segment %q", r.Path, p)
				}
				r.segs = append(r.segs, seg{kind: segLit, lit: p})
			}
		}
	}
	if deny {
		if r.Query != nil || r.JSON != nil || r.RPC != nil || len(r.Require) > 0 {
			return fmt.Errorf("deny rule %q: only method and path apply", r.Path)
		}
		return nil
	}
	for k := range r.Query {
		if IsCredentialParam(k) {
			return fmt.Errorf("rule %q: query parameter %q carries credentials", r.Path, k)
		}
	}
	for _, k := range r.Require {
		if _, ok := r.Query[k]; !ok {
			return fmt.Errorf("rule %q: required parameter %q is not allowed", r.Path, k)
		}
	}
	for _, k := range r.JSONOptional {
		if _, ok := r.JSON[k]; !ok {
			return fmt.Errorf("rule %q: optional field %q has no spec", r.Path, k)
		}
	}
	if r.JSON != nil && r.RPC != nil {
		return fmt.Errorf("rule %q: json and rpc are exclusive", r.Path)
	}
	if r.RPC != nil && len(r.RPC.Methods) == 0 {
		return fmt.Errorf("rule %q: rpc without methods", r.Path)
	}
	if (r.JSON != nil || r.RPC != nil) && (r.Method == "GET" || r.Method == "DELETE") {
		return fmt.Errorf("rule %q: body rules need POST, PUT or PATCH", r.Path)
	}
	return nil
}

// MatchPath reports whether the canonical path segments match the pattern.
func (r *Rule) MatchPath(segs []string) bool {
	i := 0
	for _, s := range r.segs {
		if s.kind == segRest {
			return true
		}
		if i >= len(segs) {
			return false
		}
		v := segs[i]
		switch s.kind {
		case segLit:
			// Literals match case-insensitively: ASP.NET Core and Plex route
			// case-insensitively, so a deny rule must catch every spelling.
			if !strings.EqualFold(v, s.lit) {
				return false
			}
		case segAny:
			if v == "" {
				return false
			}
		case segInt:
			if !intSegRe.MatchString(v) {
				return false
			}
		case segHex:
			if !hexRe.MatchString(v) {
				return false
			}
		case segEnum:
			ok := false
			for _, e := range s.enum {
				if strings.EqualFold(v, e) {
					ok = true
					break
				}
			}
			if !ok {
				return false
			}
		}
		i++
	}
	return i == len(segs)
}

var intSegRe = regexp.MustCompile(`^[0-9]{1,18}$`)

// MatchDeny reports whether a deny rule applies (method, if set, and path).
func (r *Rule) MatchDeny(method string, segs []string) bool {
	if r.Method != "" && r.Method != method {
		return false
	}
	return r.MatchPath(segs)
}

// Request is the normalised request a rule is matched against.
type Request struct {
	Method string
	Target *Target
	Body   []byte
}

// Match reports whether an allow rule permits the request. defaultBodyMax
// applies when the rule sets none.
func (r *Rule) Match(req *Request, defaultBodyMax int64) bool {
	if r.Method != req.Method || !r.MatchPath(req.Target.Segments) {
		return false
	}
	for k, v := range req.Target.Query {
		spec, ok := r.Query[k]
		if !ok || !spec.Check(v[0]) {
			return false
		}
	}
	for _, k := range r.Require {
		if _, ok := req.Target.Query[k]; !ok {
			return false
		}
	}
	bodyMax := r.BodyMax
	if bodyMax <= 0 {
		bodyMax = defaultBodyMax
	}
	if int64(len(req.Body)) > bodyMax {
		return false
	}
	switch req.Method {
	case "GET", "DELETE":
		if len(req.Body) != 0 {
			return false
		}
	}
	if r.JSON != nil {
		obj, err := StrictObject(req.Body)
		if err != nil {
			return false
		}
		for k, spec := range r.JSON {
			raw, ok := obj[strings.ToLower(k)]
			if !ok {
				if containsFold(r.JSONOptional, k) {
					continue
				}
				return false
			}
			if !spec.CheckJSON(raw) {
				return false
			}
		}
	}
	if r.RPC != nil {
		call, err := ParseRPC(req.Body)
		if err != nil {
			return false
		}
		found := false
		for _, m := range r.RPC.Methods {
			if call.Method == m {
				found = true
				break
			}
		}
		if !found {
			return false
		}
		for i, spec := range r.RPC.Params {
			if i < 0 || i >= len(call.Params) || !spec.CheckJSON(call.Params[i]) {
				return false
			}
		}
	}
	return true
}

func containsFold(list []string, s string) bool {
	for _, x := range list {
		if strings.EqualFold(x, s) {
			return true
		}
	}
	return false
}
