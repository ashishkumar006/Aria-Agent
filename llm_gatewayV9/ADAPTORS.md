# Channel Adaptors — setup + verification guide

All 16 messaging surfaces live behind one contract (`adaptors/base.py`).
Each channel is a package (`adaptors/<name>/` with `adapter.py`,
`schemas.py`, plus `verify.py`/`README.md` where warranted).
The gateway owns every secret (`llm_gatewayV9/.env`, never committed);
the agent only sends `to` + `text`. Nothing below delivers a real message
during verification unless the step says so — every check is designed to
prove the wiring **without spamming anyone**.

Start the gateway first (`cd llm_gatewayV9 && uv run python main.py`,
port 8109), then in a second shell use the commands in each section.

> **Fast path (no servers):** every adaptor package carries its own
> fundamental suite — `uv run pytest adaptors/<name> -q` exercises the
> contract, schemas, normalize fixtures, webhook verification, and both
> send paths (unconfigured → `NotConfigured`, configured → mocked
> transport) offline. `uv run pytest adaptors tests/test_adaptor_contract.py -q`
> runs all 16 plus the registry-wide contract (125 tests, <1s). The curl
> flows below are for LIVE credential verification, not code correctness.

General checks that apply to every channel:

```bash
# 1. Is it registered and what does it need?
curl -s http://localhost:8109/v1/channels | python3 -m json.tool
# 2. Are MY keys seen (names only, never values)?
uv run python check_keys.py
# 3. Send test (replace CH, TO, TEXT). Unconfigured -> ok:false, never a crash:
curl -s http://localhost:8109/v1/channels/CH/send \
  -H "Content-Type: application/json" \
  -d '{"to":"TO","text":"TEXT"}'
```

Status words you'll see: `live` (keys present), `scaffold` (code ready,
keys/live wiring pending), `error` (broken import — report it).

---

## 1. Telegram — LIVE ✅ (verified: key works, bad chat rejected, no delivery)

- **Get the key:** Telegram → `@BotFather` → `/newbot` → token.
- **Set:** `TELEGRAM_BOT_TOKEN=<token>` in `llm_gatewayV9/.env`, restart gateway.
- **Verify:**
  ```bash
  curl -s http://localhost:8109/v1/channels | python3 -c \
    "import json,sys; print([c for c in json.load(sys.stdin)['channels'] if c['name']=='telegram'])"
  # expect configured:true
  curl -s http://localhost:8109/v1/channels/telegram/send \
    -H "Content-Type: application/json" -d '{"to":"0","text":"probe"}'
  # expect ok:false + "chat not found" — PROVES the key + path work with zero delivery.
  # Real send: use your numeric chat id from @userinfobot as "to".
  ```
- **Troubleshooting:** `401 Unauthorized` from Telegram = revoked/regenerated token — make a new one via @BotFather. Errors never echo the token (scrubbed).

## 2. Gmail — LIVE, token EXPIRED ⚠️ (action needed, see below)

- **Get the keys:** Google Cloud → OAuth client (Desktop) → `GMAIL_CLIENT_ID` /
  `GMAIL_CLIENT_SECRET` in gateway `.env`, then run the consent flow once:
  ```bash
  cd llm_gatewayV9 && uv run python gmail_oauth_setup.py
  ```
  It writes `GMAIL_TOKEN` + `GMAIL_REFRESH_TOKEN` to the gateway `.env`.
- **Verify:**
  ```bash
  curl -s http://localhost:8109/v1/integrations/gmail/query \
    -H "Content-Type: application/json" \
    -d '{"args":{"api_method":"list","query":"is:unread","max_results":1}}'
  # expect ok:true + message_ids (read-only, sends nothing).
  ```
- **Right now:** the stored access token returns 401 and the refresh triple
  is rejected by Google (400) — re-run `gmail_oauth_setup.py` to mint a
  fresh triple. After that, `gmail_refresh_token()` (MCP tool) renews hourly
  automatically.

## 3. GitHub — LIVE ✅ (verified: listed 20 repos)

- **Get the key:** github.com → Settings → Developer settings → personal access
  token (`repo`, `read:org`) → `GITHUB_TOKEN`.
- **Verify:** `.../github/query` with `{"args":{"api_method":"list_repos"}}` →
  expect `ok:true` + repo list (read-only).

## 4. Web search (Tavily + DDG) — LIVE ✅ (verified: 1 live result)

- **Key (optional):** tavily.com → `TAVILY_API_KEY`. Without it, DuckDuckGo
  fallback runs keyless. Monthly cap (950) is enforced gateway-side.
- **Verify:** `.../websearch/search` with `{"args":{"query":"...","max_results":1}}`.

## 5–6. Slack / Notion — code live, keys NOT set (fail-soft verified)

- **Slack:** api.slack.com → bot token (`chat:write`) → `SLACK_BOT_TOKEN`.
  Events API (inbound) additionally needs a public HTTPS URL + `SLACK_SIGNING_SECRET`.
  Verify send like Telegram; verify the Events handshake any time with no secret:
  ```bash
  curl -s http://localhost:8109/v1/hooks/slack \
    -H "Content-Type: application/json" \
    -d '{"type":"url_verification","challenge":"XYZ"}'
  # expect: XYZ
  ```
- **Notion:** notion.so/my-integrations → internal secret (`secret_...`) →
  `NOTION_TOKEN`. Verify: `.../notion/query` `list_pages` → currently
  `ok:false NOTION_TOKEN not set` (correct fail-soft).

## 8. Google Calendar — code live, key NOT set

- Google Cloud OAuth token with `calendar.events` scope → `GOOGLE_CALENDAR_TOKEN`.
- Currently `ok:false` fail-soft (correct). Same shape as Gmail setup.

## 9. Generic webhook — LIVE (keyless, HMAC optional)

- Optional `WEBHOOK_SECRET` (HMAC-SHA256, `X-Signature`/`X-Hub-Signature-256`
  headers) and `WEBHOOK_OUT_URL` for outbound delivery.
- Verify HMAC offline: `tests/test_adaptors.py` covers it; live, POST any JSON
  to `/v1/hooks/webhook` → normalized envelope back, trust `untrusted`.

## 10. WebUI + Local mic — LIVE (local trust, no keys)

- WebUI chat and mic transcripts normalize to envelopes with owner/paired
  trust. Nothing to configure.

## 11–12. Discord / Matrix — SEND LIVE (inbound listeners are deployment)

- **Discord:** developer portal → Bot → `DISCORD_BOT_TOKEN`; invite with
  `applications.commands`. Send = REST `/channels/{id}/messages`
  (+ `message_reference` threads). Inbound Gateway WS listener stays a
  deployment concern. Live = send to a test channel.
- **Matrix:** homeserver + `MATRIX_HOMESERVER/MATRIX_USER/MATRIX_PASSWORD`;
  login→cached token (re-login on 401), send PUT, plus `sync_once()`
  one-shot poll for operator loops. No permanent sync daemon yet.

## 13. LINE — SEND LIVE (verifier was already real)

- LINE Developers → `LINE_CHANNEL_SECRET` + `LINE_ACCESS_TOKEN`.
- Push live; pass `thread_id="reply:<token>"` to answer quota-free via the
  reply endpoint. Webhook needs the public URL deployment.

## 14. Signal — SEND + POLL LIVE (needs `signal-cli` binary + QR pairing)

- Install `signal-cli`, `signal-cli link` (scan QR in-app),
  `SIGNAL_NUMBER=+...`, `SIGNAL_DATA_DIR=<keys dir>`.
- `send()` is one-shot subprocess; `poll()` returns raw JSON envelopes for
  operator loops (normalize each). No daemon required.

## 15. IMAP/SMTP — SEND + POLL LIVE

- `IMAP_HOST/USER/PASS` + `SMTP_HOST/USER/PASS` (+ optional ports).
  App passwords, not login passwords. `poll()` returns parsed headers for
  `normalize()`; threading left to References headers, not subjects.

## 16–17. Twilio SMS / WhatsApp-Twilio — SEND LIVE (recommended WhatsApp path)

- Console → `TWILIO_SID/AUTH/FROM` (+ `WHATSAPP_FROM`, `TWILIO_WEBHOOK_URL`).
- Signature verifier is real. WhatsApp supports `ContentSid` templates for
  closed 24h windows; sandbox needs each test recipient to opt in —
  trial credits suffice.

## 18–19. Teams / WhatsApp-Meta / Twilio Voice — SEND LIVE (heaviest setup)

- **Teams:** Azure app registration first (`TEAMS_APP_ID/PASSWORD/TENANT`);
  token cached in-process; pass `service_url` from the inbound activity.
- **WhatsApp-Meta:** Meta App Dashboard → WhatsApp use-case → business
  portfolio → system user (`whatsapp_business_messaging` +
  `whatsapp_business_management`) → permanent token. Text + template sends
  live; thread replies via `thread_id`. Verify handshake today:
  ```bash
  curl -s "http://localhost:8109/v1/hooks/whatsapp_meta?hub.mode=subscribe&hub.verify_token=WRONG&hub.challenge=CH"
  # expect: 403. With the real WA_VERIFY_TOKEN: echoes CH.
  ```
  Business verification takes days — start it early.
- **Twilio Voice:** numbers + optional `VOICE_WS_URL`; call creation speaks
  `text` via TwiML `<Say>`, or connects `<Stream>` for media. Per-minute
  cost hint recorded.

## Policy, spend, control (already live, dry-run)

```bash
curl -s -X POST http://localhost:8109/v1/policy/evaluate \
  -H "Content-Type: application/json" -d '{"trust":"untrusted","tool":"send_telegram"}'
# -> action deny, dry_run true (reported, still allowed until armed)
curl -s http://localhost:8109/v1/spend | python3 -m json.tool     # unified ledger
curl -s http://localhost:8109/v1/control/presence                 # gateway vitals
curl -s -X POST http://localhost:8109/v1/control/pair \
  -H "Content-Type: application/json" \
  -d '{"channel":"telegram","sender_id":"<your-numeric-id>","role":"owner"}'
# pairs YOU as owner (loopback only). Find your id via @userinfobot.
```

Arming later = set `enforce: true` per rule in `policy/policy.yaml`
(hot-reloads, no restart). The kill switch stays disarmed unless you
export `ARIA_ENABLE_KILL=1`.

## Troubleshooting matrix

| Symptom | Cause | Fix |
|---|---|---|
| `ok:false ... not configured` | key missing in gateway `.env` (or gateway not restarted after adding) | add key → restart gateway |
| `401` from Gmail | hourly token expiry | MCP `gmail_refresh_token()`; if 400, re-run `gmail_oauth_setup.py` |
| `chat not found` (Telegram) | bad chat id | use numeric id from @userinfobot |
| `url_verification` 403 (Slack) | — | fixed: challenge echoes without signature now |
| Token-looking string in any error/ledger | leak — report immediately | scrubber covers bot tokens, key=value, Bearer |
| `501 ... not integrated yet` | scaffold channel, correct behavior | wire per its file's P1/P2/P5 mark |
