# LINE adaptor — SEND LIVE (push + reply; webhook needs public URL)

Messaging API webhook. The signature verifier (`verify.py`) is real and
tested; the push sender wires up in P1.

## Setup (when integrating)

1. LINE Developers → channel → `LINE_CHANNEL_SECRET` + `LINE_ACCESS_TOKEN`.
2. Webhook URL → `POST /v1/hooks/line`.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "line or registry"
# LINE adaptor — SEND LIVE (push + reply; webhook needs public URL)
```

## Gotchas

- Prefer the quota-free `replyToken` for replies when available.
