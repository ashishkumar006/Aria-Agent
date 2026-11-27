# Adaptors (`adaptors/`)

The channel layer. Each channel is a package with an **adaptor** that owns its
transport and credentials — the one place a channel's secret lives. Adaptors never
hand raw provider payloads upward; everything is normalised to a typed envelope
first.

## Shared files
| File | Responsibility |
|---|---|
| `base.py` | `BaseAdaptor` — the one interface every channel implements. Errors: `AdaptorError`, `NotConfigured` (missing keys, never leaks names), `NotIntegrated` (scaffolded, live traffic disabled). |
| `envelope.py` | `ChannelMessage` / `ChannelReply` + `TrustLevel` (owner / paired / untrusted). Raw payloads stay inside the adaptor. |
| `registry.py` | `name → instance`, built lazily and env-gated. Only adaptors with credentials are "live"; the rest report `configured=false` + required key **names**. |
| `trust.py` | Trust store + classification (`state/pairing.json`); owner pairing via the control plane, never via chat. |
| `http.py` | Shared HTTP helpers. |

## Channel packages
`bluesky/`, `discord/`, `gmail/`, `google_chat/`, `imap/`, `line/`,
`local_mic/`, `mastodon/`, `matrix/`, `ntfy/`, `signal/`, `slack/`, `teams/`,
`telegram/`, `twilio_sms/`, `twilio_voice/`, `webhook/`, `webui/`,
`whatsapp_meta/`, `whatsapp_twilio/`, `zoho_cliq/`, `zoho_mail/`.

Each package typically has `adapter.py` (transport) + `schemas.py` (wire types)
+ `verify.py` / `README.md` where warranted.

## Contract
An adaptor implements `send()`, `normalize()` (inbound → `ChannelMessage`),
`verify_webhook()`, `required_keys()`. The credential never leaves the adaptor.
Inbound additionally needs a poller (see `../*_poller.py`) because a local install
has no public webhook URL.

## Notes
- `NotConfigured` vs `NotIntegrated` are distinct: keys may exist but outbound can
  be deliberately disabled during integration.
- Policy defaults: owner allow, paired allow (rate-limited), untrusted
  deny-all-tools (see `../policy/`).