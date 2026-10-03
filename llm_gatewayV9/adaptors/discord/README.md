# Discord adaptor — SEND LIVE (inbound WS listener is deployment concern)

Bot API send is live; the inbound Gateway WebSocket listener is a deployment concern (needs a persistent connection, not a request handler).

## Setup (when integrating)

1. Developer portal → application → Bot → `DISCORD_BOT_TOKEN`.
2. Invite URL with `applications.commands` scope.
3. Slash commands: use **guild** commands while developing (global takes ~1h).

## Verify (today)

```bash
uv run pytest tests/test_adaptors.py -q -k "discord or registry"
# Discord adaptor — SEND LIVE (inbound WS listener is deployment concern)
```

## Gotchas

- Gateway WebSocket needs op-10 hello → heartbeat → identify → dispatch.
- Set `ignore_bots` semantics on receipt or the bot will talk to itself.
- Resolve mentions lazily (after the trust gate) to avoid API cost on drops.
