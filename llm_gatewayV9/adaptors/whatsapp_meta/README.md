# WhatsApp Meta Cloud API adaptor — SEND LIVE (webhook needs verification)

Full WhatsApp path, but the ceremony takes days: business portfolio,
system user, permanent token, webhook handshake, App Review for partners.

## Setup (when integrating)

1. App Dashboard → WhatsApp use-case → portfolio → Business account + number.
2. System user + `whatsapp_business_messaging` + `whatsapp_business_management`.
3. `WA_TOKEN` / `WA_PHONE_ID` / `WA_VERIFY_TOKEN`.
4. Webhook URL → `GET /v1/hooks/whatsapp_meta` (handshake) + `POST` (messages).

## Verify (today)

```bash
curl -s "http://localhost:8109/v1/hooks/whatsapp_meta?hub.mode=subscribe&hub.verify_token=WRONG&hub.challenge=CH"
# expect: 403. With the real token: echoes CH.
```

## Gotchas

- Non-200 responses are retried with backoff for up to 7 days — always 200 fast.
- Outside the 24h window: template messages only.
- Prefer the Twilio sandbox path first; same interface either way.
