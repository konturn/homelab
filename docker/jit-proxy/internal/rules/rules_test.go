package rules

import (
	"errors"
	"testing"

	"gopkg.in/yaml.v3"
)

func TestParseTargetAccepts(t *testing.T) {
	cases := []struct {
		in, res, path string
		segs          int
	}{
		{"/r/radarr/api/v3/queue", "radarr", "/api/v3/queue", 3},
		{"/r/radarr", "radarr", "/", 0},
		{"/r/radarr/", "radarr", "/", 0},
		{"/r/radarr/api/v3/movie/lookup?term=the%20matrix", "radarr", "/api/v3/movie/lookup", 4},
		{"/r/ombi/api/v1/Search/movie/the%20matrix", "ombi", "/api/v1/Search/movie/the matrix", 5},
		{"/r/plex/:/prefs", "plex", "/:/prefs", 2},
		{"/r/radarr/api/v3/con%66ig", "radarr", "/api/v3/config", 3},
	}
	for _, c := range cases {
		tg, err := ParseTarget(c.in)
		if err != nil {
			t.Errorf("%s: %v", c.in, err)
			continue
		}
		if tg.Resource != c.res || tg.Path != c.path || len(tg.Segments) != c.segs {
			t.Errorf("%s: got %q %q %d", c.in, tg.Resource, tg.Path, len(tg.Segments))
		}
	}
}

func TestParseTargetRejects(t *testing.T) {
	for _, in := range []string{
		"",
		"*",
		"http://radarr/api",
		"/api/v3/queue",               // outside /r/
		"/r/Radarr/api",               // resource case
		"/r/radarr/api//v3",           // empty segment
		"/r/radarr/api/v3/",           // trailing slash
		"/r/radarr/api/../config",     // dot segment
		"/r/radarr/api/./config",      // dot segment
		"/r/radarr/api/%2e%2e/config", // encoded dot segment
		"/r/radarr/api%2Fv3/config",   // encoded slash
		"/r/radarr/api/v3%2F%2Fconfig",
		"/r/radarr/api/v3/config%5chost", // backslash
		"/r/radarr/api/v3;x=1/config",    // path parameter
		"/r/radarr/api/v3/c%3bonfig",
		"/r/radarr/api/v3/%2566onfig", // double encoding
		"/r/radarr/api/v3/%00",
		"/r/radarr/api/v3/%zz",
		"/r/radarr/api/v3/queue#frag",
		"/r/radarr/api/v3/queue?a=1&a=2", // duplicate parameter
		"/r/radarr/api/v3/queue?a=1&A=2", // case duplicate
		"/r/radarr/api/v3/queue?a=1;b=2", // semicolon separator
		"/r/radarr/api/v3/queue?=1",      // empty key
		"/r/radarr/api/v3/queue?a=%zz",
		"/r/radarr/api/v3/%E2%80%8Bconfig", // zero-width space
		"/r/radarr/api/v3/%20config",
		"/r/radarr/api/v3/\x7fconfig",
	} {
		if _, err := ParseTarget(in); err == nil {
			t.Errorf("%q accepted", in)
		} else if !errors.Is(err, ErrNormalisation) {
			t.Errorf("%q: unexpected error type %v", in, err)
		}
	}
}

func mustRule(t *testing.T, y string, deny bool) *Rule {
	t.Helper()
	var r Rule
	if err := yaml.Unmarshal([]byte(y), &r); err != nil {
		t.Fatal(err)
	}
	if err := r.Compile(deny); err != nil {
		t.Fatal(err)
	}
	return &r
}

func req(t *testing.T, method, target, body string) *Request {
	t.Helper()
	tg, err := ParseTarget(target)
	if err != nil {
		t.Fatalf("%s: %v", target, err)
	}
	return &Request{Method: method, Target: tg, Body: []byte(body)}
}

func TestRuleMatch(t *testing.T) {
	queue := mustRule(t, `{method: GET, path: "/api/v3/queue", query: {page: int, pageSize: {type: int, min: 1, max: 500}}}`, false)
	movie := mustRule(t, `{method: GET, path: "/api/v3/movie", query: {tmdbId: int}, require: [tmdbId]}`, false)
	byID := mustRule(t, `{method: GET, path: "/api/v3/movie/{id:int}"}`, false)
	cmd := mustRule(t, `{method: POST, path: "/api/v3/command", json: {name: [MoviesSearch]}, body_max: 100}`, false)
	del := mustRule(t, `{method: DELETE, path: "/api/v3/movie/{id:int}", query: {deleteFiles: [false]}}`, false)
	rpc := mustRule(t, `{method: POST, path: "/json", rpc: {methods: [core.remove_torrent], params: {1: [false]}}}`, false)
	enum := mustRule(t, `{method: GET, path: "/api/domains/{type:allow|deny}"}`, false)

	cases := []struct {
		r    *Rule
		q    *Request
		want bool
	}{
		{queue, req(t, "GET", "/r/radarr/api/v3/queue", ""), true},
		{queue, req(t, "GET", "/r/radarr/api/v3/queue?page=2&pageSize=50", ""), true},
		{queue, req(t, "GET", "/r/radarr/api/v3/queue?pageSize=501", ""), false},
		{queue, req(t, "GET", "/r/radarr/api/v3/queue?page=x", ""), false},
		{queue, req(t, "GET", "/r/radarr/api/v3/queue?other=1", ""), false},
		{queue, req(t, "POST", "/r/radarr/api/v3/queue", ""), false},
		{queue, req(t, "GET", "/r/radarr/api/v3/queue", "body"), false},
		{queue, req(t, "GET", "/r/radarr/API/V3/QUEUE", ""), true},  // case-insensitive literal
		{movie, req(t, "GET", "/r/radarr/api/v3/movie", ""), false}, // required parameter
		{movie, req(t, "GET", "/r/radarr/api/v3/movie?tmdbId=603", ""), true},
		{byID, req(t, "GET", "/r/radarr/api/v3/movie/12", ""), true},
		{byID, req(t, "GET", "/r/radarr/api/v3/movie/x", ""), false},
		{byID, req(t, "GET", "/r/radarr/api/v3/movie/12/extra", ""), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","movieIds":[1]}`), true},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"Shutdown"}`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"Name":"MoviesSearch"}`), true},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","Name":"Shutdown"}`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","x":{"a":1,"A":2}}`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch"} {}`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `[{"name":"MoviesSearch"}]`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":["MoviesSearch"]}`), false},
		{cmd, req(t, "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","pad":"`+string(make([]byte, 0))+`xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}`), false}, // body_max
		{del, req(t, "DELETE", "/r/radarr/api/v3/movie/5", ""), true},
		{del, req(t, "DELETE", "/r/radarr/api/v3/movie/5?deleteFiles=false", ""), true},
		{del, req(t, "DELETE", "/r/radarr/api/v3/movie/5?deleteFiles=true", ""), false},
		{del, req(t, "DELETE", "/r/radarr/api/v3/movie/5?deleteFiles=False", ""), false},
		{rpc, req(t, "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abc",false],"id":1}`), true},
		{rpc, req(t, "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abc",true],"id":1}`), false},
		{rpc, req(t, "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abc","false"],"id":1}`), false},
		{rpc, req(t, "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abc"],"id":1}`), false},
		{rpc, req(t, "POST", "/r/deluge/json", `{"method":"core.remove_torrent","Method":"daemon.shutdown","params":["abc",false]}`), false},
		{rpc, req(t, "POST", "/r/deluge/json", `[{"method":"core.remove_torrent","params":["abc",false]}]`), false},
		{enum, req(t, "GET", "/r/pihole/api/domains/deny", ""), true},
		{enum, req(t, "GET", "/r/pihole/api/domains/other", ""), false},
	}
	for i, c := range cases {
		if got := c.r.Match(c.q, 1<<20); got != c.want {
			t.Errorf("case %d (%s %s %q): got %v want %v", i, c.q.Method, c.q.Target.Path, c.q.Body, got, c.want)
		}
	}
}

func TestDenyRules(t *testing.T) {
	d := mustRule(t, `{path: "/api/v3/config/**"}`, true)
	for _, p := range []string{"/r/radarr/api/v3/config", "/r/radarr/api/v3/config/host", "/r/radarr/API/v3/Config/host", "/r/radarr/api/v3/con%66ig/host"} {
		if !d.MatchDeny("GET", req(t, "GET", p, "").Target.Segments) {
			t.Errorf("%s not denied", p)
		}
	}
	if d.MatchDeny("GET", req(t, "GET", "/r/radarr/api/v3/configx", "").Target.Segments) {
		t.Error("configx denied")
	}
	m := mustRule(t, `{path: "/api/v3/qualityprofile/**", method: PUT}`, true)
	if m.MatchDeny("GET", req(t, "GET", "/r/radarr/api/v3/qualityprofile/1", "").Target.Segments) {
		t.Error("GET denied by a PUT rule")
	}
}

func TestCompileRejects(t *testing.T) {
	for _, y := range []string{
		`{method: GET, path: "api/v3"}`,
		`{method: TRACE, path: "/x"}`,
		`{path: "/x"}`,
		`{method: GET, path: "/x/**"}`,
		`{method: GET, path: "/x", query: {apikey: any}}`,
		`{method: GET, path: "/x", query: {ApiKey: any}}`,
		`{method: GET, path: "/x", query: {X-Plex-Token: any}}`,
		`{method: GET, path: "/x", require: [a]}`,
		`{method: GET, path: "/x", json: {a: int}}`,
		`{method: GET, path: "/x/{id:float}"}`,
		`{method: POST, path: "/x", json: {a: int}, rpc: {methods: [m]}}`,
	} {
		var r Rule
		if err := yaml.Unmarshal([]byte(y), &r); err != nil {
			continue
		}
		if err := r.Compile(false); err == nil {
			t.Errorf("compiled: %s", y)
		}
	}
}

func TestEscapedPathRoundTrip(t *testing.T) {
	tg, err := ParseTarget("/r/ombi/api/v1/Search/movie/the%20matrix%20%C3%A9")
	if err != nil {
		t.Fatal(err)
	}
	if got := tg.EscapedPath(); got != "/api/v1/Search/movie/the%20matrix%20%C3%A9" {
		t.Errorf("escaped path %q", got)
	}
}

func yamlUnmarshal(b []byte, v interface{}) error { return yaml.Unmarshal(b, v) }
