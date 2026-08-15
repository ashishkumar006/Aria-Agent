# Aria — General-Purpose AI Agent

> **One-liner:** A multi-agent AI assistant that plans, reasons, and acts — decomposing natural-language requests into a skill DAG, routing LLM calls through a failover gateway, and executing real-world actions (passwordless Gmail, GitHub, Slack, Notion, Telegram, desktop app control) behind a chat UI with neural TTS.

---

## 🎯 What This Project Demonstrates

| Area | Evidence |
|---|---|
| **Systems design** | Multi-agent orchestrator (planner → skills → formatter DAG) over a separate LLM gateway |
| **LLM engineering** | Prompt design, tool-use loops, model routing, caching, cost attribution |
| **Real integrations** | GitHub, Gmail (OAuth2, passwordless), Slack, Notion, Telegram, Calendar via MCP |
| **Desktop automation** | 4-layer computer-use cascade (accessibility → vision) on `cua-driver` |
| **Security mindset** | OAuth over passwords, scoped tokens, fail-soft tools, gated approvals |
| **Voice** | Server-side Kokoro neural TTS |

**Tech stack:** `Python 3.11` · `FastAPI` · `MCP (stdio)` · `LLM Gateway` · `OAuth2 / Gmail API` · `cua-driver` · `FAISS` · `Kokoro TTS` · `httpx` · `Prompt Engineering` · `Windows automation`

---

## ✨ Capabilities

- **Orchestrated reasoning** — planner breaks requests into a skill DAG (researcher, browser, distiller, coder, summariser, formatter) with automatic recovery subgraphs on failure
- **Real-world actions (Tier-1 integrations)** — passwordless Gmail (OAuth), GitHub, Slack, Notion, Telegram, Google Calendar, weather
- **Computer-use** — 4-layer cascade (extract → deterministic → accessibility → vision) driving real desktop apps via `cua-driver`, gated and approval-based by default
- **Voice** — server-side Kokoro neural TTS; browser speech-to-text input
- **Failover LLM gateway** — model routing, caching, pricing, multi-provider fan-out with per-agent/per-session cost attribution

---

## 🏗️ Architecture

```
project3/
├── llm_gatewayV9/        # FastAPI LLM gateway (port 8109)
├── S9SharedCode/code/    # Aria agent core (port 8500)
│   ├── flow.py           # async orchestrator (Executor.run)
│   ├── skills.py         # skill registry + tool catalog
│   ├── agent_server.py   # web server + SSE chat + TTS
│   ├── mcp_server.py     # 21 MCP tools (stdio), incl. Tier-1
│   ├── computer_use/     # layered computer-use package
│   ├── browser/          # browser skill (4-layer cascade)
│   ├── web/              # static chat UI
│   ├── prompts/          # per-skill system prompts
│   ├── models/           # Kokoro ONNX TTS assets (runtime)
│   └── state/            # sessions, memory, vector index (runtime)
└── ARCHITECTURE.md       # full design doc
```

See **[ARCHITECTURE.md](./ARCHITECTURE.md)** for the complete design, tool-resolution flow, computer-use cascade, and safety model.

### Request Lifecycle
```
You ──▶ /api/chat (agent :8500)
        │  Executor.run(query)
        ▼
   planner → [skill DAG] → formatter
        │  each tool-using skill calls
        ▼
   mcp_server (stdio) ──▶ Tier-1 / web / computer tools
        │  every LLM call goes through
        ▼
   V9 gateway (:8109) ──▶ provider APIs (routed, cached, failover)
        │
        ▼
   SSE stream {log, done} ──▶ chat UI (+ optional /api/tts WAV)
```

---

## 🚀 Quick Start

### Prerequisites
- Python **3.11** (project venv). System Python 3.8 will not work.
- `uv` (recommended) or manual venv.
- For computer-use: `cua-driver` installed and `COMPUTER_USE_ENABLED=true`.

### 1. Install
```bash
# gateway
cd llm_gatewayV9 && uv sync

# agent
cd ../S9SharedCode/code && uv sync
```

### 2. Configure Credentials
Copy `.env.example` → `.env` in `S9SharedCode/code/` and fill what you need
(all tools are **fail-soft** — unset ones report "not configured"):

| Feature | Env var(s) |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN` |
| Gmail (send + read, OAuth) | `GMAIL_TOKEN`, optionally `GMAIL_REFRESH_TOKEN` + `GMAIL_CLIENT_ID` + `GMAIL_CLIENT_SECRET` |
| GitHub | `GITHUB_TOKEN` |
| Slack | `SLACK_BOT_TOKEN` |
| Notion | `NOTION_TOKEN` |
| Google Calendar | `GOOGLE_CALENDAR_TOKEN` |
| Web search | `TAVILY_API_KEY` |

> **Gmail is passwordless.** Run `python gmail_oauth_setup.py` once to open a
> browser consent flow; it writes `GMAIL_TOKEN` (+ refresh token) to `.env`.
> No SMTP, no app password.

### 3. Run
```bash
# terminal A — gateway
cd llm_gatewayV9 && uv run main.py

# terminal B — agent
cd S9SharedCode/code && uv run agent_server.py
```
The agent auto-starts the gateway if not already listening; restart gateway
separately if `GET /api/health` reports `gateway_up: false`.

### 4. Use It
Open <http://127.0.0.1:8500/> and chat. Try:
- *"Read my latest 3 emails and tell me the sender and subject."*
- *"List my GitHub repositories."*
- *"What's the weather in London?"*
- *"Open Spotify and play something."* (computer-use; needs `?cu=1` approval)

---

## 🧰 MCP Tool Surface (`mcp_server.py`)

21 tools, spawned per skill invocation over stdio:

| Category | Tools |
|---|---|
| Messaging | `send_telegram`, `slack_message` |
| Email | `send_email` (Gmail OAuth), `gmail_query` (read), `gmail_refresh_token` |
| Code | `github_query` |
| Docs | `notion_query` |
| Calendar | `create_calendar_event` |
| Web | `web_search`, `fetch_url` |
| Utility | `get_time`, `currency_convert`, `get_weather`, `search_knowledge` |
| Desktop | `computer_action` (gated) |

Every tool fails soft: missing credentials return `{"ok": false, "error": "…"}`
so the agent can tell the user what to configure.

---

## 🔒 Security Model

- **No passwords** — all third-party auth is token/OAuth (Gmail uses OAuth2 Bearer tokens, not app passwords)
- **Scoped credentials** — `gmail.send` + `gmail.readonly`, `chat:write`, etc.
- **Computer-use off by default** — sensitive actions require in-UI approval behind `?cu=1`; protected paths (`C:\Windows`, `C:\Program Files`, `~/.ssh`, `~/.aws`) never writable
- **No auth on local endpoints** — bind to localhost / front with a proxy before exposing beyond your machine

---

## 🧪 Testing

```bash
cd S9SharedCode/code
uv run pytest tests/          # skill + engine + server-route suite
uv run python test_credentials.py   # smoke-test live Tier-1 creds (no secrets printed)
```

---

## 📁 Repository Hygiene

- `archive/` (sibling of this file) holds development debug scripts, probe/bench one-offs, and loose session logs — excluded from the source tree
- Runtime artifacts (`state/`, `models/`, `*.log`, `.venv/`) should be git-ignored; they are not part of the committed source

---

## 🛣️ Roadmap / Known Gaps

- Auto token-refresh on 401 (wire `gmail_refresh_token` into the call path)
- Endpoint auth for agent + gateway
- Per-tool CI smoke tests
- Cross-platform computer-use (currently Windows / `cua-driver` named pipe)

---

## 🧠 Engineering Challenges (Interview-Ready)

Real problems solved while building Aria:

1. **Toggle-loop bug in computer-use** — A media "Play" click flipped UI to "Pause", but the judge had no "done" rule, so it clicked again and toggled playback *off* (12-turn loop). Fixed with a **click-only anti-toggle guard** + a generic "activating controls" prompt rule (no domain hardcoding).

2. **Silent tool drop** — Tools in `agent_config.yaml` weren't reaching the model because they were missing from `_TOOL_CATALOG`. Root-caused the catalog as the single source of truth and added a resolution diagram (see ARCHITECTURE.md §3.1).

3. **Passwordless email** — Replaced SMTP/app-password `send_email` with **Gmail API + OAuth2** — no stored password, scoped `gmail.send`/`gmail.readonly`, plus a refresh-token helper for ~1h token expiry.

4. **Fail-soft by design** — Every integration returns `{"ok": false, "error": …}` on missing creds instead of crashing, so the agent tells the user what to configure.

## 📏 Scope & Honesty

- **Local prototype** — not a deployed service. Two services (gateway :8109, agent :8500) run on your machine
- **Computer-use** — Windows / `cua-driver` today; cascade design is provider-agnostic
- **Integrations** — wired and tested live (GitHub, Gmail read/send, Telegram); Slack/Notion wired, need tokens to exercise

---

## 📄 License

For study / portfolio use. Respect the terms of each integrated provider
(GitHub, Google, Slack, Notion, Telegram) when enabling integrations.
