# Adaptor Keys Guide — where to get each key + what each adaptor is for

All secrets go in `llm_gatewayV9/.env`, then restart the gateway
(`cd llm_gatewayV9 && uv run main.py`). Check names-only status anytime with
`uv run python check_keys.py` (never prints values). Gateway down = every
adaptor unreachable; keys present but server down still reads as NOT live.

Your current status (from `check_keys.py`): 5/16 live
(gmail, telegram, local_mic, webhook, webui). Rest are code-ready, unkeyed.

---

## 1. telegram — ✅ LIVE
**Useful for:** cheapest bot messaging, agent alerts + approvals + chat. Easiest channel.
**Get key:** Telegram → `@BotFather` → `/newbot` → token.
**Set:** `TELEGRAM_BOT_TOKEN=<token>` (+ optional `TELEGRAM_CHAT_ID` from `@userinfobot`).
**Verify:** `POST /v1/channels/telegram/send {"to":"0","text":"probe"}` → `ok:false chat not found` proves key with zero delivery.

## 2. gmail — ⚠️ key set, token EXPIRED
**Useful for:** passwordless email send + read (OAuth, no SMTP). Delegates to `integrations.gmail`.
**Get keys:** Google Cloud → OAuth client (Desktop) → `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`, then run once: `cd llm_gatewayV9 && uv run python gmail_oauth_setup.py` (writes `GMAIL_TOKEN` + `GMAIL_REFRESH_TOKEN`).
**Verify:** `POST /v1/integrations/gmail/query {"args":{"api_method":"list","max_results":1}}` → `ok:true`. 401 = hourly expiry (MCP `gmail_refresh_token()`); 400 = re-run setup.

## 3. discord — unkeyed
**Useful for:** community servers, gamer/dev groups, threaded announcements.
**Get key:** Discord Developer Portal → application → Bot → `DISCORD_BOT_TOKEN`. Invite bot with `applications.commands` scope (use guild commands in dev; global takes ~1h).
**Verify:** `uv run python -m pytest tests/test_adaptors.py -k "discord"`; live = send to a test channel. Inbound WS is deployment, not request-handler.

## 4. matrix — unkeyed
**Useful for:** federated/self-hosted chat (privacy orgs), bridges to other networks.
**Get keys:** pick a homeserver (e.g. `https://matrix.org`) → `MATRIX_HOMESERVER`, `MATRIX_USER`, `MATRIX_PASSWORD`. Login caches token server-side.
**Verify:** `pytest -k "matrix"`; live = send PUT + `sync_once()` poll.

## 5. line — unkeyed
**Useful for:** Japan/Taiwan/Thailand users — dominant chat app there.
**Get keys:** LINE Developers → channel → `LINE_CHANNEL_SECRET` + `LINE_ACCESS_TOKEN`. Webhook URL → `POST /v1/hooks/line`.
**Verify:** `pytest -k "line"`. Prefer quota-free `replyToken` (`thread_id="reply:<token>"`) for replies.

## 6. webhook — ✅ LIVE (keyless)
**Useful for:** catch-all HTTP input from any system (CI, scripts, IoT) + generic outbound POSTs.
**Get key:** none. Optional `WEBHOOK_SECRET` (HMAC over `X-Signature`) + `WEBHOOK_OUT_URL` (enables send). No secret = everything arrives `untrusted`.
**Verify:** `POST /v1/hooks/webhook {"sender_id":"me","text":"ping"}` → normalized envelope.

## 7. webui — ✅ LIVE (keyless)
**Useful for:** the agent's own chat page as a channel (ledger + policy see it like any channel).
**Get key:** none. Localhost = `owner`; pair others via `POST /v1/control/pair`.
**Verify:** `GET /v1/channels` → `webui configured:true`. Delivery to browsers is agent SSE; gateway `send()` only logs.

## 8. slack — unkeyed
**Useful for:** workplace teams, channel posts + threads, approvals where people already live.
**Get key:** api.slack.com → app → `SLACK_BOT_TOKEN` (`chat:write` scope). Inbound later: `SLACK_SIGNING_SECRET` + public HTTPS URL → event subscription to `POST /v1/hooks/slack`.
**Verify (no secret):** post `{"type":"url_verification","challenge":"XYZ"}` → echoes `XYZ`. Can't DM a `U...` id — resolve channel first; `thread_ts` for threads.

## 9. signal — unkeyed
**Useful for:** private/encrypted messaging to contacts who won't use Telegram/WhatsApp.
**Get keys:** install `signal-cli`, `signal-cli link` (scan QR in-app), then `SIGNAL_NUMBER=+...`, `SIGNAL_DATA_DIR=<keys dir>`. Daemonize `signal-cli -u $SIGNAL_NUMBER jsonRpc`.
**Verify:** `pytest -k "signal"`. Group threads key on `groupId`.

## 10. imap — unkeyed
**Useful for:** any non-Gmail mailbox (iCloud, Outlook, corporate) via standard protocols.
**Get keys:** mailbox provider → app passwords (NOT login password) → `IMAP_HOST/USER/PASS` + `SMTP_HOST/USER/PASS` (+ ports if non-default).
**Verify:** `pytest -k "imap"`. Threading on `References` headers, not subjects.

## 11. twilio_sms — unkeyed
**Useful for:** real SMS to any phone (2FA-style alerts, no app needed on recipient side).
**Get keys:** Twilio Console → `TWILIO_SID` / `TWILIO_AUTH` / `TWILIO_FROM` + exact `TWILIO_WEBHOOK_URL` (signature depends on it).
**Verify:** `pytest -k "twilio"`. US production needs A2P 10DLC; trial is dev-only. `verify.py` shared with voice + WA-Twilio.

## 12. whatsapp_twilio — unkeyed (recommended WhatsApp path)
**Useful for:** WhatsApp without Meta business ceremony — fastest WA route.
**Get keys:** Twilio Console → WhatsApp sandbox → join code per test phone; `TWILIO_SID`/`AUTH` + `WHATSAPP_FROM=whatsapp:+...` + `TWILIO_WEBHOOK_URL`. Outside 24h window: `ContentSid` templates only.
**Verify:** `pytest -k "whatsapp or twilio"`.

## 13. local_mic — ✅ LIVE (pointer)
**Useful for:** voice-in/voice-out loop (mic → STT → agent → TTS). No mic SDK — this package documents the split.
**Get key:** none. Transcripts arrive with `owner` trust (physical presence).
**Verify:** `GET /v1/tts/voices` → list = TTS up; STT needs faster-whisper in venv.

## 14. teams — unkeyed
**Useful for:** Microsoft-365 enterprises where Teams is the only allowed channel.
**Get keys:** Azure Portal → app registration → `TEAMS_APP_ID` / `TEAMS_APP_PASSWORD` / `TEAMS_TENANT`; bot manifest + tenant install; messaging endpoint → `/v1/hooks/teams`. Token is OAuth2 client-credentials, cached per serviceUrl.
**Verify:** `pytest -k "teams"`. Strip `<at>` mentions; replies need `replyToId` + stored serviceUrl.

## 15. whatsapp_meta — unkeyed (heaviest setup, days)
**Useful for:** official WhatsApp Business at scale (templates, high volume, verified sender).
**Get keys:** Meta App Dashboard → WhatsApp use-case → business portfolio + number → system user (`whatsapp_business_messaging` + `whatsapp_business_management`) → permanent token → `WA_TOKEN` / `WA_PHONE_ID` / `WA_VERIFY_TOKEN`. Webhook → `GET+POST /v1/hooks/whatsapp_meta`.
**Verify:** wrong token → 403; right token echoes challenge. Prefer Twilio sandbox first; same interface.

## 16. twilio_voice — unkeyed
**Useful for:** actual phone calls — agent speaks text via TwiML or streams audio over WebSocket.
**Get keys:** Twilio voice number + `TWILIO_VOICE_FROM`, `VOICE_WS_URL` (wss media endpoint), TwiML `<Stream>` greeting flow.
**Verify:** `pytest -k "twilio"`. Trial credits cover dev; concurrent calls need per-`streamSid` registries.

---

## After each key
1. Add to `llm_gatewayV9/.env`. 2. Restart gateway. 3. `GET /v1/channels` → `configured:true`. 4. `check_keys.py` confirms name SET (values never shown).
