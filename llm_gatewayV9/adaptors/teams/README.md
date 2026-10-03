# Teams adaptor — SEND LIVE (tenant setup still required)

Bot Framework + Azure AD: app registration, manifest, tenant install.

## Setup (when integrating)

1. Azure portal → app registration → `TEAMS_APP_ID/PASSWORD/TENANT`.
2. Bot manifest + tenant install; messaging endpoint → `/v1/hooks/teams`.
3. Token flow: OAuth2 client-credentials; cache per serviceUrl.

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "teams or registry"
```

## Gotchas

- Strip `<at>…</at>` mentions before the envelope.
- Replies thread on `replyToId`; `launch_app`-style re-entry needs the
  stored serviceUrl per conversation.
