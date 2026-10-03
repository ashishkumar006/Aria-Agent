# WhatsApp via Twilio adaptor — SEND LIVE, recommended first path

Same API and signature scheme as SMS, with `whatsapp:`-prefixed
addresses. Sandbox opt-in per test recipient; trial credits suffice.

## Setup (when integrating)

1. Twilio console → WhatsApp sandbox → join code from each test phone.
2. `TWILIO_SID/AUTH` + `WHATSAPP_FROM=whatsapp:+...` + `TWILIO_WEBHOOK_URL`.
3. Outside the 24h window: template messages (`ContentSid`) only.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "whatsapp or twilio or registry"
# prefix strip, shared HMAC, cost hints covered.
```
