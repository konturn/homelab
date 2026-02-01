# Voice Call Setup Research for Moltbot
**Date:** 2026-02-01
**Status:** Complete

## Executive Summary

Moltbot has a **fully-featured voice-call plugin** (`@moltbot/voice-call`) that supports outbound and inbound voice calls via Twilio, Telnyx, or Plivo. The plugin integrates with the core TTS system (ElevenLabs or OpenAI) for streaming speech.

**Recommended Stack:** Twilio + OpenAI TTS (or ElevenLabs if quality is paramount)

---

## 1. Moltbot Voice-Call Plugin

**Location:** `/app/docs/plugins/voice-call.md`

### Supported Providers
- **Twilio** (Programmable Voice + Media Streams) ✅ Best documented
- **Telnyx** (Call Control v2)
- **Plivo** (Voice API + XML transfer + GetInput speech)
- **mock** (dev/testing)

### Features
- **Outbound calls** (notifications or conversations)
- **Inbound calls** with allowlist policy
- **Streaming TTS** via ElevenLabs or OpenAI
- **Agent tool** (`voice_call`) for programmatic access
- **CLI** (`moltbot voicecall call/continue/speak/end/status/tail`)

### Requirements
- Publicly reachable webhook URL (ngrok, Tailscale funnel, or static domain)
- TTS provider API key (ElevenLabs or OpenAI)
- Phone provider credentials

### Config Example
```json5
{
  plugins: {
    entries: {
      "voice-call": {
        enabled: true,
        config: {
          provider: "twilio",
          fromNumber: "+15550001234",
          toNumber: "+15550005678",
          twilio: {
            accountSid: "ACxxxxxxxx",
            authToken: "..."
          },
          serve: { port: 3334, path: "/voice/webhook" },
          // publicUrl: "https://example.ngrok.app/voice/webhook",
          streaming: { enabled: true, streamPath: "/voice/stream" }
        }
      }
    }
  }
}
```

---

## 2. TTS Options Comparison

| Provider | Quality | Latency | Cost | Notes |
|----------|---------|---------|------|-------|
| **ElevenLabs** | Excellent (most natural) | ~300-500ms TTFB | ~$0.30/1K chars ($5/mo starter) | Best quality, multilingual v2 model, voice cloning |
| **OpenAI TTS** | Very Good | ~200-400ms TTFB | $15/1M input chars (~$0.015/1K) | 13 voices, promptable for tone/accent, gpt-4o-mini-tts model |
| **Edge TTS** | Good | ~200ms | FREE | **NOT SUPPORTED for telephony** (PCM output unreliable) |

### Recommendation
- **For telephony/calls:** Use OpenAI TTS or ElevenLabs only (Edge TTS explicitly unsupported)
- **Best quality:** ElevenLabs (especially eleven_v3 or eleven_multilingual_v2)
- **Best value:** OpenAI TTS (10-20x cheaper than ElevenLabs)
- **Latency-critical:** OpenAI TTS with pcm/wav output format

### TTS Cost Estimate (casual use)
- 10 mins of conversation/day = ~1,500 words = ~10,000 chars
- **ElevenLabs:** ~$3/month
- **OpenAI TTS:** ~$0.15/month

---

## 3. Phone Provider Comparison

| Provider | Per-Min (Outbound US) | Per-Min (Inbound) | Number Cost | Ease of Setup |
|----------|----------------------|-------------------|-------------|---------------|
| **Twilio** | ~$0.014/min | ~$0.0085/min | $1.15/mo local | ⭐⭐⭐⭐⭐ Best docs, widest adoption |
| **Telnyx** | ~$0.007/min | ~$0.007/min | $1.00/mo | ⭐⭐⭐⭐ Cheaper, good quality |
| **Plivo** | ~$0.009/min | ~$0.0055/min | $0.80/mo | ⭐⭐⭐ Less popular for AI agents |

### Per-Provider Notes

**Twilio:**
- Most mature, best documentation
- ConversationRelay feature for voice AI
- Media Streams API for real-time audio
- Wide regional availability
- Higher price but extremely reliable

**Telnyx:**
- ~50% cheaper than Twilio
- Call Control v2 API
- Strong WebRTC/SIP support
- Good for cost-sensitive production

**Plivo:**
- Cheapest option
- Now pivoting to AI Agents (no-code builder)
- XML-based call control
- Less community support for agent integrations

### Recommendation
**Twilio** for initial setup (best docs, Moltbot plugin well-tested). Switch to Telnyx later if costs become a concern.

---

## 4. End-to-End Latency Expectations

| Component | Latency |
|-----------|---------|
| Speech-to-Text (user input) | 200-500ms |
| Agent/LLM response | 500-2000ms (model dependent) |
| TTS generation | 200-500ms |
| Audio streaming to phone | 50-100ms |
| **Total round-trip** | **~1-3 seconds** |

For real-time conversation feel, target <2 seconds. This is achievable with:
- Fast model (gpt-4o-mini or Claude Haiku)
- OpenAI TTS (low latency mode)
- Streaming enabled

---

## 5. Moltbook Community Search

**Result:** No posts found about voice calls or phone integration. This appears to be a less-explored feature in the community.

---

## 6. Recommended Setup

### Stack
- **Phone Provider:** Twilio (easiest start)
- **TTS:** OpenAI TTS (cost-effective, low latency)
- **Model:** gpt-4o-mini or Claude Haiku (fast responses)

### Monthly Cost Estimate (Casual Use: ~30 mins calls/month)
| Item | Cost |
|------|------|
| Twilio phone number | $1.15 |
| Twilio outbound minutes (30 min) | $0.42 |
| Twilio inbound minutes (30 min) | $0.26 |
| OpenAI TTS (~30K chars) | $0.45 |
| LLM costs (separate) | varies |
| **Total** | **~$2.50/month** |

With ElevenLabs instead of OpenAI TTS, add ~$3-5/month.

### Setup Steps

1. **Install plugin:**
   ```bash
   moltbot plugins install @moltbot/voice-call
   moltbot gateway restart
   ```

2. **Get Twilio credentials:**
   - Sign up at twilio.com
   - Get Account SID + Auth Token
   - Buy a phone number ($1.15/mo)

3. **Configure webhook exposure** (pick one):
   - ngrok: `ngrok http 3334`
   - Tailscale funnel: `moltbot voicecall expose --mode funnel`
   - Static domain: set `publicUrl` directly

4. **Add config to moltbot.json:**
   ```json5
   {
     plugins: {
       entries: {
         "voice-call": {
           enabled: true,
           config: {
             provider: "twilio",
             fromNumber: "+1XXXXXXXXXX",
             twilio: {
               accountSid: "ACxxxxxxxx",
               authToken: "xxxxx"
             },
             serve: { port: 3334, path: "/voice/webhook" },
             publicUrl: "https://your-domain/voice/webhook",
             streaming: { enabled: true },
             tts: {
               provider: "openai",
               openai: { voice: "alloy" }
             }
           }
         }
       }
     }
   }
   ```

5. **Test:**
   ```bash
   moltbot voicecall call --to "+15555550123" --message "Hello from Moltbot"
   ```

---

## 7. Alternative: Talk Mode (No Phone Required)

Moltbot already has **Talk mode** for continuous voice conversations via:
- macOS/iOS/Android apps
- Uses microphone → Speech recognition → Agent → ElevenLabs TTS → Speaker
- No phone costs, just ElevenLabs API

If phone calls aren't strictly required, Talk mode is simpler and cheaper.

---

## Next Steps

1. Decide: Phone calls vs Talk mode?
2. If phone calls: Create Twilio account
3. Set up ngrok or Tailscale funnel for webhook
4. Install and configure voice-call plugin
5. Test with a quick outbound call
