# Slack adaptor — PARTIAL (send live, events in P2)

Outbound `chat.postMessage` works today. Inbound Events API waits on a
public HTTPS deployment (plus the OAuth scope maze).

## Setup

1. api.slack.com → app → `SLACK_BOT_TOKEN` (needs `chat:write`).
2. Later, for inbound: `SLACK_SIGNING_SECRET` + event subscriptions pointing
   at `POST /v1/hooks/slack` on a public URL.

## Verify (today)

```bash
# Events handshake needs no secret and always works:
curl -s http://localhost:8109/v1/hooks/slack \
  -H 'Content-Type: application/json' \
  -d '{"type":"url_verification","challenge":"XYZ"}'
# expect: XYZ
# Send path is unit-covered (NotConfigured without token).
```

## Gotchas

- `chat.postMessage` with a user id (`U...`) fails — resolve the channel first.
- `thread_ts` routes into threads; pass it through for replies.
- Signature check includes a 5-minute replay window.
