package templates

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/nkontur/jit-proxy/internal/rules"
)

func loadReal(t *testing.T) Set {
	t.Helper()
	set, err := Load(os.DirFS("../../templates"), ".")
	if err != nil {
		t.Fatal(err)
	}
	return set
}

// decide returns "deny", "allow:<template>" or "no_rule" for a request under
// a grant holding the given templates.
func decide(t *testing.T, r *Resource, tmpl []string, method, target, body string) string {
	t.Helper()
	tg, err := rules.ParseTarget(target)
	if err != nil {
		return "normalisation"
	}
	for _, d := range r.Deny {
		if d.MatchDeny(method, tg.Segments) {
			return "deny"
		}
	}
	g, err := r.Resolve(tmpl)
	if err != nil {
		t.Fatal(err)
	}
	req := &rules.Request{Method: method, Target: tg, Body: []byte(body)}
	for _, rule := range g.Rules {
		if rule.Match(req, r.BodyMax) {
			return "allow:" + rule.Template
		}
	}
	return "no_rule"
}

func TestRealTemplatesLoad(t *testing.T) {
	set := loadReal(t)
	want := []string{"bluebubbles", "deluge", "nzbget", "ombi", "pihole", "plex", "prowlarr", "radarr", "sonarr", "tautulli"}
	if got := strings.Join(set.Names(), ","); got != strings.Join(want, ",") {
		t.Fatalf("resources %s", got)
	}
	for name, r := range set {
		if r.UpstreamURL.Scheme != "https" && !r.PlainOK {
			t.Errorf("%s: plain upstream", name)
		}
		if _, ok := r.Tmpl["read"]; !ok {
			t.Errorf("%s: no read template", name)
		}
	}
}

// Tiers: never lower than the broker's resource tier (resourceTier in
// docker/jit-approval-svc/internal/vault/vault.go), and removals of titles or
// data need tier 2.
func TestTiers(t *testing.T) {
	set := loadReal(t)
	brokerTier := map[string]int{"radarr": 1, "sonarr": 1, "prowlarr": 1, "plex": 1, "ombi": 1, "nzbget": 1, "deluge": 1, "tautulli": 1, "pihole": 2, "bluebubbles": 1}
	for name, r := range set {
		if r.MinTier < brokerTier[name] {
			t.Errorf("%s: min_tier %d below the broker's %d", name, r.MinTier, brokerTier[name])
		}
	}
	for _, c := range []struct{ res, tmpl string }{{"radarr", "remove-title"}, {"sonarr", "remove-title"}, {"deluge", "remove-data"}} {
		if tier, _ := set[c.res].Tier([]string{c.tmpl}); tier < 2 {
			t.Errorf("%s/%s: tier %d", c.res, c.tmpl, tier)
		}
		if !set[c.res].Tmpl[c.tmpl].SingleUse {
			t.Errorf("%s/%s: not single-use", c.res, c.tmpl)
		}
	}
}

// The calls the OpenClaw skills make must be allowed by the template they use.
func TestGoldenSkillCalls(t *testing.T) {
	set := loadReal(t)
	cases := []struct {
		res, tmpl, method, target, body string
	}{
		{"radarr", "read", "GET", "/r/radarr/api/v3/movie/lookup?term=the%20matrix", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/movie/603", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/movie?tmdbId=603", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/movie/lookup/tmdb?tmdbId=603", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/queue?movieIds=12", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/rootfolder", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/qualityprofile", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/calendar?start=2026-11-16&end=2026-12-16", ""},
		{"radarr", "read", "GET", "/r/radarr/api/v3/history?pageSize=10", ""},
		{"radarr", "write", "POST", "/r/radarr/api/v3/movie", `{"title":"x","tmdbId":603,"qualityProfileId":1,"rootFolderPath":"/movies","monitored":true,"addOptions":{"searchForMovie":true}}`},
		{"radarr", "write", "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","movieIds":[12]}`},
		{"radarr", "write", "GET", "/r/radarr/api/v3/release?movieId=12", ""},
		{"radarr", "write", "POST", "/r/radarr/api/v3/release", `{"guid":"abc","indexerId":3,"movieId":12}`},
		{"radarr", "replace-file", "DELETE", "/r/radarr/api/v3/moviefile/77", ""},
		{"radarr", "health", "POST", "/r/radarr/api/v3/indexer/testall", "{}"},
		{"radarr", "health", "GET", "/r/radarr/api/v3/health", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/series/lookup?term=breaking%20bad", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/series?tvdbId=81189", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/series/lookup?term=tvdb%3A81189", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/episode?seriesId=4", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/episodefile/9", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/series/4", ""},
		{"sonarr", "read", "GET", "/r/sonarr/api/v3/queue?seriesIds=4", ""},
		{"sonarr", "write", "POST", "/r/sonarr/api/v3/series", `{"tvdbId":81189}`},
		{"sonarr", "write", "POST", "/r/sonarr/api/v3/command", `{"name":"EpisodeSearch","episodeIds":[1,2]}`},
		{"sonarr", "write", "GET", "/r/sonarr/api/v3/release?episodeId=456", ""},
		{"sonarr", "replace-file", "DELETE", "/r/sonarr/api/v3/episodefile/9", ""},
		{"plex", "read", "GET", "/r/plex/search?query=inception", ""},
		{"plex", "read", "GET", "/r/plex/library/sections", ""},
		{"plex", "read", "GET", "/r/plex/library/sections/1/all", ""},
		{"plex", "read", "GET", "/r/plex/library/metadata/123", ""},
		{"plex", "read", "GET", "/r/plex/status/sessions", ""},
		{"plex", "read", "GET", "/r/plex/library/onDeck", ""},
		{"plex", "refresh", "GET", "/r/plex/library/sections/1/refresh", ""},
		{"ombi", "read", "GET", "/r/ombi/api/v1/Request/count", ""},
		{"ombi", "read", "GET", "/r/ombi/api/v1/Request/movie?count=10&position=0", ""},
		{"ombi", "read", "GET", "/r/ombi/api/v1/Search/movie/inception", ""},
		{"ombi", "read", "GET", "/r/ombi/api/v1/Search/tv/breaking%20bad", ""},
		{"ombi", "requests", "POST", "/r/ombi/api/v1/Request/movie", `{"theMovieDbId":27205}`},
		{"ombi", "requests", "POST", "/r/ombi/api/v1/Request/movie/approve", `{"id":123}`},
		{"ombi", "requests", "PUT", "/r/ombi/api/v1/Request/movie/deny", `{"id":123,"reason":"have it"}`},
		{"tautulli", "read", "GET", "/r/tautulli/api/v2?cmd=get_activity", ""},
		{"tautulli", "read", "GET", "/r/tautulli/api/v2?cmd=get_history&length=10", ""},
		{"tautulli", "read", "GET", "/r/tautulli/api/v2?cmd=get_home_stats&stat_id=top_movies&stats_count=10", ""},
		{"nzbget", "read", "POST", "/r/nzbget/jsonrpc", `{"method":"status","id":1}`},
		{"nzbget", "read", "POST", "/r/nzbget/jsonrpc", `{"method":"listgroups","id":1}`},
		{"nzbget", "read", "POST", "/r/nzbget/jsonrpc", `{"method":"history","params":[false],"id":1}`},
		{"nzbget", "control", "POST", "/r/nzbget/jsonrpc", `{"method":"pausedownload","id":1}`},
		{"nzbget", "control", "POST", "/r/nzbget/jsonrpc", `{"method":"editqueue","params":["GroupPause","",[5]],"id":1}`},
		{"deluge", "read", "POST", "/r/deluge/json", `{"method":"core.get_torrents_status","params":[{},["name","state"]],"id":1}`},
		{"deluge", "control", "POST", "/r/deluge/json", `{"method":"core.pause_torrent","params":[["abcdef"]],"id":1}`},
		{"deluge", "remove", "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abcdef0123",false],"id":1}`},
		{"pihole", "read", "GET", "/r/pihole/api/stats/summary", ""},
		{"pihole", "read", "GET", "/r/pihole/api/dns/blocking", ""},
		{"pihole", "block-toggle", "POST", "/r/pihole/api/dns/blocking", `{"blocking":false,"timer":300}`},
		{"prowlarr", "read", "GET", "/r/prowlarr/api/v1/search?query=ubuntu&type=search", ""},
		{"prowlarr", "read", "GET", "/r/prowlarr/api/v1/indexerstats", ""},
		// skills/imessage/scripts: fetch-messages-bb.sh pages ASC from the checkpoint;
		// send-imessage-bb.sh looks up a group chat, then posts the text.
		{"bluebubbles", "read", "GET", "/r/bluebubbles/api/v1/ping", ""},
		{"bluebubbles", "read", "POST", "/r/bluebubbles/api/v1/message/query", `{"limit":100,"offset":0,"sort":"ASC","after":1793817715396,"with":["chat","chat.participants","attachment","messageSummaryInfo"]}`},
		{"bluebubbles", "read", "POST", "/r/bluebubbles/api/v1/message/query", `{"limit":100,"offset":300,"sort":"ASC","after":1793817715396,"with":["chat"]}`},
		{"bluebubbles", "read", "GET", "/r/bluebubbles/api/v1/chat/chat123456789012345", ""},
		{"bluebubbles", "send", "GET", "/r/bluebubbles/api/v1/chat/chat123456789012345", ""},
		{"bluebubbles", "send", "POST", "/r/bluebubbles/api/v1/message/text", `{"chatGuid":"iMessage;+;chat123456789012345","message":"line one\nline two","tempGuid":"temp-1-2","method":"private-api"}`},
		{"bluebubbles", "send", "POST", "/r/bluebubbles/api/v1/message/text", `{"chatGuid":"iMessage;+;chat1","message":"ok","tempGuid":"temp-1-2","method":"private-api","selectedMessageGuid":"p:0/ABCDEF01-2345-6789-ABCD-EF0123456789"}`},
	}
	for _, c := range cases {
		got := decide(t, set[c.res], []string{c.tmpl}, c.method, c.target, c.body)
		if !strings.HasPrefix(got, "allow:") {
			t.Errorf("%s/%s %s %s: %s", c.res, c.tmpl, c.method, c.target, got)
		}
	}
}

// Known-dangerous endpoints are refused under every template of the resource.
func TestGoldenDangerousDenied(t *testing.T) {
	set := loadReal(t)
	cases := []struct{ res, method, target, body string }{
		{"radarr", "GET", "/r/radarr/api/v3/config/host", ""},
		{"radarr", "GET", "/r/radarr/api/v3/Config/Host", ""},
		{"radarr", "PUT", "/r/radarr/api/v3/config/host", "{}"},
		{"radarr", "GET", "/r/radarr/api/v3/downloadclient", ""},
		{"radarr", "GET", "/r/radarr/api/v3/indexer", ""},
		{"radarr", "GET", "/r/radarr/api/v3/indexer/1", ""},
		{"radarr", "GET", "/r/radarr/api/v3/notification", ""},
		{"radarr", "POST", "/r/radarr/api/v3/system/shutdown", ""},
		{"radarr", "POST", "/r/radarr/api/v3/system/restart", ""},
		{"radarr", "POST", "/r/radarr/api/v3/command", `{"name":"Backup"}`},
		{"radarr", "POST", "/r/radarr/api/v3/command", `{"name":"ApplicationUpdate"}`},
		{"radarr", "POST", "/r/radarr/api/v3/command", `{"name":"MoviesSearch","Name":"Backup"}`},
		{"radarr", "GET", "/r/radarr/api/v3/movie", ""}, // whole library: too large, not needed
		{"radarr", "DELETE", "/r/radarr/api/v3/movie/1?deleteFiles=true", ""},
		{"radarr", "GET", "/r/radarr/api/v3/log/file/radarr.txt", ""},
		{"radarr", "GET", "/r/radarr/api/v3/queue?apikey=x", ""},
		{"sonarr", "GET", "/r/sonarr/api/v3/config/host", ""},
		{"sonarr", "DELETE", "/r/sonarr/api/v3/series/1?deleteFiles=true", ""},
		{"sonarr", "POST", "/r/sonarr/api/v3/command", `{"name":"MissingEpisodeSearch"}`},
		{"prowlarr", "GET", "/r/prowlarr/api/v1/indexer", ""},
		{"prowlarr", "GET", "/r/prowlarr/api/v1/applications", ""},
		{"plex", "GET", "/r/plex/:/prefs", ""},
		{"plex", "GET", "/r/plex/myplex/account", ""},
		{"plex", "GET", "/r/plex/security/token", ""},
		{"plex", "GET", "/r/plex/transcode/sessions", ""},
		{"plex", "GET", "/r/plex/library/metadata/1/thumb/2", ""},
		{"plex", "DELETE", "/r/plex/library/metadata/1", ""},
		{"ombi", "GET", "/r/ombi/api/v1/Settings/plex", ""},
		{"ombi", "GET", "/r/ombi/api/v1/settings/Plex", ""},
		{"ombi", "GET", "/r/ombi/api/v1/Identity/Users", ""},
		{"ombi", "GET", "/r/ombi/api/v1/Request/movie/search/x", ""},
		{"tautulli", "GET", "/r/tautulli/api/v2?cmd=get_settings", ""},
		{"tautulli", "GET", "/r/tautulli/api/v2?cmd=get_apikey", ""},
		{"tautulli", "GET", "/r/tautulli/api/v2?cmd=delete_all_library_history", ""},
		{"tautulli", "GET", "/r/tautulli/api/v2?cmd=get_activity&apikey=x", ""},
		{"tautulli", "GET", "/r/tautulli/api/v2", ""},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `{"method":"config","id":1}`},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `{"method":"saveconfig","params":[[]],"id":1}`},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `{"method":"append","params":["x.nzb","http://evil/x"],"id":1}`},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `{"method":"shutdown","id":1}`},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `[{"method":"status"},{"method":"shutdown"}]`},
		{"nzbget", "POST", "/r/nzbget/jsonrpc", `{"method":"history","params":[true],"id":1}`},
		{"nzbget", "GET", "/r/nzbget/jsonrpc/config", ""},
		{"nzbget", "POST", "/r/nzbget/xmlrpc", `<methodCall/>`},
		{"deluge", "POST", "/r/deluge/json", `{"method":"auth.login","params":["x"],"id":1}`},
		{"deluge", "POST", "/r/deluge/json", `{"method":"core.add_torrent_url","params":["http://evil/x",{}],"id":1}`},
		{"deluge", "POST", "/r/deluge/json", `{"method":"core.set_config","params":[{}],"id":1}`},
		{"deluge", "POST", "/r/deluge/json", `{"method":"web.connect","params":["x"],"id":1}`},
		{"deluge", "POST", "/r/deluge/json", `{"method":"core.remove_torrent","params":["abc",true],"id":1}`},
		{"pihole", "GET", "/r/pihole/api/config", ""},
		{"pihole", "GET", "/r/pihole/api/teleporter", ""},
		{"pihole", "POST", "/r/pihole/api/auth", `{"password":"x"}`},
		{"pihole", "POST", "/r/pihole/api/action/restartdns", ""},
		{"pihole", "POST", "/r/pihole/api/dns/blocking", `{"blocking":false}`},
		{"pihole", "POST", "/r/pihole/api/dns/blocking", `{"blocking":false,"timer":null}`},
		{"pihole", "POST", "/r/pihole/api/dns/blocking", `{"blocking":false,"timer":86400}`},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/server/info", ""},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/server/logs", ""},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/mac/restart", ""},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/settings", ""},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/findmy/devices", ""},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/icloud/contact", ""},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/attachment/abc/download", ""},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/abc/unsend", `{"partIndex":0}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/abc/edit", `{"editedMessage":"x"}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/multipart", `{"chatGuid":"x","parts":[]}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/react", `{"chatGuid":"x","selectedMessageGuid":"y","reaction":"love"}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/chat/new", `{"addresses":["x"],"message":"hi"}`},
		{"bluebubbles", "DELETE", "/r/bluebubbles/api/v1/chat/abc", ""},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/chat/abc/participant/add", `{"address":"x"}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/query?password=x", `{"limit":10}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/query", `{"limit":5000}`},
		{"bluebubbles", "POST", "/r/bluebubbles/api/v1/message/text", `{"chatGuid":"x","message":"hi","tempGuid":"t","method":"private-api","Method":"apple-script"}`},
		{"bluebubbles", "GET", "/r/bluebubbles/api/v1/handle/query", ""},
	}
	for _, c := range cases {
		r := set[c.res]
		var all []string
		for n := range r.Tmpl {
			// remove-data and remove-title are allowed, at tier 2, by design.
			if n == "remove-data" || n == "remove-title" {
				continue
			}
			all = append(all, n)
		}
		if got := decide(t, r, all, c.method, c.target, c.body); strings.HasPrefix(got, "allow:") {
			t.Errorf("%s %s %s %s: %s", c.res, c.method, c.target, c.body, got)
		}
	}
}

func TestParseRejects(t *testing.T) {
	base := "resource: x\nmin_tier: 1\nauth: {type: header, name: H, vault_path: homelab/data/x, field: f}\n" +
		"templates:\n  read:\n    min_tier: 1\n    description: d\n    allow:\n      - {method: GET, path: /a}\n"
	for name, y := range map[string]string{
		"plain http to a host name": "upstream: http://radarr.lab.example\nplain_http_on_internal: true\n" + base,
		"plain http without opt-in": "upstream: http://plex:32400\n" + base,
		"upstream with path":        "upstream: https://x.example/api\n" + base,
		"upstream with userinfo":    "upstream: https://u:p@x.example\n" + base,
		"unknown field":             "upstream: https://x.example\nextra: 1\n" + base,
		"vault path outside kv":     strings.Replace("upstream: https://x.example\n"+base, "homelab/data/x", "sys/policy", 1),
	} {
		if _, err := Parse([]byte(y)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := Parse([]byte("upstream: https://x.example\n" + base)); err != nil {
		t.Fatalf("base invalid: %v", err)
	}
}

// The proxy's Vault policy (local.jit_proxy_paths in
// terraform/vault/policies.tf) grants read on exactly the paths the
// templates name: nothing missing, nothing extra.
func TestVaultPolicyMatchesTemplates(t *testing.T) {
	src, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "terraform", "vault", "policies.tf"))
	if err != nil {
		t.Skipf("policies.tf not available: %v", err)
	}
	m := regexp.MustCompile(`(?s)jit_proxy_paths\s*=\s*\[(.*?)\]`).FindSubmatch(src)
	if m == nil {
		t.Fatal("local.jit_proxy_paths not found in policies.tf")
	}
	tf := map[string]bool{}
	for _, p := range regexp.MustCompile(`"([^"]+)"`).FindAllSubmatch(m[1], -1) {
		tf[string(p[1])] = true
	}
	want := map[string]bool{}
	for _, r := range loadReal(t) {
		want[r.Auth.VaultPath] = true
	}
	for p := range want {
		if !tf[p] {
			t.Errorf("template path %s missing from the jit-proxy policy", p)
		}
	}
	for p := range tf {
		if !want[p] {
			t.Errorf("jit-proxy policy grants %s, which no template uses", p)
		}
	}
}
