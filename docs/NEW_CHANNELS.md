# New channels to add to Aria

This document surveys free/cheap channels Aria could add, what each does, how to
wire it into the gateway, and what value it brings. It is a **plan, not an
implementation** — nothing here is wired yet.

Cost policy: **free protocols first; pay only where there is no free option, and
pick the cheapest vendor.** Almost every "channel people pay for" is free.

---

## How a channel is wired in Aria

A channel is added in one place per concern, so credentials live in exactly one
process (the gateway):

| Layer | File | Role |
|---|---|---|
| **Adaptor** | `llm_gatewayV9/adaptors/<name>/adapter.py` | `send()`, `normalize()` (inbound → `ChannelMessage`), `verify_webhook()`, `required_keys()`. The credential never leaves here. |
| **Integration** | `llm_gatewayV9/integrations/<name>.py` | The functions the agent's tools call (send/read/query), returning `{ok: ...}`. |
| **Dispatch** | `llm_gatewayV9/channels_api.py` | Routes `/v1/integrations/{service}/{op}` and `/v1/channels/{name}/send`. |
| **MCP tool** | `S9SharedCode/code/mcp_server.py` | Thin forwarder the model calls (e.g. `telegram_send_message`). |
| **Bundle** | `S9SharedCode/code/skills.py` (`CHANNEL_TOOLS`) | Groups a channel's tools so `load_tool_bundle("<name>")` appends them mid-run. |
| **Grant** | `S9SharedCode/code/agent_config.yaml` (`action.tools_allowed`) | Which tools the action skill may call. |
| **Docs** | `llm_gatewayV9/key_info.py` | Names, purpose, where to get the key, how to refresh (never values). |

Inbound (two-way chat) additionally needs a **poller** (like `telegram_poller.py`,
`gmail_poller.py`, `slack_poller.py`) that reads new messages and feeds
`channels_api._record_inbound` — because a local install has no public webhook URL.

---

## New channels (not in the gateway at all)

### ntfy — phone push notifications
- **What it does:** one HTTP POST pushes a notification to your phone/desktop. Supports titles, priorities, tags, and **action buttons** (approve/deny from your phone). Topics are created on the fly.
- **Cost:** **FREE** — hosted `ntfy.sh` (250 messages/day per IP, no signup), or self-host for unlimited.
- **What you need:** nothing (hosted), or a self-hosted server. Optional `NTFY_TOKEN` for a reserved topic.
- **API:** `POST https://ntfy.sh/<topic>` with the message as the body (or JSON `{topic, message, title, tags, actions}`).
- **How to wire:** a tiny `integrations/ntfy.py` (`send(topic, message, title?, actions?)` → one httpx POST), an adaptor for outbound, an MCP tool `ntfy_send(topic, message)`, a `ntfy` bundle, and a grant. No inbound needed (unless you want replies via its action buttons → webhook).
- **Value:** the single best zero-setup addition — instant alerts + approve/deny buttons on your phone.

### Zoho Cliq — team chat (Slack/Teams peer)
- **What it does:** channels, DMs, bots, slash commands, file sharing. Bots can post to channels/chats and receive messages via incoming webhooks.
- **Cost:** **FREE** plan — unlimited users, 100 bots, 100 slash commands, 100 GB storage, API access.
- **What you need:** a Zoho account; a **bot webhook token** (simplest) or an OAuth client (Zoho API Console).
- **API:** `POST https://cliq.zoho.com/api/v2/bots/<bot>/message`; channel messages API; per-bot incoming webhooks. ~1000 req / 5 min.
- **How to wire:** `integrations/cliq.py` (send to a channel/bot), an adaptor (`send`, `normalize` for inbound webhooks), MCP tools (`cliq_message`, `cliq_history`), a `cliq` bundle, and a grant. Auth via webhook token (no app review).
- **Value:** a clean Slack-class channel at $0 — especially if you/your org runs Zoho (very common in India).

### Zoho Mail — custom-domain email
- **What it does:** send/read/manage mail on a custom domain, with OAuth.
- **Cost:** **FREE** — 5 users, 5 GB each, custom domain, **India data center**. (Free plan excludes IMAP/POP, but the API works.)
- **What you need:** a Zoho account + custom domain; an OAuth client (`ZohoMail.messages.CREATE/ALL` scopes).
- **API:** REST (`https://www.zohoapis.com/mail/v1/...`), OAuth 2.0.
- **How to wire:** `integrations/zoho_mail.py` (send/query), an adaptor, MCP tools, a bundle, and a grant. Reuses the `oauth_flows.py` pattern already built for Gmail/Calendar.
- **Value:** custom-domain business email at $0 — a cleaner "non-Gmail email" path than raw IMAP.

### Bluesky (AT Protocol) — social
- **What it does:** post to Bluesky, read public feeds, reply to mentions, follow. Open protocol, no app review.
- **Cost:** **FREE** — no developer account, no key; an **app password** from the account.
- **What you need:** a Bluesky account + an app password (Settings → App passwords).
- **API:** `com.atproto.server.createSession` (login) → `com.atproto.repo.createRecord` (post). 300-char posts. SDKs: `@atproto/api` (TS), `atproto` (Python).
- **How to wire:** `integrations/bluesky.py` (session + createRecord), MCP tools (`bsky_post`, `bsky_feed`), a bundle, and a grant.
- **Value:** free social posting to a growing network; the easiest official bot integration that exists.

### Mastodon — federated social
- **What it does:** post ("toot"), read timelines, reply, follow — on any Mastodon instance (ActivityPub).
- **Cost:** **FREE** — per-instance REST API.
- **What you need:** a Mastodon account + an app token (Preferences → Development).
- **API:** `POST https://<instance>/api/v1/statuses` (with `in_reply_to_id` for threads); streaming API for mentions.
- **How to wire:** `integrations/mastodon.py` (statuses), an adaptor, MCP tools, a bundle, and a grant.
- **Value:** free, open, privacy-friendly social; good if you're on the fediverse.

### Mattermost / Zulip / Rocket.Chat — self-hosted team chat
- **What they do:** Slack-style team chat, self-hosted (data sovereignty).
- **Cost:** **FREE** (open-source; you pay only for the server).
- **What you need:** a self-hosted instance + a bot token.
- **API:** REST + webhooks (Mattermost), REST + topics (Zulip), REST (Rocket.Chat).
- **How to wire:** standard adaptor + integration + tools + bundle.
- **Value:** only if you self-host (e.g., a company that wants on-prem chat).

### Google Chat — Workspace
- **What it does:** post to Google Chat spaces (Workspace's Slack).
- **Cost:** **FREE** with Workspace.
- **What you need:** a Google Workspace account + a service account / bot.
- **API:** Google Chat API (`spaces.messages.create`).
- **Value:** if you're on Google Workspace (complements Gmail/Calendar).

---

## Existing-but-not-wired channels worth wiring (already health-probed)

These already have key docs / probe logic in `deploy_api.py` but no agent tools:

| Channel | Cost | Value | Notes |
|---|---|---|---|
| **WhatsApp (Meta Cloud API direct)** | **FREE tier** (1,000 service msgs/mo per number) | High — reach anyone by phone | Go **direct** (not Twilio) to avoid markup; needs a Meta app + phone number. |
| **Matrix** | FREE (self-host or free homeserver) | Medium — E2EE, decentralized | Needs a homeserver + bot account; `MATRIX_*` keys already documented. |
| **IMAP/SMTP** | FREE (your mailbox) | Medium — non-Google email | `IMAP_*`/`SMTP_*` already documented; standard protocols. |
| **Microsoft Teams** | FREE (M365) | Situational — corporate | Heavy (Azure app registration). |
| **LINE** | FREE | Regional (Japan/Taiwan/Thailand) | `LINE_*` documented. |

## What to skip (paid or fragile)
- **Twilio SMS** — ~$0.008/msg (the only unavoidable per-message cost; WhatsApp's free tier covers smartphones).
- **Signal** — no official bot API; signal-cli hacks are fragile and can get the account banned.
- **Reddit** — new API access closed Oct 2026; commercial ~$0.24/1k requests.
- **X/Twitter** — paid API tiers.

## Recommended build order (cost-optimized)
1. **ntfy** — free phone push with approve/deny buttons (biggest flexibility win, zero setup).
2. **Zoho Cliq** — free team chat (if you run Zoho).
3. **Zoho Mail** — free custom-domain email (India DC).
4. **WhatsApp (Meta direct)** — free-tier reach to anyone.
5. **Bluesky** — free social posting.
6. **IMAP/SMTP** — non-Google email.

All of 1–5 are **$0**. Each is a self-contained adaptor + integration + tools + bundle, following the same pattern as the existing telegram/gmail/notion channels.