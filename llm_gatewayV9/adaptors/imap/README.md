# IMAP/SMTP adaptor — LIVE send + poll (P2)

RFC-standard fallback for mailboxes without Gmail OAuth. App passwords,
IMAP IDLE (or polling) inbound, SMTP submission outbound.

## Setup (when integrating)

1. `IMAP_HOST/USER/PASS` + `SMTP_HOST/USER/PASS` (app passwords, not the
   login password, for Gmail/iCloud/etc).
2. Prefer IDLE over polling; fall back to polling where IDLE is missing.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "imap or registry"
```

## Gotchas

- Attachment encoding per RFC 2045/2231 — verify against real clients.
- Threading on `References`/`In-Reply-To`, not subject lines.
