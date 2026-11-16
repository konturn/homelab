// Package rules normalises agent requests and matches them against the
// method/path/query/body rules of a grant's templates.
//
// The request target is normalised once and then either rejected or matched;
// it is never "fixed". The proxy forwards exactly the canonical form that was
// matched, so a rule decision can never differ from what the upstream sees.
package rules

import (
	"errors"
	"fmt"
	"net/url"
	"strings"
	"unicode"
	"unicode/utf8"
)

// ErrNormalisation is returned for any request target the proxy refuses to
// interpret (ambiguous encodings, dot segments, path parameters, ...).
var ErrNormalisation = errors.New("normalisation")

func normErr(format string, a ...interface{}) error {
	return fmt.Errorf("%w: %s", ErrNormalisation, fmt.Sprintf(format, a...))
}

// Target is a parsed, canonical request target.
type Target struct {
	Resource string     // from /r/<resource>/...
	Path     string     // canonical, decoded path ("/" for the root)
	Segments []string   // decoded path segments (empty for "/")
	Query    url.Values // decoded query, at most one value per key
}

// EscapedPath is the canonical path as it is forwarded upstream.
func (t *Target) EscapedPath() string {
	if len(t.Segments) == 0 {
		return "/"
	}
	parts := make([]string, len(t.Segments))
	for i, s := range t.Segments {
		parts[i] = url.PathEscape(s)
	}
	return "/" + strings.Join(parts, "/")
}

// QueryKeys returns the query parameter names, sorted (for audit logs).
func (t *Target) QueryKeys() []string {
	keys := make([]string, 0, len(t.Query))
	for k := range t.Query {
		keys = append(keys, k)
	}
	sortStrings(keys)
	return keys
}

// CredentialParams are query parameters that carry credentials for one of
// the proxied services. An agent request carrying any of them (in any case)
// is refused, and no template may allow them.
var CredentialParams = []string{
	"apikey", "api_key", "x-plex-token", "token", "access_token", "sid",
	"password", "pass", "passwd", "auth", "session", "_session_id",
}

// IsCredentialParam reports whether a query parameter name is a credential.
func IsCredentialParam(name string) bool {
	for _, c := range CredentialParams {
		if strings.EqualFold(name, c) {
			return true
		}
	}
	return false
}

// ParseTarget parses the raw request target (http.Request.RequestURI) of an
// agent request: /r/<resource>/<upstream path>?<query>.
func ParseTarget(requestURI string) (*Target, error) {
	if requestURI == "" || requestURI[0] != '/' {
		return nil, normErr("not an origin-form target")
	}
	for i := 0; i < len(requestURI); i++ {
		c := requestURI[i]
		if c < 0x21 || c > 0x7e {
			return nil, normErr("raw byte 0x%02x in target", c)
		}
	}
	if strings.ContainsRune(requestURI, '#') {
		return nil, normErr("fragment in target")
	}
	rawPath, rawQuery, _ := strings.Cut(requestURI, "?")

	rest, ok := strings.CutPrefix(rawPath, "/r/")
	if !ok {
		return nil, normErr("target outside /r/")
	}
	resource, tail, hasTail := strings.Cut(rest, "/")
	if !validResource(resource) {
		return nil, normErr("invalid resource name")
	}
	t := &Target{Resource: resource}
	if hasTail {
		tail = "/" + tail
	} else {
		tail = "/"
	}

	path, segs, err := canonicalPath(tail)
	if err != nil {
		return nil, err
	}
	t.Path, t.Segments = path, segs

	q, err := parseQuery(rawQuery)
	if err != nil {
		return nil, err
	}
	t.Query = q
	return t, nil
}

func validResource(s string) bool {
	if s == "" || len(s) > 32 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return false
		}
	}
	return true
}

// canonicalPath decodes the raw path exactly once and rejects anything whose
// meaning could differ between the proxy and an upstream.
func canonicalPath(raw string) (string, []string, error) {
	if strings.Contains(raw, "//") {
		return "", nil, normErr("empty path segment")
	}
	decoded, err := url.PathUnescape(raw)
	if err != nil {
		return "", nil, normErr("bad percent-encoding")
	}
	if !utf8.ValidString(decoded) {
		return "", nil, normErr("invalid UTF-8")
	}
	for _, r := range decoded {
		switch {
		case r == '\\', r == ';', r == '%', r == '?', r == '#', r == 0:
			return "", nil, normErr("forbidden character %q after decoding", r)
		case !unicode.IsPrint(r):
			return "", nil, normErr("non-printable character after decoding")
		}
	}
	if strings.Contains(decoded, "//") {
		return "", nil, normErr("encoded slash produces an empty segment")
	}
	if decoded == "/" {
		return "/", nil, nil
	}
	segs := strings.Split(decoded[1:], "/")
	for _, s := range segs {
		switch s {
		case "":
			return "", nil, normErr("trailing slash")
		case ".", "..":
			return "", nil, normErr("dot segment")
		}
		if strings.TrimSpace(s) != s {
			return "", nil, normErr("leading or trailing space in segment")
		}
	}
	// An encoded "/" inside a segment ("%2F") decodes to a slash and would
	// change the segment structure; it is caught here because the decoded
	// path is split again and must re-encode to an equivalent raw path.
	if rawSegs := strings.Split(raw[1:], "/"); len(rawSegs) != len(segs) {
		return "", nil, normErr("encoded slash")
	}
	return decoded, segs, nil
}

func parseQuery(raw string) (url.Values, error) {
	if raw == "" {
		return url.Values{}, nil
	}
	q, err := url.ParseQuery(raw)
	if err != nil {
		return nil, normErr("bad query")
	}
	seenFold := map[string]bool{}
	for k, vs := range q {
		if k == "" {
			return nil, normErr("empty query key")
		}
		if len(vs) != 1 {
			return nil, normErr("duplicate query parameter")
		}
		lk := strings.ToLower(k)
		if seenFold[lk] {
			return nil, normErr("query parameters differing only in case")
		}
		seenFold[lk] = true
		if !utf8.ValidString(k) || !utf8.ValidString(vs[0]) {
			return nil, normErr("invalid UTF-8 in query")
		}
		for _, r := range k + vs[0] {
			if r == 0 || (!unicode.IsPrint(r) && r != ' ') {
				return nil, normErr("control character in query")
			}
		}
	}
	return q, nil
}

func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j] < s[j-1]; j-- {
			s[j], s[j-1] = s[j-1], s[j]
		}
	}
}
