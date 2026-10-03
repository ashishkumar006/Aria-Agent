# Twilio Voice adaptor — CALL CREATION LIVE (media loop separate)

Calls + media streams: sub-second latency budget, mulaw/8kHz codecs,
barge-in, hold behavior, WebSocket audio in/out.

## Setup (when integrating)

1. Twilio number with voice + `TWILIO_VOICE_FROM`.
2. `VOICE_WS_URL` (wss media endpoint) + TwiML `<Stream>` greeting flow.
3. Transcription callback → normalized text envelopes.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "twilio or registry"
```

## Gotchas

- Trial credits cover dev; production needs compliance.
- Concurrent calls need per-`streamSid` registries so audio doesn't clobber.
