package actions

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/telegram"
)

type sent struct {
	id   int
	text string
	rows [][]telegram.InlineButton
}

type fakeMessenger struct {
	mu      sync.Mutex
	next    int
	sent    []sent
	edits   []sent
	answers []string
	sendErr error
}

func (f *fakeMessenger) SendWithButtons(text string, rows [][]telegram.InlineButton) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.sendErr != nil {
		return 0, f.sendErr
	}
	f.next++
	f.sent = append(f.sent, sent{f.next, text, rows})
	return f.next, nil
}

func (f *fakeMessenger) EditWithButtons(id int, text string, rows [][]telegram.InlineButton) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.edits = append(f.edits, sent{id, text, rows})
	return nil
}

func (f *fakeMessenger) AnswerCallback(id, text string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.answers = append(f.answers, text)
	return nil
}

func (f *fakeMessenger) lastEdit() sent {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.edits[len(f.edits)-1]
}

var qButtons = []Button{{Action: "quarantine", Label: "Quarantine canary"}, {Action: "ignore", Label: "Ignore"}}

func newSvc() (*Service, *fakeMessenger, *time.Time) {
	f := &fakeMessenger{}
	s := New(f)
	now := time.Date(2026, 10, 28, 12, 0, 0, 0, time.UTC)
	s.now = func() time.Time { return now }
	return s, f, &now
}

func TestCreateSendsButtonsWithCallbackData(t *testing.T) {
	s, f, _ := newSvc()
	msgID, err := s.Create("p-abc123", "<b>canary</b> needs attention", qButtons, 0)
	if err != nil || msgID != 1 {
		t.Fatalf("create: %v %d", err, msgID)
	}
	rows := f.sent[0].rows
	if len(rows) != 1 || len(rows[0]) != 2 {
		t.Fatalf("keyboard: %+v", rows)
	}
	if rows[0][0].CallbackData != "act:quarantine:p-abc123" || rows[0][1].CallbackData != "act:ignore:p-abc123" {
		t.Errorf("callback data: %+v", rows[0])
	}
	for _, b := range rows[0] {
		if len(b.CallbackData) > 64 {
			t.Errorf("callback data over Telegram's 64 bytes: %q", b.CallbackData)
		}
	}
	if _, err := s.Create("p-abc123", "again", qButtons, 0); !errors.Is(err, ErrExists) {
		t.Errorf("duplicate id: %v", err)
	}
}

func TestCreateValidation(t *testing.T) {
	s, _, _ := newSvc()
	long := strings.Repeat("x", maxText+1)
	cases := []struct {
		id, text string
		buttons  []Button
	}{
		{"Bad_ID", "t", nil},
		{"abc", "t", nil},
		{"p-ok-id", "", nil},
		{"p-ok-id", long, nil},
		{"p-ok-id", "t", []Button{{"a", "A"}, {"b", "B"}, {"c", "C"}, {"d", "D"}, {"e", "E"}}},
		{"p-ok-id", "t", []Button{{"Quarantine", "x"}}},
		{"p-ok-id", "t", []Button{{"ok", ""}}},
		{"p-ok-id", "t", []Button{{"ok", strings.Repeat("l", maxLabel+1)}}},
		{"p-ok-id", "t", []Button{{"ok", "a"}, {"ok", "b"}}},
		{"p-ok-id", "t", []Button{{"a:b", "x"}}},
	}
	for _, c := range cases {
		if _, err := s.Create(c.id, c.text, c.buttons, 0); err == nil {
			t.Errorf("expected error for %+v", c)
		}
	}
}

func TestCallbackRecordsOneDecision(t *testing.T) {
	s, f, _ := newSvc()
	s.Create("p-abc123", "msg", qButtons, 0)
	if !s.HandleCallback("cb1", "act:quarantine:p-abc123") {
		t.Fatal("act: callback not handled")
	}
	// a second press (or the other button) does nothing
	s.HandleCallback("cb2", "act:ignore:p-abc123")
	s.HandleCallback("cb3", "act:quarantine:p-abc123")
	d := s.Decisions(0)
	if len(d) != 1 || d[0].Action != "quarantine" || d[0].ID != "p-abc123" || d[0].MessageID != 1 {
		t.Fatalf("decisions: %+v", d)
	}
	if len(s.Decisions(0)) != 0 {
		t.Error("decisions are handed out once")
	}
	e := f.lastEdit()
	if e.rows != nil || !strings.Contains(e.text, "Quarantine canary</b> requested") {
		t.Errorf("working edit: %+v", e)
	}
	if f.answers[0] != "Quarantine canary: requested" || f.answers[1] != "This request is no longer active." {
		t.Errorf("answers: %v", f.answers)
	}
}

func TestCallbackRejectsUnknownActionIDAndExpired(t *testing.T) {
	s, _, now := newSvc()
	s.Create("p-abc123", "msg", qButtons, 60)
	for _, data := range []string{"act:release:p-abc123", "act:quarantine:p-nope00", "act:", "act:quarantine"} {
		if !s.HandleCallback("cb", data) {
			t.Errorf("%q not handled", data)
		}
	}
	*now = now.Add(2 * time.Minute)
	s.HandleCallback("cb", "act:quarantine:p-abc123")
	if d := s.Decisions(0); len(d) != 0 {
		t.Errorf("expected no decisions, got %+v", d)
	}
	if s.HandleCallback("cb", "jit:approve:req-1") {
		t.Error("jit: callbacks must not be taken")
	}
}

func TestUpdateRearmsAndCloses(t *testing.T) {
	s, f, _ := newSvc()
	s.Create("p-abc123", "msg", qButtons, 0)
	s.HandleCallback("cb", "act:quarantine:p-abc123")
	s.Decisions(0)
	if err := s.Update("p-abc123", "quarantined", []Button{{"release", "Release canary"}}, 0); err != nil {
		t.Fatal(err)
	}
	if e := f.lastEdit(); e.rows[0][0].CallbackData != "act:release:p-abc123" {
		t.Errorf("re-armed keyboard: %+v", e.rows)
	}
	s.HandleCallback("cb", "act:release:p-abc123")
	if d := s.Decisions(0); len(d) != 1 || d[0].Action != "release" {
		t.Fatalf("release decision: %+v", d)
	}
	if err := s.Update("p-abc123", "released", nil, 0); err != nil {
		t.Fatal(err)
	}
	if f.lastEdit().rows != nil {
		t.Error("closing update must remove the keyboard")
	}
	s.HandleCallback("cb", "act:release:p-abc123")
	if len(s.Decisions(0)) != 0 {
		t.Error("closed prompt accepted a press")
	}
	if err := s.Update("p-unknown", "x", nil, 0); !errors.Is(err, ErrNotFound) {
		t.Errorf("unknown update: %v", err)
	}
}

func TestDecisionsLongPoll(t *testing.T) {
	s, _, _ := newSvc()
	s.Create("p-abc123", "msg", qButtons, 0)
	start := time.Now()
	if d := s.Decisions(100 * time.Millisecond); len(d) != 0 {
		t.Fatalf("unexpected %+v", d)
	}
	if time.Since(start) < 90*time.Millisecond {
		t.Error("long poll returned early")
	}
	go func() {
		time.Sleep(50 * time.Millisecond)
		s.HandleCallback("cb", "act:ignore:p-abc123")
	}()
	start = time.Now()
	d := s.Decisions(5 * time.Second)
	if len(d) != 1 || time.Since(start) > 2*time.Second {
		t.Fatalf("long poll did not wake: %+v after %s", d, time.Since(start))
	}
}

func TestSweepExpiresAndForgets(t *testing.T) {
	s, f, now := newSvc()
	s.Create("p-abc123", "msg", qButtons, 60)
	*now = now.Add(61 * time.Second)
	s.Sweep()
	if e := f.lastEdit(); e.rows != nil || !strings.Contains(e.text, "Expired") {
		t.Errorf("expiry edit: %+v", e)
	}
	*now = now.Add(25 * time.Hour)
	s.Sweep()
	if err := s.Update("p-abc123", "x", nil, 0); !errors.Is(err, ErrNotFound) {
		t.Errorf("resolved prompt not forgotten: %v", err)
	}
}

func TestSendFailureForgetsPrompt(t *testing.T) {
	s, f, _ := newSvc()
	f.sendErr = errors.New("telegram API error: boom")
	if _, err := s.Create("p-abc123", "msg", qButtons, 0); err == nil {
		t.Fatal("expected error")
	}
	f.sendErr = nil
	if _, err := s.Create("p-abc123", "msg", qButtons, 0); err != nil {
		t.Fatalf("retry after failed send: %v", err)
	}
}

func do(t *testing.T, h http.Handler, method, path string, body interface{}) (int, map[string]interface{}) {
	t.Helper()
	var buf bytes.Buffer
	if body != nil {
		json.NewEncoder(&buf).Encode(body)
	}
	req := httptest.NewRequest(method, path, &buf)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	var out map[string]interface{}
	json.Unmarshal(w.Body.Bytes(), &out)
	return w.Code, out
}

func TestHTTPAPI(t *testing.T) {
	s, f, _ := newSvc()
	h := s.Handler()
	code, out := do(t, h, "POST", "/v1/prompts", promptBody{ID: "p-abc123", Text: "hi", Buttons: qButtons})
	if code != http.StatusCreated || out["message_id"].(float64) != 1 {
		t.Fatalf("create: %d %v", code, out)
	}
	if code, _ := do(t, h, "POST", "/v1/prompts", promptBody{ID: "p-abc123", Text: "hi"}); code != http.StatusConflict {
		t.Errorf("duplicate: %d", code)
	}
	if code, _ := do(t, h, "POST", "/v1/prompts", promptBody{ID: "x", Text: "hi"}); code != http.StatusBadRequest {
		t.Errorf("bad id: %d", code)
	}
	if code, _ := do(t, h, "POST", "/v1/prompts", "not an object"); code != http.StatusBadRequest {
		t.Errorf("bad body: %d", code)
	}
	s.HandleCallback("cb", "act:quarantine:p-abc123")
	code, out = do(t, h, "GET", "/v1/decisions?wait=0", nil)
	if code != 200 || len(out["decisions"].([]interface{})) != 1 {
		t.Fatalf("decisions: %d %v", code, out)
	}
	code, out = do(t, h, "GET", "/v1/decisions", nil)
	if code != 200 || len(out["decisions"].([]interface{})) != 0 {
		t.Fatalf("empty decisions must be [] not null: %d %v", code, out)
	}
	if code, _ := do(t, h, "POST", "/v1/prompts/p-abc123", promptBody{Text: "done"}); code != 200 {
		t.Errorf("update: %d", code)
	}
	if code, _ := do(t, h, "POST", "/v1/prompts/p-zzz999", promptBody{Text: "done"}); code != http.StatusNotFound {
		t.Errorf("update unknown: %d", code)
	}
	if code, _ := do(t, h, "POST", "/v1/notify", promptBody{Text: "alert"}); code != http.StatusCreated {
		t.Errorf("notify: %d", code)
	}
	if f.sent[len(f.sent)-1].rows != nil {
		t.Error("notify must not have buttons")
	}
	if code, _ := do(t, h, "GET", "/v1/prompts", nil); code != http.StatusMethodNotAllowed {
		t.Errorf("GET /v1/prompts: %d", code)
	}
	if code, out := do(t, h, "GET", "/v1/health", nil); code != 200 || out["status"] != "ok" {
		t.Errorf("health: %d %v", code, out)
	}
}

func TestListenReplacesStaleSocketWithPrivateMode(t *testing.T) {
	// short path: Unix socket paths are limited to ~104 bytes (macOS)
	dir, err := os.MkdirTemp("/tmp", "act")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(dir)
	path := filepath.Join(dir, "a.sock")
	os.WriteFile(path, []byte("stale"), 0o644)
	l, err := Listen(path)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	st, err := os.Stat(path)
	if err != nil || st.Mode().Perm() != 0o600 || st.Mode()&os.ModeSocket == 0 {
		t.Fatalf("socket mode: %v %v", st.Mode(), err)
	}
}
