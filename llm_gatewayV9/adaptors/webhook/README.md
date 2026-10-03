# Generic webhook adaptor — LIVE (keyless, HMAC optional)

HTTP in, HTTP out. The simplest adapter; the hardest to test.

## Setup

1. Optional: `WEBHOOK_SECRET` (shared HMAC secret, checked against
   `X-Signature` / `X-Hub-Signature-256` / `X-Webhook-Signature`).
2. Optional: `WEBHOOK_OUT_URL` (enables outbound delivery).
3. Without a secret the endpoint is open — everything arrives `untrusted`.

## Verify

```bash
curl -s http://localhost:8109/v1/hooks/webhook \
  -H 'Content-Type: application/json' -d '{"sender_id":"me","text":"ping"}'
# expect: normalized envelope back, trust untrusted.
```

## Gotchas

- Accept several common payload layouts (`text`/`message`, `msg_id`/`id`).
- Outbound without `WEBHOOK_OUT_URL` returns not-configured, never raises.
