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

// RequestDisplayInfo holds the display fields for a JIT request,
// used by both initial send and subsequent edit messages.
type RequestDisplayInfo struct {
	RequestID  string
	Resource   string
	Tier       int
	Reason     string
	Requester  string
	TTL        string
	Scopes     []string
	VaultPaths []VaultPathInfo
}

// formatRequestDetails returns the HTML-formatted detail block for a request.
func formatRequestDetails(info RequestDisplayInfo) string {
	tierDesc := "Quick Approve"
	if info.Tier >= 3 {
		tierDesc = "Elevated"
	}

	scopeStr := ""
	if len(info.Scopes) > 0 {
		scopeStr = fmt.Sprintf("\n<b>Scopes:</b> %s", strings.Join(info.Scopes, ", "))
	}

	vaultPathStr := ""
	if len(info.VaultPaths) > 0 {
		vaultPathStr = "\n\n📂 <b>Vault Paths Requested:</b>"
		for _, vp := range info.VaultPaths {
			vaultPathStr += fmt.Sprintf("\n  • <code>%s</code> [%s]", vp.Path, strings.Join(vp.Capabilities, ", "))
		}
	}

	return fmt.Sprintf(
		"<b>Resource:</b> %s\n"+
			"<b>Tier:</b> %d (%s)\n"+
			"<b>TTL:</b> %s\n"+
			"<b>Requester:</b> %s\n"+
			"<b>Reason:</b> %s%s%s",
		info.Resource, info.Tier, tierDesc, info.TTL, info.Requester, info.Reason, scopeStr, vaultPathStr,
	)
}

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
	if tier >= 3 {
		emoji = "🔒"
	}

	info := RequestDisplayInfo{
		RequestID:  requestID,
		Resource:   resource,
		Tier:       tier,
		Reason:     reason,
		Requester:  requester,
		TTL:        ttlStr,
		Scopes:     scopes,
		VaultPaths: vaultPaths,
	}

	text := fmt.Sprintf(
		"%s <b>JIT Access Request</b> [%s]\n\n%s\n\n⏳ Awaiting approval...",
		emoji, requestID, formatRequestDetails(info),
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
func (c *Client) EditMessageApproved(messageID int, info RequestDisplayInfo) error {
	text := fmt.Sprintf(
		"✅ <b>Approved</b> [%s]\n\n%s\n\n<b>Approved at:</b> %s",
		info.RequestID, formatRequestDetails(info), time.Now().Format("15:04:05 MST"),
	)
	return c.editMessage(messageID, text)
}

// EditMessageDenied edits an approval message to show it was denied.
func (c *Client) EditMessageDenied(messageID int, info RequestDisplayInfo) error {
	text := fmt.Sprintf(
		"❌ <b>Denied</b> [%s]\n\n%s\n\n<b>Denied at:</b> %s",
		info.RequestID, formatRequestDetails(info), time.Now().Format("15:04:05 MST"),
	)
	return c.editMessage(messageID, text)
}

// EditMessageTimeout edits an approval message to show it timed out.
func (c *Client) EditMessageTimeout(messageID int, info RequestDisplayInfo) error {
	text := fmt.Sprintf(
		"⏰ <b>Expired</b> [%s]\n\n%s\n\n<b>Expired at:</b> %s",
		info.RequestID, formatRequestDetails(info), time.Now().Format("15:04:05 MST"),
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

// getPublicIP returns the current public IP address.
func getPublicIP() (string, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Get("https://ifconfig.me")
	if err != nil {
		return "", fmt.Errorf("get public IP: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return "", fmt.Errorf("read public IP response: %w", err)
	}

	ip := strings.TrimSpace(string(body))
	if ip == "" {
		return "", fmt.Errorf("empty public IP response")
	}
	return ip, nil
}

// SetWebhook configures the Telegram webhook URL.
// It resolves the current public IP and passes it to Telegram via ip_address
// to ensure callbacks are delivered to the correct address even after IP changes.
func (c *Client) SetWebhook(url, secret string) error {
	payload := map[string]interface{}{
		"url":             url,
		"secret_token":    secret,
		"allowed_updates": []string{"callback_query"},
	}

	// Resolve and pin our public IP so Telegram doesn't use a stale cached address
	if ip, err := getPublicIP(); err != nil {
		logger.Warn("webhook_ip_resolve_failed", logger.Fields{
			"error": err.Error(),
		})
	} else {
		payload["ip_address"] = ip
		logger.Info("webhook_ip_resolved", logger.Fields{
			"ip": ip,
		})
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
