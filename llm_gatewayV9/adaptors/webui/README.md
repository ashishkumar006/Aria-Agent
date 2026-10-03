# WebUI chat adaptor — LIVE (local trust, no keys)

The agent's own chat page (and the future PWA) as a channel: normalises
the session bus into envelopes so ledger + policy see it like any other
channel.

## Setup

None. Localhost traffic defaults to `owner`; pair other operators via
`POST /v1/control/pair`.

## Verify

```bash
curl -s http://localhost:8109/v1/channels | python3 -c \
  "import json,sys; print([c for c in json.load(sys.stdin)['channels'] if c['name']=='webui'])"
# configured:true (keyless adaptors are always live).
```

## Gotchas

- Delivery to browsers happens over each session's SSE stream (agent
  side); gateway `send()` only records the ledger row.
