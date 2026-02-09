package telegram

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/nkontur/jit-approval-svc/internal/logger"
)

// Client wraps the Telegram Bot API.
type Client struct {
	token   string
	chatID  int64
	baseURL string
	http    *http.Client
}

// New creates a new Telegram client.
func New(token string, chatID int64) *Client {
	return &Client{
		token:   token,
		chatID:  chatID,
		baseURL: "https://api.telegram.org/bot" + token,
		http: &http.Client{
			Timeout: 10 * time.Second,
		},
	}
}

// InlineButton represents a Telegram inline keyboard button.
type InlineButton struct {
	Text         string `json:"text"`
	CallbackData string `json:"callback_data"`
}

// SendApprovalMessage sends an approval request to Noah with inline buttons.
// Returns the message ID for later editing.
// VaultPathInfo holds path and capabilities for display in Telegram messages.
type VaultPathInfo struct {
	Path         string
	Capabilities []string
}

func (c *Client) SendApprovalMessage(requestID, resource string, tier int, reason, requester string, ttlStr string, scopes []string, vaultPaths []VaultPathInfo) (int, error) {
	emoji := "🔐"
	tierDesc := "Quick Approve"
	if tier >= 3 {
		emoji = "🔒"
		tierDesc = "Elevated"
	}

	scopeStr := ""
	if len(scopes) > 0 {
		scopeStr = fmt.Sprintf("\n<b>Scopes:</b> %s", strings.Join(scopes, ", "))
	}

	vaultPathStr := ""
	if len(vaultPaths) > 0 {
		vaultPathStr = "\n\n📂 <b>Vault Paths Requested:</b>"
		for _, vp := range vaultPaths {
			vaultPathStr += fmt.Sprintf("\n  • <code>%s</code> [%s]", vp.Path, strings.Join(vp.Capabilities, ", "))
		}
	}

	text := fmt.Sprintf(
		"%s <b>JIT Access Request</b> [%s]\n\n"+
			"<b>Resource:</b> %s\n"+
			"<b>Tier:</b> %d (%s)\n"+
			"<b>TTL:</b> %s\n"+
			"<b>Requester:</b> %s\n"+
			"<b>Reason:</b> %s%s%s\n\n"+
			"⏳ Awaiting approval...",
		emoji, requestID, resource, tier, tierDesc, ttlStr, requester, reason, scopeStr, vaultPathStr,
	)

	buttons := [][]InlineButton{
		{
			{Text: "✅ Approve", CallbackData: fmt.Sprintf("jit:approve:%s", requestID)},
			{Text: "❌ Deny", CallbackData: fmt.Sprintf("jit:deny:%s", requestID)},
		},
	}

	return c.sendMessage(text, buttons)
}

// EditMessageApproved edits an approval message to show it was approved.
func (c *Client) EditMessageApproved(messageID int, requestID, resource string) error {
	text := fmt.Sprintf(
		"✅ <b>Approved</b> [%s]\n\n"+
			"<b>Resource:</b> %s\n"+
			"<b>Approved at:</b> %s",
		requestID, resource, time.Now().Format("15:04:05 MST"),
	)
	return c.editMessage(messageID, text)
}

// EditMessageDenied edits an approval message to show it was denied.
func (c *Client) EditMessageDenied(messageID int, requestID, resource string) error {
	text := fmt.Sprintf(
		"❌ <b>Denied</b> [%s]\n\n"+
			"<b>Resource:</b> %s\n"+
			"<b>Denied at:</b> %s",
		requestID, resource, time.Now().Format("15:04:05 MST"),
	)
	return c.editMessage(messageID, text)
}

// EditMessageTimeout edits an approval message to show it timed out.
func (c *Client) EditMessageTimeout(messageID int, requestID, resource string) error {
	text := fmt.Sprintf(
		"⏰ <b>Expired</b> [%s]\n\n"+
			"<b>Resource:</b> %s\n"+
			"<b>Reason:</b> Request timed out",
		requestID, resource,
	)
	return c.editMessage(messageID, text)
}

// sendMessage sends a message with optional inline keyboard.
func (c *Client) sendMessage(text string, buttons [][]InlineButton) (int, error) {
	payload := map[string]interface{}{
		"chat_id":    c.chatID,
		"text":       text,
		"parse_mode": "HTML",
	}

	if len(buttons) > 0 {
		payload["reply_markup"] = map[string]interface{}{
			"inline_keyboard": buttons,
		}
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return 0, fmt.Errorf("marshal message payload: %w", err)
	}

	resp, err := c.http.Post(c.baseURL+"/sendMessage", "application/json", bytes.NewReader(body))
	if err != nil {
		return 0, fmt.Errorf("send telegram message: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		OK     bool `json:"ok"`
		Result struct {
			MessageID int `json:"message_id"`
		} `json:"result"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return 0, fmt.Errorf("decode telegram response: %w", err)
	}
	if !result.OK {
		return 0, fmt.Errorf("telegram API error: %s", result.Description)
	}

	logger.Info("telegram_message_sent", logger.Fields{
		"message_id": result.Result.MessageID,
	})

	return result.Result.MessageID, nil
}

// editMessage edits an existing message (removes inline keyboard).
func (c *Client) editMessage(messageID int, text string) error {
	payload := map[string]interface{}{
		"chat_id":    c.chatID,
		"message_id": messageID,
		"text":       text,
		"parse_mode": "HTML",
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal edit payload: %w", err)
	}

	resp, err := c.http.Post(c.baseURL+"/editMessageText", "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("edit telegram message: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		OK          bool   `json:"ok"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return fmt.Errorf("decode telegram edit response: %w", err)
	}
	if !result.OK {
		return fmt.Errorf("telegram edit API error: %s", result.Description)
	}

	return nil
}

// SetWebhook configures the Telegram webhook URL.
func (c *Client) SetWebhook(url, secret string) error {
	payload := map[string]interface{}{
		"url":          url,
		"secret_token": secret,
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal webhook payload: %w", err)
	}

	resp, err := c.http.Post(c.baseURL+"/setWebhook", "application/json", bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("set webhook: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		OK          bool   `json:"ok"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return fmt.Errorf("decode webhook response: %w", err)
	}
	if !result.OK {
		return fmt.Errorf("telegram webhook error: %s", result.Description)
	}

	logger.Info("telegram_webhook_set", logger.Fields{
		"url": url,
	})

	return nil
}
