# Aria — General-Purpose AI Agent

A self-hosted AI agent: an LLM gateway, a FastAPI agent server with an async DAG
executor, and a React console. Bring your own keys.

```
┌────────────┐    HTTP     ┌──────────────┐    providers    ┌──────────┐
│  Console   │ ─────────► │    Agent     │ ──────────────► │ Gateway  │
│  (React)   │ ◄───────── │  :8500       │ ◄────────────── │  :8109   │
└────────────┘   SSE      └──────────────┘   /v1/chat      └──────────┘
                                    │                              │
                                    │ MCP tools                    │ memory plane
                                    ▼                              ▼
                             skills, browser,              drawers, ledger,
                             computer use, docs            retrieval, policy
```

## What it does

- **Documents** — upload PDF/DOCX/MD/TXT/HTML/CSV/XLSX, chunk, embed and retrieve
  grounded answers with citations. Per-conversation opt-in.
- **Chat** — streaming and plain, with read-only tools (`web_search`,
  `search_knowledge`, calendar, mail, GitHub, Slack) and document grounding.
- **Research** — an async DAG executor (`flow.py`) that fans out over skills.
- **Computer use** — browser and desktop control, gated by an approval allowlist.
- **Protocols** — AG-UI events and A2UI component surfaces, both validated.
- **Memory** — append-only drawers (working, episode, fact, playbook, document)
  with hybrid keyword + vector recall, a cost ledger and a policy gate.
- **Code** — a read-only workspace browser with tabs, search, outline and
  diagnostics. It does not execute anything.

## Quick start

**Requirements** — Python 3.11+, Node 18+, [Ollama](https://ollama.com) for
local embeddings (`nomic-embed-text`, 768-dim).

```bash
# gateway
cd llm_gatewayV9 && uv sync && uv run main.py        # :8109

# agent + console
cd S9SharedCode/code && uv sync && uv run agent_server.py   # :8500
```

Copy `.env.example` to `.env` and fill in whichever provider keys you have — the
gateway fails over across whatever is configured, and runs fully on Ollama alone.

Open <http://127.0.0.1:8500>. Both services bind loopback and the agent issues a
per-launch token that the console sends as `X-Aria-Token`.

## Architecture

| Path | Role |
|---|---|
| `llm_gatewayV9/` | FastAPI gateway: routing, failover, providers, memory plane, ledger, policy |
| `llm_gatewayV9/providers.py` | Provider adapters (OpenAI-compat, Gemini, Ollama, Groq, Cerebras, NVIDIA, OpenRouter, GitHub, Kilo) |
| `S9SharedCode/code/agent_server.py` | Agent API, SSE chat, documents, AG-UI, A2UI, auth |
| `S9SharedCode/code/flow.py` | DAG executor |
| `S9SharedCode/code/skills.py` | MCP tool catalog |
| `S9SharedCode/code/console-frontend/` | React console (Vite + TypeScript) |

### Request lifecycle

```
browser ──▶ agent :8500
              ├─ auth (per-launch token, loopback)
              ├─ document retrieval (optional, per conversation)
              ├─ gateway.ensure_gateway()
              └─ POST /v1/chat ──▶ gateway
                                       ├─ normalise, estimate tokens
                                       ├─ provider resolution (pin → route → auto_route)
                                       ├─ capability filter, cooldown/backoff
                                       ├─ execute (retry, then failover)
                                       ├─ ledger write
                                       └─ stream or JSON
```

## Security model

- **Loopback + per-launch token.** Binding to a non-loopback address without a
  token refuses to start. The token is injected into the SPA and sent as
  `X-Aria-Token`; cross-origin writes are rejected.
- **Policy gate** on the gateway, dry-run by default. Agent-dispatched MCP calls
  still bypass it — see Known gaps.
- **Computer use** is allowlist-driven and defaults to disabled.
- **Secrets** are never committed; only `.env.example` is tracked.

## Testing

```bash
cd llm_gatewayV9        && uv run python -m pytest tests/ -q
cd S9SharedCode/code    && uv run python -m pytest tests/ -q
cd S9SharedCode/code/console-frontend && npx tsc --noEmit && npm run build && npx playwright test
```

The live-provider test (`test_worker_chat_each_live_provider`) is deselected in
the default run because repeated live calls hit rate limits.

## Known gaps

Honest list of what is not finished:

- **Gateway `/v1/*` has no authentication.** The agent token guards `:8500`, but
  every gateway route is reachable unauthenticated on its port. Bind loopback-only
  or front it before exposing it.
- **Policy is dry-run**, and MCP mutations dispatched by the agent skip the gate.
- **Image URLs in `/v1/chat` are fetched without SSRF checks.**
- **Streaming bypasses rate accounting, retry and failover.**
- **No conversation idempotency** — a double-click starts two paid runs.
- **Computer-use approval** is per-session, not per-action.

## Repository hygiene

- Runtime `state/`, logs, model weights, `node_modules/` and `.venv/` are ignored.
- No credentials in the tree. `.env.example` files are templates with empty values.