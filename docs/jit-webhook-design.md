# JIT Approval Webhook Notifications — Design Document

> **Status:** Draft  
> **Author:** Prometheus (moltbot)  
> **Date:** 2026-02-12  
> **Depends on:** [JIT Access Design Doc](./jit-access-design.md)  
> **Stakeholder:** Noah Kontur

---

## 1. Problem Statement

Today, when the agent requests JIT access (Tier 2+), a sub-agent polls `GET /api/v1/request/:id` every 5–15 seconds until Noah approves, denies, or the request times out. This wastes LLM tokens (~$0.02–0.10 per approval wait), occupies a sub-agent session, and adds 5–60 seconds of unnecessary latency after approval (waiting for the next poll cycle).

**Goal:** The JIT approval bot pushes a notification to OpenClaw the moment a request is resolved, eliminating polling entirely.

---

## 2. Architecture: Before vs After

### Before (Polling)

```
Agent ──POST /request──▶ JIT Bot ──Telegram──▶ Noah
Agent ──GET /request/:id──▶ JIT Bot  (pending)     │
Agent ──GET /request/:id──▶ JIT Bot  (pending)     │
Agent ──GET /request/:id──▶ JIT Bot  (pending)   taps ✅
Agent ──GET /request/:id──▶ JIT Bot  (approved + token)
```

**Cost:** N poll cycles × LLM turn cost. Sub-agent blocked the whole time.

### After (Webhook Push)

```
Agent ──POST /request──▶ JIT Bot ──Telegram──▶ Noah
  (agent session suspends, no polling)              │
                                                  taps ✅
JIT Bot ──POST /hooks/agent──▶ OpenClaw ──▶ Agent receives token
```

**Cost:** 2 LLM turns total (request + callback). Zero polling.

---

## 3. Webhook Endpoint Choice: `/hooks/agent`

### Options Considered

| Endpoint | Behavior | Fit |
|----------|----------|-----|
| `/hooks/wake` | Injects text into main session, triggers heartbeat | ❌ Main session must parse and route; no session isolation |
| `/hooks/agent` | Runs an isolated agent turn with its own `sessionKey` | ✅ Can target the exact session waiting for this approval |
| Custom mapping | Requires config changes, JS transform | ❌ Over-engineered for structured internal events |

### Decision: `/hooks/agent` with consistent `sessionKey`

**Why:** The `/hooks/agent` endpoint lets the JIT bot target a specific session by `sessionKey`. The requesting agent passes its session key in the JIT request. When the approval resolves, the bot fires a webhook with that same `sessionKey`, and OpenClaw delivers the result directly to the waiting context.

This avoids the main session having to parse and route approval events. The approval lands exactly where it's needed.

---

## 4. Session Targeting

### Recommended: Option B — Direct session targeting

The requesting sub-agent includes its `sessionKey` in the JIT request. The JIT bot stores it and uses it in the webhook callback.

```
Sub-agent (sessionKey: "agent:main:subagent:abc123")
  ──POST /api/v1/request──▶ JIT Bot
    body: {
      "requester": "prometheus",
      "resource": "ssh:router",
      "reason": "...",
      "ttl_minutes": 15,
      "callback_session_key": "agent:main:subagent:abc123"
    }

On approval:
  JIT Bot ──POST /hooks/agent──▶ OpenClaw
    body: {
      "message": "JIT request req-a1b2c3 APPROVED for ssh:router. Token: hvs.XXX. Expires: 2026-02-12T17:00:00Z.",
      "sessionKey": "agent:main:subagent:abc123",
      "deliver": false,
      "name": "JIT"
    }
```

**Why not Option A (wake main session)?** Adds routing complexity. Main session would need to understand JIT request IDs, look up which sub-agent is waiting, and forward. Fragile.

**Why not Option C (pattern-based keys like `hook:jit:req-abc`)?** Creates a new session rather than resuming the waiting one. The sub-agent that made the request would never receive the callback. This would only work if we redesigned the flow so the requesting agent fires-and-forgets and a separate hook-triggered agent picks up the result — unnecessarily complex.

### Session Key Lifecycle

1. Sub-agent spawns with sessionKey `agent:main:subagent:<uuid>`
2. Sub-agent submits JIT request, including its sessionKey as `callback_session_key`
3. Sub-agent's final message: "JIT request submitted, waiting for webhook callback"
4. Sub-agent session suspends (no active turn)
5. JIT bot resolves request → fires `/hooks/agent` with that sessionKey
6. OpenClaw resumes the session with the approval message as input
7. Sub-agent processes the token and completes its task

**Key concern:** If the sub-agent session has been garbage-collected by OpenClaw before the callback arrives, the webhook creates a new session with that key — which is fine, as long as the message contains enough context (request ID, resource, token) for a fresh agent turn to act on it.

---

## 5. Security Analysis

This is the critical section. Adding a webhook callback from the JIT bot to OpenClaw creates a new attack surface.

### 5.1 Threat: Forged Approval via Webhook

**Attack:** A compromised container on the Docker network sends `POST /hooks/agent` with a fake "approved" message and a malicious token (or instructions to the agent).

**Mitigations (layered):**

1. **Webhook token authentication.** The `/hooks/agent` endpoint requires `Authorization: Bearer <token>`. The JIT bot must have this token. An attacker without the token gets 401.

2. **Network isolation.** The OpenClaw webhook listener should only be reachable from the JIT approval service container. Docker network policy or iptables on the host can restrict this.

3. **Cryptographic attestation (from existing design).** The JIT bot already signs grant responses with Ed25519 (see JIT design doc §7.1). The webhook payload MUST include the same signature. The receiving agent verifies the signature against the known public key before accepting ANY approval.

4. **Request ID correlation.** The agent only accepts approvals for request IDs it actually submitted. A forged webhook with an unknown request ID is ignored.

5. **Token validation.** Before using any Vault token received via webhook, the agent validates it against Vault (`POST /auth/token/lookup-self`) to confirm it has the expected policies and TTL.

**Defense in depth stack:**
```
Layer 1: Bearer token on webhook endpoint (blocks unauthenticated)
Layer 2: Network restriction (blocks other containers)
Layer 3: Ed25519 signature (blocks token-holder without signing key)
Layer 4: Request ID correlation (blocks unsolicited approvals)
Layer 5: Vault token validation (blocks fake tokens)
```

An attacker would need ALL of: webhook token + network access + Ed25519 private key + knowledge of pending request IDs + ability to mint valid Vault tokens. At that point they've compromised both the JIT bot and Vault, and we have bigger problems.

### 5.2 Threat: Replay Attacks

**Attack:** Attacker captures a legitimate webhook callback and replays it later.

**Mitigations:**
- The Ed25519 signature includes a nonce and `granted_at` timestamp (per existing design)
- The agent rejects callbacks for request IDs already in `claimed` state
- Vault tokens have TTLs; replaying an expired token is useless
- The JIT bot's SQLite tracks request state — a request can only transition to `approved` once

### 5.3 Threat: Webhook Token Compromise

**Where the token lives:**
- Stored in Vault at `secret/data/services/openclaw/webhook_token`
- JIT approval bot reads it at startup via its AppRole auth
- Lives in JIT bot process memory only (not as env var — same pattern as §8.7 in JIT design doc)

**Rotation:**
- Token rotation requires updating both OpenClaw config and Vault secret
- Can be automated: JIT bot reads token from Vault on each webhook call (not cached), OpenClaw config reloads on SIGHUP
- Rotation frequency: monthly, or immediately on suspected compromise

### 5.4 Threat: Webhook Payload as Prompt Injection

**Attack:** The webhook message field contains prompt injection targeting the receiving agent.

**Mitigations:**
- OpenClaw wraps webhook payloads in safety boundaries by default (`<<<EXTERNAL_UNTRUSTED_CONTENT>>>`)
- The JIT bot sends structured, predictable messages — not arbitrary user content
- The receiving agent should parse the message for expected fields (request ID, status, token) rather than treating it as free-form instructions
- Consider using `allowUnsafeExternalContent: true` ONLY if the safety wrapper interferes with token parsing, and ONLY after confirming the network isolation in §5.1

**Recommendation:** Keep the safety wrapper ON. Parse the structured content within it. This is defense in depth — even if the JIT bot is compromised and sends malicious instructions, the wrapper tells the agent to treat it as untrusted.

### 5.5 Threat: Denial of Service on Webhook

**Attack:** Flood `/hooks/agent` to exhaust OpenClaw's capacity.

**Mitigations:**
- Bearer token requirement blocks unauthenticated floods
- OpenClaw likely has its own rate limiting on hook endpoints
- Network restriction (§5.1) limits attack surface to the JIT bot container

### 5.6 Token Storage Summary

| Secret | Stored In | Accessed By | How |
|--------|-----------|-------------|-----|
| OpenClaw webhook token | Vault `secret/data/services/openclaw/webhook_token` | JIT approval bot | AppRole auth at startup, held in process memory |
| Ed25519 signing key | Vault `secret/data/services/jit-approval/signing_key` | JIT approval bot | AppRole auth at startup |
| Ed25519 public key | OpenClaw workspace file or env var | Agent | Read at startup (public, not secret) |
| JIT bot API key | Vault `secret/data/services/jit-approval/api_key` | Agent (Prometheus) | Tier 1 auto-read from Vault |

---

## 6. Data Flow

### JIT Request (Agent → JIT Bot)

```json
{
  "requester": "prometheus",
  "resource": "ssh:router",
  "tier": 2,
  "reason": "Fix iptables rule for Tailscale DNS",
  "ttl_minutes": 15,
  "callback_session_key": "agent:main:subagent:59db4167-ec03-433b-9df4-a0c912be7292",
  "callback_url": "http://moltbot:18789/hooks/agent"
}
```

**New fields:** `callback_session_key`, `callback_url` (optional — can be config instead).

### Webhook Callback (JIT Bot → OpenClaw)

#### On Approval:
```json
{
  "message": "JIT_CALLBACK request_id=req-a1b2c3 status=approved resource=ssh:router token=hvs.CAESI... expires_at=2026-02-12T17:00:00Z signature=base64... nonce=random123 granted_at=2026-02-12T16:45:00Z",
  "sessionKey": "agent:main:subagent:59db4167-ec03-433b-9df4-a0c912be7292",
  "name": "JIT",
  "deliver": false
}
```

#### On Denial:
```json
{
  "message": "JIT_CALLBACK request_id=req-a1b2c3 status=denied resource=ssh:router",
  "sessionKey": "agent:main:subagent:59db4167-ec03-433b-9df4-a0c912be7292",
  "name": "JIT",
  "deliver": false
}
```

#### On Timeout:
```json
{
  "message": "JIT_CALLBACK request_id=req-a1b2c3 status=expired resource=ssh:router",
  "sessionKey": "...",
  "name": "JIT",
  "deliver": false
}
```

**Why `deliver: false`?** The approval result is for the agent, not for Noah. Noah already saw the result on Telegram.

**Why structured key=value in message?** Easier for the agent to parse reliably than prose. The `JIT_CALLBACK` prefix lets the agent immediately identify these messages.

---

## 7. OpenClaw Config Changes

```jsonc
{
  "hooks": {
    "enabled": true,
    "token": "<from-vault>",  // shared secret, rotatable
    "path": "/hooks",
    "allowedAgentIds": ["hooks", "main"]
  }
}
```

**Changes needed:**
- Ensure `hooks.enabled: true` (may already be set)
- Generate and store a dedicated webhook token in Vault
- Confirm the OpenClaw gateway is listening on a port reachable from the JIT bot container (e.g., `moltbot:18789` on the internal Docker network)

**No custom mappings needed.** We use `/hooks/agent` directly.

---

## 8. JIT Approval Bot Code Changes

### New: Webhook notification function

Add to `internal/telegram/` or a new `internal/webhook/` package:

```go
// internal/webhook/client.go
type Client struct {
    baseURL string
    token   string
    http    *http.Client
}

func (c *Client) NotifyApproval(ctx context.Context, req NotifyRequest) error {
    payload := map[string]interface{}{
        "message":    formatCallbackMessage(req),
        "sessionKey": req.CallbackSessionKey,
        "name":       "JIT",
        "deliver":    false,
    }
    // POST to baseURL + "/hooks/agent" with Bearer token
    // Retry with backoff on 5xx, fail on 4xx
}
```

### Modified functions

**`EditMessageApproved`** — after editing Telegram message, also call `webhook.NotifyApproval()`:

```go
func (h *Handler) onApprove(requestID string) {
    // 1. Mint Vault token (existing)
    token := h.mintToken(request)
    
    // 2. Sign the grant (existing from §7.1)
    signature := h.signGrant(request, token)
    
    // 3. Edit Telegram message (existing)
    h.telegram.EditMessageApproved(msgID, requestID, resource)
    
    // 4. NEW: Push webhook to OpenClaw
    err := h.webhook.NotifyApproval(ctx, webhook.NotifyRequest{
        RequestID:          requestID,
        Status:             "approved",
        Resource:           resource,
        Token:              token,
        ExpiresAt:          expiresAt,
        Signature:          signature,
        Nonce:              nonce,
        GrantedAt:          grantedAt,
        CallbackSessionKey: request.CallbackSessionKey,
    })
    if err != nil {
        // Log error, but don't fail — agent can still fall back to polling
        logger.Error("webhook_notify_failed", logger.Fields{"error": err})
    }
}
```

**Same pattern for `EditMessageDenied` and `EditMessageTimeout`.**

### New: Request schema changes

Add `callback_session_key` and `callback_url` fields to the request model:

```go
type Request struct {
    // ... existing fields ...
    CallbackSessionKey string `json:"callback_session_key,omitempty"`
    CallbackURL        string `json:"callback_url,omitempty"`
}
```

### New: Vault secret reads at startup

```go
func (s *Service) loadWebhookConfig() error {
    // Read webhook token from Vault
    secret, err := s.vault.Logical().Read("secret/data/services/openclaw/webhook_token")
    // Store in s.webhookToken (process memory only)
}
```

---

## 9. Agent-Side Changes

### Current flow (jit-lib.sh)

```bash
jit_request() {
    REQ_ID=$(curl -s -X POST .../request -d '...' | jq -r '.request_id')
    # Poll loop for 5 minutes
    for i in $(seq 1 60); do
        STATUS=$(curl -s .../request/$REQ_ID | jq -r '.status')
        case $STATUS in
            approved) ... ;;
            denied) ... ;;
        esac
        sleep 5
    done
}
```

### New flow

```bash
jit_request() {
    local session_key="${OPENCLAW_SESSION_KEY:-unknown}"
    
    REQ_ID=$(curl -s -X POST .../request \
        -d "{
            \"requester\": \"prometheus\",
            \"resource\": \"$resource\",
            \"reason\": \"$reason\",
            \"ttl_minutes\": $ttl,
            \"callback_session_key\": \"$session_key\"
        }" | jq -r '.request_id')
    
    echo "JIT request $REQ_ID submitted. Approval will arrive via webhook callback."
    echo "REQUEST_ID=$REQ_ID"
    # No polling loop. Sub-agent suspends here.
    # The webhook callback will resume this session with the result.
}
```

The sub-agent's flow becomes:
1. Call `jit_request` — submits request, returns immediately
2. Report to main session: "Request submitted, waiting for callback"
3. Session suspends (sub-agent turn ends)
4. Webhook fires → session resumes with approval/denial message
5. Sub-agent parses `JIT_CALLBACK` message, extracts token, continues work

### Callback parsing (agent-side)

```bash
# When the session resumes from webhook callback:
parse_jit_callback() {
    local msg="$1"
    if [[ "$msg" == JIT_CALLBACK* ]]; then
        local status=$(echo "$msg" | grep -oP 'status=\K\w+')
        local token=$(echo "$msg" | grep -oP 'token=\K\S+')
        local signature=$(echo "$msg" | grep -oP 'signature=\K\S+')
        # Verify signature before using token
        # ...
    fi
}
```

In practice, the LLM agent will parse this from the message text directly — no bash function needed. The structured format (`key=value`) makes this reliable.

---

## 10. Failure Modes

| Failure | Impact | Recovery |
|---------|--------|----------|
| **Webhook unreachable** (OpenClaw down) | JIT bot logs error; approval still recorded in SQLite and Telegram | Agent can fall back to polling as backup |
| **Invalid webhook token** | 401 from OpenClaw; JIT bot logs error | Check Vault secret, rotate token |
| **Session expired/GC'd** | OpenClaw creates new session with that key; agent turn runs without prior context | Message contains full context (request ID, resource, token) — works standalone |
| **JIT bot crashes after Telegram approve, before webhook** | On restart, crash recovery (§10.4 of JIT design) picks up approved-but-not-notified requests and re-fires webhooks |
| **Duplicate webhook delivery** | Agent receives callback twice; second time request ID is already `claimed` | Agent ignores duplicate (idempotent by request ID) |
| **Network partition** (containers can't reach each other) | Both webhook and polling fail | Agent reports failure, waits for network recovery |
| **Webhook token rotated, JIT bot has stale token** | 401 on webhook calls | JIT bot should re-read token from Vault on 401 (self-healing) |

### Fallback: Hybrid Polling + Webhook

During migration, keep polling as a fallback:

```bash
jit_request() {
    # Submit request with callback info
    REQ_ID=$(curl -s -X POST .../request -d '...' | jq -r '.request_id')
    
    # Light polling as fallback (every 30s instead of 5s, only 3 attempts)
    for i in 1 2 3; do
        sleep 30
        STATUS=$(curl -s .../request/$REQ_ID | jq -r '.status')
        [[ "$STATUS" == "approved" || "$STATUS" == "denied" ]] && break
    done
    # If still pending after 90s, rely entirely on webhook callback
}
```

This provides resilience during the transition period without the cost of aggressive polling.

---

## 11. Migration Path

### Phase 1: Webhook Infrastructure (no behavior change)

1. Store OpenClaw webhook token in Vault
2. Add `callback_session_key` and `callback_url` fields to JIT request schema (optional, ignored if empty)
3. Deploy JIT bot with webhook client code (sends webhooks when callback fields present)
4. Enable OpenClaw hooks if not already enabled
5. **Test:** Manually curl `/hooks/agent` from JIT bot container to confirm connectivity

### Phase 2: Agent Sends Callback Info

1. Update `jit-lib.sh` to include `callback_session_key` in requests
2. JIT bot starts sending webhook notifications on resolve
3. **Keep polling active** as fallback (hybrid mode)
4. Monitor: confirm webhooks are arriving, sessions resuming correctly

### Phase 3: Drop Polling

1. Remove polling loop from `jit-lib.sh`
2. Sub-agent flow becomes fully event-driven
3. Monitor for 1 week for any missed callbacks
4. Remove `GET /api/v1/request/:id` token-delivery functionality (keep for status checks only)

### Backward Compatibility

- Requests without `callback_session_key` work exactly as before (polling)
- JIT bot only sends webhooks when callback info is present
- No breaking changes to the API

---

## 12. Recommendation Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Endpoint | `/hooks/agent` | Direct session targeting, isolated execution |
| Session targeting | Option B (direct sessionKey) | No routing logic needed in main session |
| Token delivery | In webhook message body | Simpler than separate fetch; signed for integrity |
| Security baseline | Bearer token + network isolation + Ed25519 signatures | Defense in depth; any single layer failing is survivable |
| Webhook token storage | Vault, read at startup, process memory only | Consistent with JIT design philosophy |
| Fallback | Hybrid polling during migration | Zero-risk rollout |
| Safety wrapper | Keep ON | JIT bot messages are structured; wrapper doesn't hurt and helps if bot is compromised |

---

## Appendix A: Webhook Payload Examples

### Full approval callback (as sent by JIT bot):

```bash
curl -X POST http://moltbot:18789/hooks/agent \
  -H 'Authorization: Bearer <webhook-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "JIT_CALLBACK request_id=req-a1b2c3 status=approved resource=ssh:router token=hvs.CAESIBxk... expires_at=2026-02-12T17:00:00Z nonce=f8a2c1 granted_at=2026-02-12T16:45:00Z signature=base64...",
    "sessionKey": "agent:main:subagent:59db4167-ec03-433b-9df4-a0c912be7292",
    "name": "JIT",
    "deliver": false,
    "timeoutSeconds": 120
  }'
```

### Full denial callback:

```bash
curl -X POST http://moltbot:18789/hooks/agent \
  -H 'Authorization: Bearer <webhook-token>' \
  -H 'Content-Type: application/json' \
  -d '{
    "message": "JIT_CALLBACK request_id=req-a1b2c3 status=denied resource=ssh:router reason=not_needed_right_now",
    "sessionKey": "agent:main:subagent:59db4167-ec03-433b-9df4-a0c912be7292",
    "name": "JIT",
    "deliver": false
  }'
```
