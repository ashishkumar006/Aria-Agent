# Signal adaptor — SEND + POLL LIVE (needs signal-cli binary)

No official API exists: everything goes through a `signal-cli` daemon over
JSON-RPC. Pairing is QR-code-based.

## Setup (when integrating)

1. Install `signal-cli`, `signal-cli link` (scan the QR in-app).
2. `SIGNAL_NUMBER=+...`, `SIGNAL_DATA_DIR=<keys dir>` (secret-grade).
3. Daemonize `signal-cli -u $SIGNAL_NUMBER jsonRpc`.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "signal or registry"
```

## Gotchas

- Drop receipts/typing indicators before they reach the envelope.
- Group chat threads on `groupInfo.groupId`.
- Never log message bodies at info level.
