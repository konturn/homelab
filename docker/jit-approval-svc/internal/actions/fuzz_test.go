package actions

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// FuzzHandleCallback: arbitrary callback data never panics, and a decision
// is recorded only for exactly "act:<button action>:<pending prompt id>".
//
//	go test ./internal/actions -run '^$' -fuzz '^FuzzHandleCallback$' -fuzztime 1m
func FuzzHandleCallback(f *testing.F) {
	for _, s := range []string{
		"act:quarantine:canary-01", "act:ignore:canary-01", "act:delete:canary-01",
		"act:quarantine:canary-02", "act:quarantine:", "act::canary-01", "act:", "jit:approve:req-1",
		"act:quarantine:canary-01:x", "ACT:quarantine:canary-01", "act:Quarantine:canary-01",
		"act:quarantine:canary-01 ", "act:quarantine:cаnary-01",
	} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data string) {
		s, _, _ := newSvc()
		if _, err := s.Create("canary-01", "Canary tripped", qButtons, 0); err != nil {
			t.Fatal(err)
		}
		handled := s.HandleCallback("cb", data)
		if handled != strings.HasPrefix(data, CallbackPrefix) {
			t.Fatalf("HandleCallback(%q) handled=%v", data, handled)
		}
		got := s.Decisions(0)
		valid := data == "act:quarantine:canary-01" || data == "act:ignore:canary-01"
		if valid != (len(got) == 1) {
			t.Fatalf("HandleCallback(%q) recorded %v", data, got)
		}
		if len(got) == 1 && "act:"+got[0].Action+":"+got[0].ID != data {
			t.Fatalf("decision %+v for data %q", got[0], data)
		}
	})
}

// FuzzPromptAPI: arbitrary bodies on the socket API never panic or 5xx
// (other than 502/503 from the messenger or capacity), and only valid
// prompts reach Telegram.
func FuzzPromptAPI(f *testing.F) {
	f.Add([]byte(`{"id":"canary-01","text":"hi","buttons":[{"action":"ok","label":"OK"}]}`), false)
	f.Add([]byte(`{"id":"Canary","text":"hi"}`), false)
	f.Add([]byte(`{"id":"canary-01","text":"   "}`), false)
	f.Add([]byte(`{"id":"canary-01","text":"hi","buttons":[{"action":"ok","label":"OK"},{"action":"ok","label":"OK"}]}`), false)
	f.Add([]byte(`{"id":"canary-01","text":"hi","ttl_seconds":-9223372036854775808}`), false)
	f.Add([]byte(`{"text":"hi"}`), true)
	f.Add([]byte(`not json`), false)
	f.Fuzz(func(t *testing.T, body []byte, notify bool) {
		s, fm, _ := newSvc()
		path := "/v1/prompts"
		if notify {
			path = "/v1/notify"
		}
		req := httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, req)
		if w.Code >= 500 && w.Code != http.StatusBadGateway && w.Code != http.StatusServiceUnavailable {
			t.Fatalf("status %d: %s", w.Code, w.Body)
		}
		fm.mu.Lock()
		n := len(fm.sent)
		var text string
		if n > 0 {
			text = fm.sent[0].text
		}
		fm.mu.Unlock()
		if (w.Code == http.StatusCreated) != (n == 1) {
			t.Fatalf("status %d but %d messages sent", w.Code, n)
		}
		if n == 1 && (strings.TrimSpace(text) == "" || len(text) > maxText) {
			t.Fatalf("sent invalid text of %d bytes", len(text))
		}
		for _, p := range s.prompts {
			if !idRE.MatchString(p.id) || validateButtons(p.buttons) != nil {
				t.Fatalf("stored invalid prompt %+v", p)
			}
			if d := p.expires.Sub(p.created); d <= 0 || d > maxTTL {
				t.Fatalf("prompt ttl %v outside (0, %v]", d, maxTTL)
			}
		}
	})
}
