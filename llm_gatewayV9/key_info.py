"""How to obtain and refresh every credential the gateway uses.

Names, purpose, where to get them, and how to renew them — never values.
`collect()` returns the same records with a live `set` flag so the console can
render a "what's missing" board without ever handling a secret.

Run standalone:
    cd llm_gatewayV9 && uv run python key_info.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Windows consoles default to cp1252; box-drawing headers would crash mid-report.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STATE = Path(__file__).resolve().parent / "state"
ENV_PATH = Path(__file__).resolve().parent / ".env"

# Standalone (`python key_info.py`) must see .env; the gateway loads it itself.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(ENV_PATH)
except Exception:
    pass


def _env_keys() -> set[str]:
    """Which keys the process can see. Never returns values."""
    return {k for k, v in os.environ.items() if v}


# Each record:
#   env      — the variable name(s)
#   enables  — what it turns on
#   group    — llm | channel | integration | voice
#   get      — where to get it (URL or steps)
#   refresh  — how to renew it ("none" = long-lived, re-issue to rotate)
#   script   — a helper in this repo, if any
KEYS: list[dict] = [
    # ── LLM providers ─────────────────────────────────────────────────────
    dict(env="GEMINI_API_KEY", enables="gemini + gemini35lite workers (4-key pool with GEMINI_API_KEYS)",
         group="llm", get="Google AI Studio -> https://aistudio.google.com/apikey -> Create API key",
         refresh="none (rotate in AI Studio, paste the new key)", script=""),
    dict(env="NVIDIA_API_KEY", enables="nvidia worker", group="llm",
         get="NVIDIA NGC -> https://ngc.nvidia.com -> Setup -> Generate API Key",
         refresh="none (rotate in NGC)", script=""),
    dict(env="GROQ_API_KEY", enables="groq worker", group="llm",
         get="Groq Cloud -> https://console.groq.com/keys -> Create API Key",
         refresh="none (rotate in console)", script=""),
    dict(env="CEREBRAS_API_KEY", enables="cerebras worker", group="llm",
         get="Cerebras Cloud -> https://cloud.cerebras.ai -> API Keys",
         refresh="none (rotate in console)", script=""),
    dict(env="OPEN_ROUTER_API_KEY", enables="openrouter worker", group="llm",
         get="OpenRouter -> https://openrouter.ai/keys -> Create Key",
         refresh="none (rotate in console)", script=""),
    dict(env="KILO_API_KEY", enables="kilo worker (the 471-model free pool)", group="llm",
         get="Your Kilo account -> API keys", refresh="none (rotate in Kilo)", script=""),
    dict(env="GITHUB_ACCESS_TOKEN", enables="github worker + GitHub integration", group="llm",
         get="GitHub -> Settings -> Developer settings -> Personal access tokens (fine-grained)",
         refresh="none (rotate in GitHub; expiry is set on the token)", script=""),

    # ── Chat / delivery channels ──────────────────────────────────────────
    dict(env="TELEGRAM_BOT_TOKEN", enables="telegram send + poll", group="channel",
         get="Telegram -> @BotFather -> /newbot -> copy the token",
         refresh="none (rotate with BotFather /revoke)", script=""),
    dict(env="DISCORD_BOT_TOKEN", enables="discord send", group="channel",
         get="Discord Developer Portal -> your application -> Bot -> Reset Token",
         refresh="none (rotate in the portal)", script=""),
    dict(env="SLACK_BOT_TOKEN", enables="slack send + history", group="channel",
         get="Slack app -> OAuth & Permissions -> Bot User OAuth Token (xoxb-)",
         refresh="if the app has token rotation ON: run refresh_tokens.py (needs SLACK_CLIENT_ID/SECRET + SLACK_REFRESH_TOKEN)",
         script="refresh_tokens.py"),
    dict(env="SLACK_REFRESH_TOKEN + SLACK_CLIENT_ID + SLACK_CLIENT_SECRET",
         enables="slack token rotation", group="channel",
         get="Slack app -> OAuth & Permissions -> Refresh token (xoxe-); app credentials from Basic Information",
         refresh="single-use: each refresh returns a new pair, written back by refresh_tokens.py",
         script="refresh_tokens.py"),
    dict(env="GMAIL_TOKEN", enables="gmail send + query", group="channel",
         get="Run gmail_oauth_setup.py (Google Cloud OAuth client + consent)",
         refresh="run refresh_tokens.py, or the gmail_refresh_token() MCP tool",
         script="gmail_oauth_setup.py / refresh_tokens.py"),
    dict(env="GMAIL_REFRESH_TOKEN + GMAIL_CLIENT_ID + GMAIL_CLIENT_SECRET",
         enables="gmail refresh without a browser", group="channel",
         get="Google Cloud Console -> APIs & Services -> Credentials -> OAuth client (Web/Desktop); consent scope gmail.send + gmail.readonly",
         refresh="long-lived; refresh_tokens.py mints a new GMAIL_TOKEN",
         script="refresh_tokens.py"),
    dict(env="ZOHO_TOKEN", enables="zoho_mail send + query", group="channel",
         get="Run zoho_mail_oauth_setup.py (Zoho API Console OAuth client + consent)",
         refresh="run zoho_mail_oauth_setup.py again, or the zoho_mail_refresh_token() MCP tool",
         script="zoho_mail_oauth_setup.py"),
    dict(env="ZOHO_REFRESH_TOKEN + ZOHO_CLIENT_ID + ZOHO_CLIENT_SECRET",
         enables="zoho_mail refresh without a browser", group="channel",
         get="Zoho API Console -> https://api-console.zoho.com -> Server-based app; consent scopes ZohoMail.accounts.READ + ZohoMail.messages.READ + ZohoMail.messages.CREATE",
         refresh="long-lived; zoho_mail_oauth_setup.py mints a new ZOHO_TOKEN",
         script="zoho_mail_oauth_setup.py"),
    dict(env="ZOHO_DC", enables="zoho_mail data centre (com|eu|in|com.au|jp|ca)", group="channel",
         get="Your Zoho account's data centre (Zoho India = in). Default com.",
         refresh="none", script=""),
    dict(env="NTFY_URL / NTFY_TOKEN", enables="ntfy phone push (keyless hosted)", group="channel",
         get="Hosted ntfy.sh needs nothing; set NTFY_URL for self-host or NTFY_TOKEN for a reserved topic",
         refresh="none", script=""),
    dict(env="ZOHO_CLIQ_TOKEN", enables="zoho_cliq team chat send", group="channel",
         get="Zoho Cliq -> your bot -> webhook token (or channel webhook). Set ZOHO_CLIQ_DC if not com",
         refresh="none (rotate the bot token in Cliq)", script=""),
    dict(env="BSKY_HANDLE + BSKY_APP_PASSWORD", enables="bluesky post", group="channel",
         get="Bluesky -> Settings -> App passwords -> create one (use the handle + app password)",
         refresh="none (revoke/recreate the app password)", script=""),
    dict(env="MASTODON_TOKEN + MASTODON_INSTANCE", enables="mastodon post", group="channel",
         get="Mastodon -> Preferences -> Development -> New application (read/write); instance = your server host",
         refresh="none (rotate in Development)", script=""),
    dict(env="GOOGLE_CHAT_WEBHOOK", enables="google_chat post to a space", group="channel",
         get="Google Chat space -> Apps & integrations -> Webhooks -> add -> copy the URL",
         refresh="none (recreate the webhook to rotate)", script=""),
    dict(env="GOOGLE_CALENDAR_TOKEN", enables="calendar create + query", group="channel",
         get="Run calendar_oauth_setup.py (same Google OAuth client, calendar scope)",
         refresh="run refresh_tokens.py, or calendar_refresh_token()",
         script="calendar_oauth_setup.py / refresh_tokens.py"),
    dict(env="GOOGLE_CALENDAR_REFRESH_TOKEN + GOOGLE_CALENDAR_CLIENT_ID + GOOGLE_CALENDAR_CLIENT_SECRET",
         enables="calendar refresh without a browser", group="channel",
         get="Google Cloud Console -> Credentials -> OAuth client",
         refresh="long-lived; refresh_tokens.py mints a new token", script="refresh_tokens.py"),
    dict(env="MATRIX_HOMESERVER + MATRIX_USER + MATRIX_PASSWORD", enables="matrix send", group="channel",
         get="Your Matrix homeserver -> register a bot account",
         refresh="none (re-login to rotate the access token)", script=""),
    dict(env="SIGNAL_NUMBER", enables="signal send (signal-cli)", group="channel",
         get="A phone number you control, linked to signal-cli",
         refresh="none (re-link signal-cli)", script=""),
    dict(env="TEAMS_APP_ID + TEAMS_APP_PASSWORD + TEAMS_TENANT", enables="teams send", group="channel",
         get="Azure Portal -> App registrations -> your bot app",
         refresh="rotate the client secret in Azure", script=""),
    dict(env="LINE_CHANNEL_SECRET + LINE_ACCESS_TOKEN", enables="line send", group="channel",
         get="LINE Developers Console -> your provider/channel -> Messaging API",
         refresh="rotate the channel access token in the console", script=""),

    # ── SMS / voice / WhatsApp ────────────────────────────────────────────
    dict(env="TWILIO_SID + TWILIO_AUTH + TWILIO_FROM", enables="twilio_sms send", group="channel",
         get="Twilio Console -> Account Info (SID + Auth Token) and a phone number",
         refresh="rotate the Auth Token in Twilio", script=""),
    dict(env="TWILIO_VOICE_FROM + VOICE_WS_URL", enables="twilio_voice", group="channel",
         get="Twilio Console -> phone numbers + a public WebSocket URL for media streams",
         refresh="rotate in Twilio", script=""),
    dict(env="WA_TOKEN + WA_PHONE_ID + WA_VERIFY_TOKEN", enables="whatsapp_meta send", group="channel",
         get="Meta for Developers -> WhatsApp Business -> API setup",
         refresh="rotate the system-user token in Meta", script=""),

    # ── Email (generic) ───────────────────────────────────────────────────
    dict(env="IMAP_HOST + IMAP_USER + IMAP_PASS + SMTP_HOST + SMTP_USER + SMTP_PASS",
         enables="imap send/poll over a normal mailbox", group="channel",
         get="Your mail provider's IMAP/SMTP settings (app password if 2FA)",
         refresh="rotate the app password with the provider", script=""),

    # ── Integrations ──────────────────────────────────────────────────────
    dict(env="NOTION_TOKEN", enables="notion_query (pages/databases)", group="integration",
         get="Notion -> Settings -> Integrations -> New internal integration -> copy the secret",
         refresh="rotate in Notion", script=""),
    dict(env="TAVILY_API_KEY", enables="web_search", group="integration",
         get="Tavily -> https://app.tavily.com -> API Keys", refresh="rotate in Tavily", script=""),

    # ── Voice ─────────────────────────────────────────────────────────────
    dict(env="KOKORO_MODEL + KOKORO_VOICES", enables="local TTS", group="voice",
         get="Bundled Kokoro model files", refresh="n/a", script=""),
]


def collect() -> list[dict]:
    """KEYS plus a live `set` flag (never the value)."""
    present = _env_keys()
    out = []
    for k in KEYS:
        # A record may name several variables ("A + B"); it counts as set when
        # every variable it names is present.
        names = [n.strip() for n in k["env"].split("+")]
        row = dict(k)
        row["name"] = " + ".join(names)
        row["set"] = all(n in present for n in names)
        out.append(row)
    return out


def main() -> None:
    rows = collect()
    for group in ("llm", "channel", "integration", "voice"):
        subset = [r for r in rows if r["group"] == group]
        if not subset:
            continue
        print(f"\n── {group} ──")
        for r in subset:
            mark = "SET     " if r["set"] else "NOT SET "
            print(f"{mark} {r['name']}")
            print(f"         enables: {r['enables']}")
            print(f"         get:     {r['get']}")
            if r["refresh"] and r["refresh"] != "none":
                print(f"         refresh: {r['refresh']}")
    missing = [r["name"] for r in rows if not r["set"]]
    print(f"\n{len(rows) - len(missing)}/{len(rows)} configured")
    if missing:
        print("missing: " + ", ".join(missing))


if __name__ == "__main__":
    main()