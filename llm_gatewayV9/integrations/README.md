# Integrations (`integrations/`)

Third-party service functions the agent's tools call, **owned by the gateway** so
credentials live in exactly one place (`llm_gatewayV9/.env`). Moved here from the
agent's `mcp_server.py`; behaviour is verbatim (same fail-soft `{ok: ...}` shapes).
The agent's MCP tools are thin HTTP callers over
`POST /v1/integrations/{service}/{op}`.

> Services with their own channel adaptor (telegram, slack) live in `../adaptors/`.
> Gmail has **both** an adaptor (envelopes) and integration functions (raw tool
> shapes) sharing the same keys.

## Files
| File | Service / ops |
|---|---|
| `gmail.py` | Send / query / profile / refresh (OAuth, no SMTP). |
| `calendar.py` | Google Calendar query / create / refresh. |
| `zoho_mail.py` | Zoho Mail send / query / reply / mark / move / delete / folders. |
| `zoho_calendar.py` | Zoho Calendar list calendars / events. |
| `github.py` | Repos, code, issues, PRs, search. |
| `notion.py` | Pages / databases / blocks. |
| `telegram_api.py` | Telegram send / get / edit / delete. |
| `discord_api.py` | Discord message / guilds / channels. |
| `slack.py` | Slack message / history / refresh. |
| `cliq.py` | Zoho Cliq messages. |
| `ntfy.py` | ntfy push + history. |
| `bluesky.py` | Bluesky post (AT Protocol). |
| `mastodon.py` | Mastodon post. |
| `google_chat.py` | Google Chat message. |
| `websearch.py` | Web search + fetch (Tavily primary, DDG fallback). |

## Contract
Every function returns a **fail-soft** dict: `{"ok": false, "error": "... not set"}`
when a credential is missing (never raises), so the agent can tell the user what to
configure. `test_integrations.py`, `test_new_channels.py`, `test_zoho_*.py` cover
these.

## Notes
- Keys are read from `llm_gatewayV9/.env` (never the agent env).
- New channel = adaptor (optional) + integration functions + MCP tool + bundle +
  grant + `key_info.py` docs.