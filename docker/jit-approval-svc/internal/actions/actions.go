// Package actions lets a trusted local client (on the Docker host) put a
// message with inline buttons in front of Noah and learn which button he
// pressed. It only relays decisions: it never acts on them. The client
// executes the decision itself and reports the outcome back by updating the
// message.
//
// The API is served on a Unix socket (ACTIONS_SOCKET) that is bind-mounted
// from a host directory, so filesystem permissions on the host are the access
// control: nothing on a container network can reach it, and there is no
// shared secret to leak.
//
//	POST /v1/prompts           {id, text, buttons:[{action,label}], ttl_seconds}
//	POST /v1/prompts/{id}      {text, buttons, ttl_seconds}   edit text; new buttons re-arm it
//	GET  /v1/decisions?wait=N  pressed buttons since the last call (long poll, N <= 25 s)
//	POST /v1/notify            {text}                          plain message, no buttons
//	GET  /v1/health
//
// Button presses arrive on the existing Telegram webhook as callback data
// "act:<action>:<id>", after the webhook's secret-token and chat-ID checks.
package actions

import (
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"net"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
	"github.com/nkontur/jit-approval-svc/internal/telegram"
)

const (
	// CallbackPrefix marks callback data handled by this package.
	CallbackPrefix = "act:"

	maxButtons    = 4
	maxLabel      = 40
	maxText       = 3500
	maxPrompts    = 200
	maxDecisions  = 100
	maxWait       = 25 * time.Second
	defaultTTL    = 72 * time.Hour
	maxTTL        = 7 * 24 * time.Hour
	keepResolved  = 24 * time.Hour
	statusPending = "pending"
	statusDecided = "decided"
	statusClaimed = "claimed"
	statusExpired = "expired"
)

var (
	idRE     = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{3,39}$`)
	actionRE = regexp.MustCompile(`^[a-z][a-z0-9-]{1,15}$`)

	// ErrNotFound is returned for an unknown prompt ID.
	ErrNotFound = errors.New("prompt not found")
	// ErrExists is returned when creating a prompt whose ID is still known.
	ErrExists = errors.New("prompt id already exists")
	// ErrFull is returned when too many prompts are held.
	ErrFull = errors.New("too many prompts")
)

// Messenger is the part of the Telegram client this package needs.
type Messenger interface {
	SendWithButtons(text string, rows [][]telegram.InlineButton) (int, error)
	EditWithButtons(messageID int, text string, rows [][]telegram.InlineButton) error
	AnswerCallback(callbackID, text string) error
}

// Button is one inline button: Action is returned in the decision, Label is shown.
type Button struct {
	Action string `json:"action"`
	Label  string `json:"label"`
}

// Decision is one button press, delivered once to the client.
type Decision struct {
	ID        string    `json:"id"`
	Action    string    `json:"action"`
	DecidedAt time.Time `json:"decided_at"`
	MessageID int       `json:"message_id"`
}

type prompt struct {
	id        string
	text      string
	buttons   []Button
	messageID int
	status    string
	created   time.Time
	expires   time.Time
	resolved  time.Time
}

// Service holds prompts in memory. A restart forgets them: pending buttons then
// answer "no longer active", and the client re-sends what it still needs.
type Service struct {
	msgr Messenger
	now  func() time.Time

	mu        sync.Mutex
	prompts   map[string]*prompt
	decisions []Decision
	wake      chan struct{}
}

// New creates a Service that sends through msgr.
func New(msgr Messenger) *Service {
	return &Service{msgr: msgr, now: time.Now, prompts: map[string]*prompt{}, wake: make(chan struct{})}
}

// --- validation -------------------------------------------------------------

func validateButtons(buttons []Button) error {
	if len(buttons) > maxButtons {
		return fmt.Errorf("at most %d buttons", maxButtons)
	}
	seen := map[string]bool{}
	for _, b := range buttons {
		if !actionRE.MatchString(b.Action) {
			return fmt.Errorf("invalid action %q", b.Action)
		}
		if seen[b.Action] {
			return fmt.Errorf("duplicate action %q", b.Action)
		}
		seen[b.Action] = true
		if b.Label == "" || len([]rune(b.Label)) > maxLabel {
			return fmt.Errorf("button label must be 1-%d characters", maxLabel)
		}
	}
	return nil
}

func validateText(text string) error {
	if strings.TrimSpace(text) == "" {
		return errors.New("text is required")
	}
	if len(text) > maxText {
		return fmt.Errorf("text longer than %d bytes", maxText)
	}
	return nil
}

func ttlFrom(seconds int) time.Duration {
	if seconds <= 0 {
		return defaultTTL
	}
	d := time.Duration(seconds) * time.Second
	if d > maxTTL {
		return maxTTL
	}
	return d
}

func keyboard(id string, buttons []Button) [][]telegram.InlineButton {
	if len(buttons) == 0 {
		return nil
	}
	row := make([]telegram.InlineButton, 0, len(buttons))
	for _, b := range buttons {
		row = append(row, telegram.InlineButton{Text: b.Label, CallbackData: CallbackPrefix + b.Action + ":" + id})
	}
	return [][]telegram.InlineButton{row}
}

// --- operations ---------------------------------------------------------------

// Create sends a new prompt.
func (s *Service) Create(id, text string, buttons []Button, ttlSeconds int) (int, error) {
	if !idRE.MatchString(id) {
		return 0, fmt.Errorf("invalid id %q", id)
	}
	if err := validateText(text); err != nil {
		return 0, err
	}
	if err := validateButtons(buttons); err != nil {
		return 0, err
	}
	s.mu.Lock()
	if _, ok := s.prompts[id]; ok {
		s.mu.Unlock()
		return 0, ErrExists
	}
	if len(s.prompts) >= maxPrompts {
		s.mu.Unlock()
		return 0, ErrFull
	}
	now := s.now()
	p := &prompt{id: id, text: text, buttons: buttons, status: statusPending, created: now, expires: now.Add(ttlFrom(ttlSeconds))}
	if len(buttons) == 0 {
		p.status, p.resolved = statusClaimed, now
	}
	s.prompts[id] = p
	s.mu.Unlock()

	msgID, err := s.msgr.SendWithButtons(text, keyboard(id, buttons))
	if err != nil {
		s.mu.Lock()
		delete(s.prompts, id)
		s.mu.Unlock()
		return 0, err
	}
	s.mu.Lock()
	p.messageID = msgID
	s.mu.Unlock()
	logger.Info("action_prompt_sent", logger.Fields{"prompt_id": id, "buttons": len(buttons), "telegram_message_id": msgID})
	return msgID, nil
}

// Update replaces a prompt's text and buttons. Non-empty buttons make it
// pending again (e.g. "Release" after a quarantine); empty buttons close it.
func (s *Service) Update(id, text string, buttons []Button, ttlSeconds int) error {
	if err := validateText(text); err != nil {
		return err
	}
	if err := validateButtons(buttons); err != nil {
		return err
	}
	s.mu.Lock()
	p, ok := s.prompts[id]
	if !ok {
		s.mu.Unlock()
		return ErrNotFound
	}
	now := s.now()
	p.text, p.buttons = text, buttons
	if len(buttons) > 0 {
		p.status, p.expires, p.resolved = statusPending, now.Add(ttlFrom(ttlSeconds)), time.Time{}
	} else if p.status == statusPending {
		p.status, p.resolved = statusClaimed, now
	}
	msgID := p.messageID
	s.mu.Unlock()
	return s.msgr.EditWithButtons(msgID, text, keyboard(id, buttons))
}

// Notify sends a plain message without buttons.
func (s *Service) Notify(text string) (int, error) {
	if err := validateText(text); err != nil {
		return 0, err
	}
	return s.msgr.SendWithButtons(text, nil)
}

// HandleCallback processes callback data "act:<action>:<id>". It returns
// false if the data is not for this package.
func (s *Service) HandleCallback(callbackID, data string) bool {
	if !strings.HasPrefix(data, CallbackPrefix) {
		return false
	}
	rest := strings.TrimPrefix(data, CallbackPrefix)
	action, id, _ := strings.Cut(rest, ":")

	s.mu.Lock()
	p, ok := s.prompts[id]
	var label string
	valid := ok && p.status == statusPending && s.now().Before(p.expires)
	if valid {
		valid = false
		for _, b := range p.buttons {
			if b.Action == action {
				valid, label = true, b.Label
			}
		}
	}
	if !valid {
		s.mu.Unlock()
		logger.Warn("action_callback_rejected", logger.Fields{"prompt_id": id, "action": action})
		s.answer(callbackID, "This request is no longer active.")
		return true
	}
	now := s.now()
	p.status, p.resolved = statusDecided, now
	text, msgID := p.text, p.messageID
	if len(s.decisions) >= maxDecisions {
		s.decisions = s.decisions[1:]
	}
	s.decisions = append(s.decisions, Decision{ID: id, Action: action, DecidedAt: now, MessageID: msgID})
	close(s.wake)
	s.wake = make(chan struct{})
	s.mu.Unlock()

	logger.Info("action_decided", logger.Fields{"prompt_id": id, "action": action})
	s.answer(callbackID, label+": requested")
	working := fmt.Sprintf("%s\n\n⏳ <b>%s</b> requested at %s, working…", text, html.EscapeString(label),
		now.Format("15:04:05 MST"))
	if err := s.msgr.EditWithButtons(msgID, working, nil); err != nil {
		logger.Error("action_edit_failed", logger.Fields{"prompt_id": id, "error": err.Error()})
	}
	return true
}

func (s *Service) answer(callbackID, text string) {
	if callbackID == "" {
		return
	}
	if err := s.msgr.AnswerCallback(callbackID, text); err != nil {
		logger.Warn("action_answer_failed", logger.Fields{"error": err.Error()})
	}
}

// Decisions returns (and hands over) the pending decisions, waiting up to
// wait for one to arrive if there are none.
func (s *Service) Decisions(wait time.Duration) []Decision {
	if wait > maxWait {
		wait = maxWait
	}
	deadline := time.NewTimer(wait)
	defer deadline.Stop()
	for {
		s.mu.Lock()
		if len(s.decisions) > 0 || wait <= 0 {
			out := s.decisions
			s.decisions = nil
			for _, d := range out {
				if p, ok := s.prompts[d.ID]; ok && p.status == statusDecided {
					p.status = statusClaimed
				}
			}
			s.mu.Unlock()
			if out == nil {
				out = []Decision{}
			}
			return out
		}
		wake := s.wake
		s.mu.Unlock()
		select {
		case <-wake:
		case <-deadline.C:
			wait = 0
		}
	}
}

// Sweep expires pending prompts past their TTL (removing their buttons) and
// forgets resolved ones after a day.
func (s *Service) Sweep() {
	now := s.now()
	type edit struct {
		id    string
		msgID int
		text  string
	}
	var edits []edit
	s.mu.Lock()
	for id, p := range s.prompts {
		switch {
		case p.status == statusPending && !now.Before(p.expires):
			p.status, p.resolved = statusExpired, now
			edits = append(edits, edit{id, p.messageID, p.text + "\n\n⏰ <b>Expired</b>, no action taken."})
		case p.status != statusPending && !p.resolved.IsZero() && now.Sub(p.resolved) > keepResolved:
			delete(s.prompts, id)
		}
	}
	s.mu.Unlock()
	for _, e := range edits {
		logger.Info("action_prompt_expired", logger.Fields{"prompt_id": e.id})
		if err := s.msgr.EditWithButtons(e.msgID, e.text, nil); err != nil {
			logger.Error("action_edit_failed", logger.Fields{"prompt_id": e.id, "error": err.Error()})
		}
	}
}

// --- HTTP API ---------------------------------------------------------------

type promptBody struct {
	ID         string   `json:"id"`
	Text       string   `json:"text"`
	Buttons    []Button `json:"buttons"`
	TTLSeconds int      `json:"ttl_seconds"`
}

// Handler returns the HTTP API (served on the Unix socket only).
func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /v1/prompts", func(w http.ResponseWriter, r *http.Request) {
		var b promptBody
		if !decode(w, r, &b) {
			return
		}
		msgID, err := s.Create(b.ID, b.Text, b.Buttons, b.TTLSeconds)
		if err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, map[string]interface{}{"id": b.ID, "message_id": msgID})
	})
	mux.HandleFunc("POST /v1/prompts/{id}", func(w http.ResponseWriter, r *http.Request) {
		var b promptBody
		if !decode(w, r, &b) {
			return
		}
		if err := s.Update(r.PathValue("id"), b.Text, b.Buttons, b.TTLSeconds); err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"id": r.PathValue("id"), "status": "updated"})
	})
	mux.HandleFunc("GET /v1/decisions", func(w http.ResponseWriter, r *http.Request) {
		wait, _ := strconv.Atoi(r.URL.Query().Get("wait"))
		writeJSON(w, http.StatusOK, map[string]interface{}{"decisions": s.Decisions(time.Duration(wait) * time.Second)})
	})
	mux.HandleFunc("POST /v1/notify", func(w http.ResponseWriter, r *http.Request) {
		var b promptBody
		if !decode(w, r, &b) {
			return
		}
		msgID, err := s.Notify(b.Text)
		if err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, map[string]int{"message_id": msgID})
	})
	mux.HandleFunc("GET /v1/health", func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		n := len(s.prompts)
		s.mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]interface{}{"status": "ok", "prompts": n})
	})
	return mux
}

func decode(w http.ResponseWriter, r *http.Request, v interface{}) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 16<<10)
	if err := json.NewDecoder(r.Body).Decode(v); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON body"})
		return false
	}
	return true
}

func writeErr(w http.ResponseWriter, err error) {
	status := http.StatusBadRequest
	switch {
	case errors.Is(err, ErrNotFound):
		status = http.StatusNotFound
	case errors.Is(err, ErrExists):
		status = http.StatusConflict
	case errors.Is(err, ErrFull):
		status = http.StatusServiceUnavailable
	case strings.Contains(err.Error(), "telegram"):
		status = http.StatusBadGateway
	}
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// Listen opens the Unix socket at path (replacing a stale one) with mode 0600.
func Listen(path string) (net.Listener, error) {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove stale socket: %w", err)
	}
	l, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0o600); err != nil {
		l.Close()
		return nil, err
	}
	return l, nil
}

// Server returns the HTTP server for the socket. WriteTimeout leaves room for
// the decisions long poll.
func (s *Service) Server() *http.Server {
	return &http.Server{
		Handler:      s.Handler(),
		ReadTimeout:  10 * time.Second,
		WriteTimeout: maxWait + 15*time.Second,
		IdleTimeout:  60 * time.Second,
	}
}
