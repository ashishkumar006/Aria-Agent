# Gateway credentials — what exists, how to get it, how to refresh it

Live status (never values):

```bash
cd llm_gatewayV9
uv run python key_info.py            # human board
curl -s -H "X-Gateway-Token: $(cat state/gateway.token)" \
     http://127.0.0.1:8109/v1/keys/info        # JSON
curl -s -H "X-Gateway-Token: $(cat state/gateway.token)" \
     http://127.0.0.1:8109/v1/channels/health  # live credential probe
```

**Configured: 17/26.** **Channels: 7/7 have a credential, but only 3/7 are alive**
(telegram, discord, notion OK; **slack, gmail, calendar, github fail** — see the
runbook below).

## LLM providers (all SET)

| Key | Enables | Get it from | Refresh |
|---|---|---|---|
| `GEMINI_API_KEY` (+ `GEMINI_API_KEYS` pool) | gemini / gemini35lite workers | https://aistudio.google.com/apikey | rotate in AI Studio |
| `NVIDIA_API_KEY` | nvidia worker | https://ngc.nvidia.com → API Key | rotate in NGC |
| `GROQ_API_KEY` | groq worker | https://console.groq.com/keys | rotate in console |
| `CEREBRAS_API_KEY` | cerebras worker | https://cloud.cerebras.ai | rotate in console |
| `OPEN_ROUTER_API_KEY` | openrouter worker | https://openrouter.ai/keys | rotate in console |
| `KILO_API_KEY` | kilo worker (471-model free pool) | your Kilo account | rotate in Kilo |
| `GITHUB_ACCESS_TOKEN` | github worker + integration | GitHub → Settings → Developer settings → PATs | rotate in GitHub |

> Note: only **gemini + kilo are wired as providers** today. The nvidia/groq/
> cerebras/openrouter keys exist but those providers are disabled in `LIMITS`.

## Chat / delivery channels

| Channel | Key(s) | Status | Get it from | Refresh |
|---|---|---|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` | ✅ **alive** | @BotFather → `/newbot` | BotFather `/revoke` |
| Discord | `DISCORD_BOT_TOKEN` | ✅ **alive** | Developer Portal → Bot → Reset Token | rotate in portal |
| Slack | `SLACK_BOT_TOKEN` (+ rotation triple) | ❌ `invalid_auth` | Slack app → OAuth & Permissions → xoxb | `refresh_tokens.py` (needs rotation triple) |
| Gmail | `GMAIL_TOKEN` + `GMAIL_REFRESH_TOKEN` + `GMAIL_CLIENT_ID/SECRET` | ❌ 401 / `invalid_grant` | `gmail_oauth_setup.py` | `refresh_tokens.py` |
| Calendar | `GOOGLE_CALENDAR_TOKEN` + refresh + client | ❌ 401 / `invalid_grant` | `calendar_oauth_setup.py` | `refresh_tokens.py` |
| Matrix | `MATRIX_HOMESERVER/USER/PASSWORD` | not set | your homeserver | re-login |
| Signal | `SIGNAL_NUMBER` | not set | signal-cli | re-link |
| Teams | `TEAMS_APP_ID/PASSWORD/TENANT` | not set | Azure app registration | rotate secret |
| LINE | `LINE_CHANNEL_SECRET/ACCESS_TOKEN` | not set | LINE Developers Console | rotate token |
| Twilio SMS | `TWILIO_SID/AUTH/FROM` | not set | Twilio console | rotate auth token |
| Twilio Voice | `TWILIO_VOICE_FROM/VOICE_WS_URL` | not set | Twilio console | rotate |
| WhatsApp (Meta) | `WA_TOKEN/PHONE_ID/VERIFY_TOKEN` | not set | Meta for Developers | rotate system-user token |
| IMAP/SMTP | `IMAP_*` + `SMTP_*` | not set | your mail provider | app password |
| local_mic / webhook / webui | (none) | ✅ no credential needed | — | — |

## Inbound chat (talk TO the agent, not just receive)

All channels can SEND to you. These also let you **chat back** — a message you
send becomes an agent turn and the reply returns over the same channel. A local
install has no public HTTPS URL, so each uses an in-process poller (like
Telegram's) started at gateway boot; set the `*_POLLING=off` env var to disable.

| Channel | How inbound works | What you set |
|---|---|---|
| **Telegram** | long-poll `getUpdates` (works today) | send `/start` to the bot |
| **Gmail** | polls unread INBOX mail, replies by email | `GMAIL_INBOUND_FROM=you@yourmail` (a DIFFERENT address than the agent's own mailbox; the agent's own sends are skipped so there is no loop) |
| **Slack** | reads the bot's DMs + channels | a valid `xoxb-` Bot User OAuth Token (`SLACK_BOT_TOKEN`) |

Gmail/Slack inbound feed the same trust gate as Telegram: the first sender on a
channel is paired as owner, and only owner/paired senders get a turn.

## Integrations

| Key | Enables | Get it from | Refresh |
|---|---|---|---|
| `NOTION_TOKEN` | notion_query | Notion → Settings → Integrations | rotate in Notion |
| `TAVILY_API_KEY` | web_search | https://app.tavily.com | rotate in Tavily |

## Voice

| Key | Enables |
|---|---|
| `KOKORO_MODEL`, `KOKORO_VOICES` | local TTS |

---

## Runbook — fix the broken channels

### Gmail + Calendar: `invalid_grant`
The stored refresh tokens are expired/revoked. Re-authorize (browser flow):

```bash
cd llm_gatewayV9
uv run python gmail_oauth_setup.py       # writes GMAIL_TOKEN + GMAIL_REFRESH_TOKEN
uv run python calendar_oauth_setup.py    # writes GOOGLE_CALENDAR_TOKEN + refresh
```

Then renew without a browser any time:

```bash
uv run python refresh_tokens.py
```

### GitHub: 401 Bad credentials
Rotate the PAT: GitHub → Settings → Developer settings → Personal access
tokens → (fine-grained) → new token with the scopes you need → replace
`GITHUB_ACCESS_TOKEN` in `.env`.

### Slack: `invalid_auth`
The bot token lives under **OAuth & Permissions → Bot User OAuth Token** (`xoxb-…`), **not** Basic Information (that page only has client id/secret, signing secret, verification token). Add scopes (`chat:write`, `channels:history`, …) → **Install to Workspace** → copy the `xoxb-…` into `SLACK_BOT_TOKEN`. If rotation is ON, use `SLACK_REFRESH_TOKEN` (`xoxe.x-…`) + `SLACK_CLIENT_ID` + `SLACK_CLIENT_SECRET` and run `refresh_tokens.py`.

### Anything else
Set the variable, restart the gateway, then:

```bash
curl -s -H "X-Gateway-Token: $(cat state/gateway.token)" \
     http://127.0.0.1:8109/v1/channels/health
```

The gateway also logs one line at boot:

```
channels: 4/7 configured but UNHEALTHY: slack(...), gmail(...), calendar(...), github(...)
```

## Which account is connected?

`gmail_profile` (MCP tool) and `gmail_refresh.py` both print the **email address**
the OAuth token belongs to, plus message/thread counts:

```bash
cd llm_gatewayV9
uv run python gmail_refresh.py     # refreshes, then prints "gmail account: you@gmail.com"
```

If the token is expired (`invalid_grant`), re-authorize first — the account is
only readable with a live token. The OAuth **client id** (`GMAIL_CLIENT_ID`) is
the GCP project, not the account; it does not tell you who is signed in.

## Scripts

| Script | What it does |
|---|---|
| `key_info.py` | prints every credential: SET/NOT SET, what it enables, where to get it, how to refresh |
| `refresh_tokens.py` | renews Gmail / Calendar / Slack OAuth access tokens without a browser |
| `channel_probe.py` | live read-only probe of each configured channel (used at boot + `/v1/channels/health`) |
| `gmail_oauth_setup.py` / `calendar_oauth_setup.py` | first-time browser OAuth for Gmail / Calendar |