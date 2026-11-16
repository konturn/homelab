package redact

import (
	"encoding/base64"
	"encoding/json"
	"net/url"
	"strings"
	"testing"
)

var testSecrets = []string{
	"0123456789abcdef0123456789abcdef", // Servarr-style hex key
	"Pl3x-T0ken_xyz/+=",                // URL/JSON-special characters
	"hunter2&<b>\"q\"",                 // HTML/JSON escapes
	"s3ss10n-1d-v4lu3",
}

// encodings of s that an upstream might put in a response.
func encodings(s string) []string {
	j, _ := json.Marshal(s)
	return []string{
		s,
		url.QueryEscape(s),
		url.PathEscape(s),
		string(j[1 : len(j)-1]),
		base64.StdEncoding.EncodeToString([]byte(s)),
		base64.URLEncoding.EncodeToString([]byte(s)),
		base64.StdEncoding.EncodeToString([]byte("user:" + s)),    // basic auth
		base64.StdEncoding.EncodeToString([]byte("ab" + s + "c")), // other alignment
		strings.ToUpper(s),
	}
}

func scrubberWith(secrets ...string) *Scrubber {
	s := New()
	for i, sec := range secrets {
		s.Set(string(rune('a'+i)), sec)
	}
	return s
}

func TestKnownSecretsAllEncodings(t *testing.T) {
	s := scrubberWith(testSecrets...)
	for _, sec := range testSecrets {
		for _, enc := range encodings(sec) {
			if enc == strings.ToUpper(sec) && !isHexish(sec) {
				continue
			}
			body := `{"x":"prefix ` + enc + ` suffix"}`
			out, n := s.Scrub([]byte(body))
			if n == 0 || leaks(string(out), sec) || !strings.Contains(string(out), "prefix") {
				t.Errorf("secret %q in encoding %q not redacted: %s", sec, enc, out)
			}
		}
	}
}

// leaks reports whether any variant the scrubber looks for is still present.
func leaks(out, secret string) bool {
	for _, v := range Variants(secret) {
		if strings.Contains(out, v) {
			return true
		}
	}
	return false
}

func TestPatterns(t *testing.T) {
	s := New()
	cases := map[string]string{
		`{"apiKey":"abc123def456"}`: `{"apiKey":"[REDACTED]"}`,
		`{"password": "pw"}`:        `{"password": "[REDACTED]"}`,
		`{"accessToken":"t"}`:       `{"accessToken":"[REDACTED]"}`,
		`{"fields":[{"name":"apiKey","label":"API Key","value":"zzz"}]}`: `{"fields":[{"name":"apiKey","label":"API Key","value":"[REDACTED]"}]}`,
		`<Server token="abc" name="x"/>`:                                 `<Server token="[REDACTED]" name="x"/>`,
		`see http://h/api?apikey=deadbeef&x=1`:                           `see http://h/api?apikey=[REDACTED]&x=1`,
		`url /x?X-Plex-Token=abcdef`:                                     `url /x?X-Plex-Token=[REDACTED]`,
		`grant jitp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA end`:     `grant [REDACTED] end`,
		`{"name":"baseUrl","value":"http://x"}`:                          `{"name":"baseUrl","value":"http://x"}`,
		`{"title":"The Matrix","year":1999}`:                             `{"title":"The Matrix","year":1999}`,
	}
	for in, want := range cases {
		out, _ := s.Scrub([]byte(in))
		if string(out) != want {
			t.Errorf("%s\n got %s\nwant %s", in, out, want)
		}
	}
}

func TestVariantsShortSecretBase64(t *testing.T) {
	// base64 cores shorter than minB64Core are not searched (false
	// positives), but raw and escaped forms always are.
	v := Variants("abcdef")
	found := false
	for _, x := range v {
		if x == "abcdef" {
			found = true
		}
	}
	if !found {
		t.Fatal("raw form missing")
	}
}

// FuzzScrub (I2): whatever surrounds them, held secrets in any encoding and
// grant secrets never survive scrubbing.
func FuzzScrub(f *testing.F) {
	f.Add([]byte(`{"a":"b"}`), uint8(0), uint8(0), uint16(3))
	f.Add([]byte(`<x y="z"/>`), uint8(1), uint8(4), uint16(0))
	f.Add([]byte("plain text"), uint8(2), uint8(6), uint16(5))
	f.Add([]byte(""), uint8(3), uint8(7), uint16(0))
	s := scrubberWith(testSecrets...)
	f.Fuzz(func(t *testing.T, body []byte, which, enc uint8, pos uint16) {
		sec := testSecrets[int(which)%len(testSecrets)]
		encs := encodings(sec)
		e := encs[int(enc)%len(encs)]
		p := int(pos) % (len(body) + 1)
		in := string(body[:p]) + e + string(body[p:]) + " jitp_" + strings.Repeat("Q", 43)
		out, _ := s.Scrub([]byte(in))
		for _, x := range testSecrets {
			if leaks(string(out), x) {
				t.Fatalf("secret leaked: in %q out %q", in, out)
			}
		}
		if strings.Contains(string(out), "jitp_"+strings.Repeat("Q", 43)) {
			t.Fatalf("grant secret leaked: %q", out)
		}
	})
}

func TestShortValuesIgnored(t *testing.T) {
	s := New()
	s.Set("x", "s", "ab")
	if out, n := s.Scrub([]byte(`{"status":"ok"}`)); n != 0 || string(out) != `{"status":"ok"}` {
		t.Fatalf("short value redacted: %s", out)
	}
}
