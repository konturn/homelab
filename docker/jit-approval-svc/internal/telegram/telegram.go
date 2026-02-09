package telegram

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
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
func (c *Client) SendApprovalMessage(requestID, resource string, tier int, reason, requester string, ttlStr string) (int, error) {
	emoji := "🔐"
	tierDesc := "Quick Approve"
	if tier >= 3 {
		emoji = "🔒"
		tierDesc = "Elevated"
	}

	text := fmt.Sprintf(
		"%s <b>JIT Access Request</b> [%s]\n\n"+
			"<b>Resource:</b> %s\n"+
			"<b>Tier:</b> %d (%s)\n"+
			"<b>TTL:</b> %s\n"+
			"<b>Requester:</b> %s\n"+
			"<b>Reason:</b> %s\n\n"+
			"⏳ Awaiting approval...",
		emoji, requestID, resource, tier, tierDesc, ttlStr, requester, reason,
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

// WebhookInfo holds the response from getWebhookInfo.
type WebhookInfo struct {
	URL string `json:"url"`
}

// GetWebhookInfo returns the current webhook configuration.
func (c *Client) GetWebhookInfo() (*WebhookInfo, error) {
	resp, err := c.http.Get(c.baseURL + "/getWebhookInfo")
	if err != nil {
		return nil, fmt.Errorf("get webhook info: %w", err)
	}
	defer resp.Body.Close()

	respBody, _ := io.ReadAll(resp.Body)

	var result struct {
		OK     bool        `json:"ok"`
		Result WebhookInfo `json:"result"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(respBody, &result); err != nil {
		return nil, fmt.Errorf("decode webhook info response: %w", err)
	}
	if !result.OK {
		return nil, fmt.Errorf("telegram webhook info error: %s", result.Description)
	}

	return &result.Result, nil
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
