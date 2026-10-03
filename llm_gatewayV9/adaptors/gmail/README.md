# Gmail adaptor — PARTIAL (send/read live, push in P2)

OAuth2, no SMTP/app passwords. Send + read delegate to
`integrations.gmail` (one implementation, shared keys).

## Setup

1. Google Cloud → OAuth client (Desktop) → `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET`.
2. `cd llm_gatewayV9 && uv run python gmail_oauth_setup.py` (writes
   `GMAIL_TOKEN` + `GMAIL_REFRESH_TOKEN` to the gateway `.env`).
3. Hourly expiry renews via `gmail_refresh_token()` (MCP tool).

## Verify

```bash
curl -s http://localhost:8109/v1/integrations/gmail/query \
  -H 'Content-Type: application/json' \
  -d '{"args":{"api_method":"list","max_results":1}}'
# ok:true = live. 401 = re-run the OAuth setup.
```

## Gotchas

- Push delivery (Pub/Sub watch + JWT push endpoint) lands after P2.
- `send_email` parameter shape stays MCP-compatible (`to/subject/body`).
