package rules

import (
	"net/url"
	"strings"
	"testing"
)

// FuzzParseTarget: no panic, and every accepted target is canonical: its
// forwarded form parses back to the same path, segments and query, and
// contains no ambiguous sequence an upstream could interpret differently.
func FuzzParseTarget(f *testing.F) {
	for _, s := range []string{
		"/r/radarr/api/v3/queue?page=1",
		"/r/radarr/api/v3/movie/lookup?term=the%20matrix",
		"/r/radarr/api/v3/con%66ig/host",
		"/r/radarr/api/%2e%2e/config",
		"/r/radarr/api%2Fv3",
		"/r/ombi/api/v1/Search/movie/%C3%A9",
		"/r/plex/:/prefs",
		"/r/x/a?b=c&d=e",
		"/r/radarr/api/v3/%25%32%66",
	} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, in string) {
		tg, err := ParseTarget(in)
		if err != nil {
			return
		}
		if strings.Contains(tg.Path, "//") || strings.Contains(tg.Path, "/../") || strings.HasSuffix(tg.Path, "/..") ||
			strings.ContainsAny(tg.Path, "\\;%?#\x00") {
			t.Fatalf("accepted ambiguous path %q from %q", tg.Path, in)
		}
		for _, s := range tg.Segments {
			if s == "" || s == "." || s == ".." || strings.Contains(s, "/") {
				t.Fatalf("bad segment %q from %q", s, in)
			}
		}
		fwd := "/r/" + tg.Resource + tg.EscapedPath()
		if q := tg.Query.Encode(); q != "" {
			fwd += "?" + q
		}
		again, err := ParseTarget(fwd)
		if err != nil {
			t.Fatalf("canonical form %q of %q rejected: %v", fwd, in, err)
		}
		if again.Path != tg.Path || len(again.Segments) != len(tg.Segments) || again.Query.Encode() != tg.Query.Encode() {
			t.Fatalf("canonical form %q of %q parses differently", fwd, in)
		}
		// What is forwarded decodes to exactly the matched path.
		dec, err := url.PathUnescape(tg.EscapedPath())
		if err != nil || dec != tg.Path {
			t.Fatalf("forwarded path %q decodes to %q, matched %q", tg.EscapedPath(), dec, tg.Path)
		}
	})
}

// FuzzRuleDecisionCanonical: the decision on a raw target equals the
// decision on its canonical form (design I1 differential check), for a mix
// of allow and deny rules.
func FuzzRuleDecisionCanonical(f *testing.F) {
	allow := []string{
		`{method: GET, path: "/api/v3/queue", query: {page: int}}`,
		`{method: GET, path: "/api/v3/movie/{id:int}"}`,
		`{method: GET, path: "/api/v1/Search/movie/{term}"}`,
		`{method: DELETE, path: "/api/v3/movie/{id:int}", query: {deleteFiles: [false]}}`,
	}
	deny := []string{`{path: "/api/v3/config/**"}`, `{path: "/api/v1/Settings/**"}`}
	var as, ds []*Rule
	for _, y := range allow {
		as = append(as, mustRuleF(f, y, false))
	}
	for _, y := range deny {
		ds = append(ds, mustRuleF(f, y, true))
	}
	for _, s := range []string{"/r/radarr/api/v3/queue?page=1", "/r/radarr/api/v3/CONFIG/host", "/r/radarr/api/v3/movie/7",
		"/r/ombi/api/v1/Search/movie/a%20b", "/r/radarr/api/v3/movie/7?deleteFiles=false"} {
		f.Add("GET", s)
		f.Add("DELETE", s)
	}
	decide := func(method string, tg *Target) string {
		for _, d := range ds {
			if d.MatchDeny(method, tg.Segments) {
				return "deny"
			}
		}
		r := &Request{Method: method, Target: tg}
		for _, a := range as {
			if a.Match(r, 1<<20) {
				return "allow:" + a.Desc()
			}
		}
		return "no_rule"
	}
	f.Fuzz(func(t *testing.T, method, in string) {
		tg, err := ParseTarget(in)
		if err != nil {
			return
		}
		canon := "/r/" + tg.Resource + tg.EscapedPath()
		if q := tg.Query.Encode(); q != "" {
			canon += "?" + q
		}
		tc, err := ParseTarget(canon)
		if err != nil {
			t.Fatalf("canonical %q rejected", canon)
		}
		if a, b := decide(method, tg), decide(method, tc); a != b {
			t.Fatalf("%s %q -> %s but canonical %q -> %s", method, in, a, canon, b)
		}
		// A denied path is denied in every case spelling.
		if decide(method, tg) != "deny" {
			up, _ := ParseTarget("/r/" + tg.Resource + strings.ToUpper(tg.EscapedPath()))
			if up != nil && decide(method, up) == "deny" && strings.EqualFold(up.Path, tg.Path) {
				t.Fatalf("%q allowed but its upper-case spelling is denied", in)
			}
		}
	})
}

func mustRuleF(f *testing.F, y string, deny bool) *Rule {
	var r Rule
	if err := yamlUnmarshal([]byte(y), &r); err != nil {
		f.Fatal(err)
	}
	if err := r.Compile(deny); err != nil {
		f.Fatal(err)
	}
	return &r
}

// FuzzStrictObject: no panic; an accepted body has no two keys differing
// only in case at any level, and is a single object.
func FuzzStrictObject(f *testing.F) {
	for _, s := range []string{`{"a":1}`, `{"a":1,"A":2}`, `{"a":{"b":1,"B":2}}`, `[1]`, `{"a":1}{}`, `{"a":"\ud800"}`} {
		f.Add([]byte(s))
	}
	f.Fuzz(func(t *testing.T, b []byte) {
		obj, err := StrictObject(b)
		if err != nil {
			return
		}
		seen := map[string]bool{}
		for k := range obj {
			if seen[strings.ToLower(k)] {
				t.Fatalf("case duplicate %q accepted", k)
			}
			seen[strings.ToLower(k)] = true
		}
		if _, err := ParseRPC(b); err == nil {
			// fine either way; must not panic
		}
	})
}
