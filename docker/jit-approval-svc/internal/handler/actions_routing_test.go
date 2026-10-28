package handler

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/nkontur/jit-approval-svc/internal/actions"
	"github.com/nkontur/jit-approval-svc/internal/telegram"
)

type nopMessenger struct{ n int }

func (m *nopMessenger) SendWithButtons(string, [][]telegram.InlineButton) (int, error) {
	m.n++
	return m.n, nil
}
func (m *nopMessenger) EditWithButtons(int, string, [][]telegram.InlineButton) error { return nil }
func (m *nopMessenger) AnswerCallback(string, string) error                          { return nil }

func webhook(t *testing.T, h *Handler, secret string, from int64, data string) int {
	t.Helper()
	body, _ := json.Marshal(map[string]interface{}{
		"update_id": 1,
		"callback_query": map[string]interface{}{
			"id": "cb-1", "from": map[string]interface{}{"id": from},
			"message": map[string]interface{}{"message_id": 1}, "data": data,
		},
	})
	req := httptest.NewRequest(http.MethodPost, "/telegram/webhook", bytes.NewReader(body))
	req.Header.Set("X-Telegram-Bot-Api-Secret-Token", secret)
	w := httptest.NewRecorder()
	h.HandleTelegramWebhook(w, req)
	return w.Code
}

func TestWebhookRoutesActionCallbacks(t *testing.T) {
	h := mockHandler()
	svc := actions.New(&nopMessenger{})
	h.SetActions(svc)
	if _, err := svc.Create("p-route1", "msg", []actions.Button{{Action: "ignore", Label: "Ignore"}}, 0); err != nil {
		t.Fatal(err)
	}

	// wrong secret and wrong user are rejected before routing
	if code := webhook(t, h, "wrong", h.cfg.TelegramChatID, "act:ignore:p-route1"); code != http.StatusUnauthorized {
		t.Errorf("wrong secret: %d", code)
	}
	webhook(t, h, "test-secret", 12345, "act:ignore:p-route1")
	if d := svc.Decisions(0); len(d) != 0 {
		t.Fatalf("callback from another user was accepted: %+v", d)
	}

	if code := webhook(t, h, "test-secret", h.cfg.TelegramChatID, "act:ignore:p-route1"); code != http.StatusOK {
		t.Fatalf("webhook: %d", code)
	}
	d := svc.Decisions(0)
	if len(d) != 1 || d[0].Action != "ignore" {
		t.Fatalf("decision not recorded: %+v", d)
	}
}

func TestWebhookJITCallbacksUnaffectedByActions(t *testing.T) {
	h := mockHandler()
	h.SetActions(actions.New(&nopMessenger{}))
	req, _ := h.store.Create("prometheus", "gitlab", 2, "test", []string{"api"})
	// deny path does not need Vault or Telegram (message ID 0)
	if code := webhook(t, h, "test-secret", h.cfg.TelegramChatID, "jit:deny:"+req.ID); code != http.StatusOK {
		t.Fatalf("webhook: %d", code)
	}
	if got := h.store.Get(req.ID).Status; got != "denied" {
		t.Errorf("jit deny not processed with actions enabled: %s", got)
	}
}
