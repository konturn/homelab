// Package redact removes secrets from upstream responses and from log lines.
//
// Two passes run on every text response:
//
//   - Known secrets: every credential the proxy holds (for all resources, not
//     only the one being called, plus live session IDs) is replaced in raw,
//     URL-encoded, JSON-escaped, hex-case and base64 forms. Base64 is matched
//     at all three byte alignments, so a secret embedded in a larger encoded
//     blob (basic-auth headers, tokens in encoded URLs) is caught too.
//   - Patterns: values of JSON fields and XML attributes whose names say they
//     hold credentials, apikey=/token= query values in URLs, Servarr
//     "fields" entries for API keys and passwords, and grant secrets (jitp_).
//
// Responses are buffered (capped per template) and scrubbed whole, so a
// secret split across network reads is still seen in one piece.
package redact

import (
	"encoding/base64"
	"encoding/json"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// Marker replaces every redacted value.
const Marker = "[REDACTED]"

// MinSecretLen is the shortest secret the known-secret pass will look for;
// shorter values would match ordinary text. Secrets are checked at load.
const MinSecretLen = 6

// minB64Core is the shortest base64 fragment searched for.
const minB64Core = 8

// Scrubber holds the current set of known secrets.
type Scrubber struct {
	mu       sync.RWMutex
	sources  map[string][]string // owner -> secrets (resource, session)
	patterns []string            // every encoded variant, longest first
	replacer *strings.Replacer
}

// New returns an empty scrubber.
func New() *Scrubber {
	s := &Scrubber{sources: map[string][]string{}}
	s.rebuild()
	return s
}

// Set replaces the secrets owned by one source (a resource name, or a
// session key such as "deluge/session").
func (s *Scrubber) Set(owner string, secrets ...string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	var keep []string
	for _, v := range secrets {
		// Values this short would match ordinary text everywhere; the
		// caller logs secret_short for a credential like that.
		if len(v) >= MinSecretLen {
			keep = append(keep, v)
		}
	}
	s.sources[owner] = keep
	s.rebuild()
}

func (s *Scrubber) rebuild() {
	set := map[string]bool{}
	for _, list := range s.sources {
		for _, sec := range list {
			for _, v := range Variants(sec) {
				set[v] = true
			}
		}
	}
	pats := make([]string, 0, len(set))
	for p := range set {
		pats = append(pats, p)
	}
	// Longest first, so a longer encoding is replaced whole before a shorter
	// variant that it contains.
	sort.Slice(pats, func(i, j int) bool {
		if len(pats[i]) != len(pats[j]) {
			return len(pats[i]) > len(pats[j])
		}
		return pats[i] < pats[j]
	})
	s.patterns = pats
	args := make([]string, 0, 2*len(pats))
	for _, p := range pats {
		args = append(args, p, Marker)
	}
	s.replacer = strings.NewReplacer(args...)
}

// Variants returns every encoding of a secret the scrubber looks for.
func Variants(secret string) []string {
	if secret == "" {
		return nil
	}
	set := map[string]bool{}
	add := func(v string) {
		if v != "" {
			set[v] = true
		}
	}
	for _, base := range []string{secret, strings.ToLower(secret), strings.ToUpper(secret)} {
		if base != secret && !isHexish(secret) {
			continue // case variants only for hex-like keys
		}
		add(base)
		add(url.QueryEscape(base))
		add(url.PathEscape(base))
		add(jsonEscape(base, true))
		add(jsonEscape(base, false))
	}
	for _, enc := range []*base64.Encoding{base64.StdEncoding, base64.URLEncoding} {
		for _, core := range base64Cores(enc, secret) {
			add(core)
		}
	}
	out := make([]string, 0, len(set))
	for v := range set {
		out = append(out, v)
	}
	sort.Strings(out)
	return out
}

func isHexish(s string) bool {
	for _, c := range s {
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F' || c == '-') {
			return false
		}
	}
	return true
}

func jsonEscape(s string, html bool) string {
	var b strings.Builder
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(html)
	_ = enc.Encode(s)
	out := strings.TrimSuffix(b.String(), "\n")
	return strings.TrimSuffix(strings.TrimPrefix(out, `"`), `"`)
}

// base64Cores returns, for each of the three byte alignments, the part of
// the encoding that depends only on the secret's bytes. Whatever surrounds
// the secret in a larger encoded blob, one of these appears verbatim.
func base64Cores(enc *base64.Encoding, secret string) []string {
	var out []string
	for k := 0; k < 3; k++ {
		buf := make([]byte, k+len(secret))
		copy(buf[k:], secret)
		e := strings.TrimRight(enc.EncodeToString(buf), "=")
		drop := map[int]int{0: 0, 1: 2, 2: 3}[k]
		if len(e) <= drop {
			continue
		}
		e = e[drop:]
		if n := (k + len(secret)) % 3; n != 0 && len(e) > 0 {
			e = e[:len(e)-1]
		}
		if len(e) >= minB64Core {
			out = append(out, e)
		}
	}
	return out
}

var (
	// "apiKey": "...", "password": "...", "accessToken": "..." etc.
	jsonFieldRe = regexp.MustCompile(`(?i)("[a-z0-9_\-]*(?:apikey|api_key|password|passwd|passkey|token|secret|cookie|_sid|sessionid|session_id)"\s*:\s*)"(?:[^"\\]|\\.)*"`)
	// Servarr provider fields: {"name":"apiKey", ... "value":"..."}
	servarrFieldRe = regexp.MustCompile(`(?i)("name"\s*:\s*"[a-z0-9_.]*(?:apikey|api_key|password|passkey|token|secret|cookie|username)"[^{}]*?"value"\s*:\s*)"(?:[^"\\]|\\.)*"`)
	// XML attributes: token="...", accessToken="..."
	xmlAttrRe = regexp.MustCompile(`(?i)(\b[a-z0-9_\-]*(?:apikey|api_key|password|token|secret)=)"[^"]*"`)
	// URL query values: ?apikey=..., &X-Plex-Token=...
	urlParamRe = regexp.MustCompile(`(?i)((?:[?&]|&amp;|\\u0026)(?:apikey|api_key|x-plex-token|token|access_token|sid|password)=)[^&"'\s<>\\]+`)
	// Grant secrets
	grantRe = regexp.MustCompile(`jitp_[A-Za-z0-9_\-]{16,}`)
)

// Scrub returns the body with every known secret and pattern match replaced,
// and the number of replacements.
func (s *Scrubber) Scrub(body []byte) ([]byte, int) {
	out, n := s.scrubString(string(body))
	return []byte(out), n
}

// ScrubString is Scrub for strings.
func (s *Scrubber) ScrubString(in string) string {
	out, _ := s.scrubString(in)
	return out
}

// KnownOnly applies only the known-secret pass (binary-safe; used for log
// lines and header values).
func (s *Scrubber) KnownOnly(in string) string {
	s.mu.RLock()
	r := s.replacer
	s.mu.RUnlock()
	return r.Replace(in)
}

func (s *Scrubber) scrubString(in string) (string, int) {
	s.mu.RLock()
	r := s.replacer
	pats := s.patterns
	s.mu.RUnlock()

	n := 0
	cur := in
	// Replacing can only shrink matches, but repeat until nothing changes in
	// case removing one encoding exposes another (e.g. overlapping copies).
	for i := 0; i < 4; i++ {
		next := r.Replace(cur)
		if next == cur {
			break
		}
		n += strings.Count(next, Marker) - strings.Count(cur, Marker)
		cur = next
	}
	for _, re := range []*regexp.Regexp{jsonFieldRe, servarrFieldRe} {
		cur = re.ReplaceAllStringFunc(cur, func(m string) string {
			sub := re.FindStringSubmatch(m)
			if strings.HasSuffix(m, `"`+Marker+`"`) || strings.HasSuffix(m, `""`) {
				return m
			}
			n++
			return sub[1] + `"` + Marker + `"`
		})
	}
	cur = xmlAttrRe.ReplaceAllStringFunc(cur, func(m string) string {
		sub := xmlAttrRe.FindStringSubmatch(m)
		if strings.HasSuffix(m, `=""`) {
			return m
		}
		n++
		return sub[1] + `"` + Marker + `"`
	})
	cur = urlParamRe.ReplaceAllStringFunc(cur, func(m string) string {
		sub := urlParamRe.FindStringSubmatch(m)
		n++
		return sub[1] + Marker
	})
	cur = grantRe.ReplaceAllStringFunc(cur, func(string) string {
		n++
		return Marker
	})
	// Belt and braces: if any known encoding survived, drop the body.
	if len(pats) > 0 && r.Replace(cur) != cur {
		return `{"error":"response withheld: secret could not be redacted"}`, n + 1
	}
	return cur, n
}
