# Telegram adaptor — LIVE

Bot API over plain HTTPS. Easiest channel on the list.

## Setup

1. Message `@BotFather` → `/newbot` → copy the token.
2. `TELEGRAM_BOT_TOKEN=<token>` in `llm_gatewayV9/.env`, restart gateway.
3. Optional: `TELEGRAM_CHAT_ID=<your-numeric-id>` (from `@userinfobot`).

## Verify

```bash
curl -s http://localhost:8109/v1/channels | python3 -c \
  "import json,sys; print([c for c in json.load(sys.stdin)['channels'] if c['name']=='telegram'])"
# configured:true → live
curl -s http://localhost:8109/v1/channels/telegram/send \
  -H 'Content-Type: application/json' -d '{"to":"0","text":"probe"}'
# ok:false + "chat not found" proves key+path with zero delivery.
# Real send: use your numeric chat id as "to".
```

## Gotchas

- Error strings never echo the token (request URL carries it; scrubbed).
- 401 = revoked token → regenerate via @BotFather.
- 4096-char message cap enforced client-side.
