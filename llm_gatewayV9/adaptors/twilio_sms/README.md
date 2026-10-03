# Twilio SMS adaptor — SEND LIVE (webhook verify real; see below)

`TWILIO_SID` / `TWILIO_AUTH` / `TWILIO_FROM`, plus `TWILIO_WEBHOOK_URL`
(the exact public URL — signature verification depends on it).

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "twilio or registry"
# HMAC fixture, form shape, cost hints covered.
```

## Gotchas

- US production traffic needs A2P 10DLC registration; trial is dev-only.
- `verify.py` is shared by `whatsapp_twilio` and `twilio_voice` — one fix
  covers all three.
