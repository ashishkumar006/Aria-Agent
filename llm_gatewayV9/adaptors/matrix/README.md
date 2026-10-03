# Matrix adaptor — SEND + SYNC-POLL LIVE

Client-Server API. Homeserver choice matters (federation/retention/trust).

## Setup (when integrating)

1. `MATRIX_HOMESERVER` (e.g. `https://matrix.org`), `MATRIX_USER`, `MATRIX_PASSWORD`.
2. Login once, persist the access token server-side (never log it).
3. `/sync` long-poll for inbound; `PUT .../send/m.room.message/{txnId}` outbound.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "matrix or registry"
```

## Gotchas

- Threading is `m.relates_to.event_id`; fall back to `room_id`.
- `origin_server_ts` is millis — the adapter converts to seconds.
