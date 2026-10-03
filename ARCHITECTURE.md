# Aria — Project Architecture

> **Audience:** this is the *deep-dive* design doc. For a scannable overview,
> start with **[README.md](./README.md)** (resume-tuned summary, quick start,
> tech stack, engineering challenges). This file explains *how* Aria is built.
>
> **Scope:** the *entire* `project3` workspace — the Aria general-purpose AI
> agent (S9 orchestrator + V9 LLM gateway), its voice (Kokoro TTS) and Tier-1
> integration tools (GitHub / Slack / Notion / Gmail / Telegram / Calendar /
> weather), and the layered **computer-use** desktop agent built on
> `cua-driver`. For the computer-use-only deep-dive, see
> `COMPUTER_USE_ARCHITECTURE.md`; for the build/run plan see
> `archive/root_scratch/COMPUTER_USE_PLAN.md` (archived session plan).

---

## 1. High-level layout

```
project3/
├── llm_gatewayV9/            # FastAPI LLM gateway (port 8109) — model routing,
│                             #   caching, vision, pricing, multi-provider.
├── S9SharedCode/code/        # The Aria agent core (S9 orchestrator).
│   ├── flow.py               # Executor().run(query) — async orchestration.
│   ├── skills.py             # Skill registry + _TOOL_CATALOG (schemas the model sees).
│   ├── agent_server.py       # FastAPI web server (port 8500) + SSE chat + TTS.
│   ├── mcp_server.py         # MCP tool surface (stdio) — 37 tools, incl. Tier-1.
│   ├── gateway.py            # V9 client wrapper (LLM / embed / ensure_gateway).
│   ├── action.py             # Action-skill helper (integration confirmations).
│   ├── computer_use/         # Layered computer-use package (see §6).
│   ├── browser/              # Browser skill (4-layer web cascade).
│   ├── web/                  # Static chat UI (index.html / app.js / style.css).
│   ├── prompts/              # Per-skill system prompts (incl. action.md, computer.md).
│   ├── models/               # Kokoro ONNX TTS model + voices (runtime assets).
│   ├── state/                # Sessions, memory, vector index, recordings (runtime).
│   └── gmail_oauth_setup.py  # One-time Gmail OAuth consent helper (no SDK).
└── COMPUTER_USE_*.md         # Design + plan docs for the computer-use phase.
```

> **Note on `archive/`:** debug scripts, probe/bench one-offs, and loose
> session logs from development live in `../archive/` (sibling of this
> workspace root). They are intentionally excluded from the source tree.

---

## 2. LLM Gateway (V9)

`llm_gatewayV9` is a standalone FastAPI service on **port 8109**. It is
auto-started by `gateway.ensure_gateway()` and reads keys from
`llm_gatewayV9/.env`. It exposes:

- `/v1/chat` — chat completions with model routing (`router.py`), caching
  (`cache.py`), pricing (`pricing.py`), and multi-provider fan-out
  (`providers.py`).
- `/v1/vision` — vision/image understanding endpoint used by the browser
  and computer-use vision layers.
- `gateway.LLM().chat(messages=…, response_format=…, agent=…, model=…,
  images=…)` — the client the agent core uses.

The agent never talks to provider APIs directly; it always goes through V9.

---

## 3. Agent core (S9 orchestrator)

`flow.py` defines `Executor().run(query)`, an **async** coroutine. The web
server drives it inside `asyncio.run()` on a worker thread and streams
`data: {json}\n\n` SSE frames of types `log`, `done`, `error`.

`skills.py` holds the skill registry and the `_TOOL_CATALOG` (schemas the
planner sees). Dispatch branches include:

- **planner** — decomposes the query into an initial DAG + recovery subgraphs.
- **retriever** — FAISS / Memory search (`search_knowledge`).
- **researcher** — multi-step web research (`web_search`, `fetch_url`).
- **distiller** — extracts structured fields (with an inserted Critic node).
- **summariser** — condenses long content.
- **coder → sandbox_executor** — emits Python, runs it sandboxed.
- **browser** — web browsing via `browser/client.py` (`V9Client`), 4-layer cascade.
- **computer** — routes to `ComputerUseSkill` (see §6).
- **action** — the agent's "hands": calls the real-world integration tools
  (Tier-1 + web + time + currency). Returns a structured confirmation.
- **formatter** — renders the final answer for the user (terminal node).

`agent_config.yaml` declares each skill's prompt file, `tools_allowed`, and
`temperature`/`max_tokens`.

### 3.1 Tool resolution flow
```
agent_config.yaml (tools_allowed)
        │  names
        ▼
skills.py :: tool_payload()  ── looks up ──▶  _TOOL_CATALOG (JSON schemas)
        │  payload (only names present in catalog survive)
        ▼
mcp_runner.run_with_tools()  ── spawns ──▶  mcp_server.py (stdio, 1 session/skill)
        │  tool_call from model
        ▼
mcp_server tool fn  ── returns ──▶  result fed back to model until final text
```
Names absent from `_TOOL_CATALOG` warn loudly and are skipped, so the
catalog stays the single source of truth for what the model may call.

---

## 4. Voice & integration tools

- **Kokoro TTS** (`kokoro-onnx`): `/api/tts` returns a WAV; `app.js` plays
  it via `Audio()`. Loaded lazily as a singleton from `models/`
  (`kokoro-v1.0.onnx`, `voices-v1.0.bin`) and warmed at import.
- **Speech-to-text**: Web Speech API in the browser (`🎤` button).
- **Integration tools** (see §5): Telegram, Gmail (OAuth send + read),
  GitHub, Slack, Notion, Google Calendar, weather. Gated behind the agent's
  normal tool-selection and fail-soft when credentials are missing.

---

## 5. Tier-1 integrations (MCP tools)

All integrations are **fail-soft**: if a credential env var is unset the
tool returns `{"ok": false, "error": "… not set"}` instead of raising, so
the agent can tell the user what to configure. Credentials come from
`llm_gatewayV9/.env`; the agent env holds only URLs and flags, no secrets
(see `S9SharedCode/code/.env.example`, which is secret-free by design).

| Tool | Purpose | Required env var(s) |
|---|---|---|
| `send_telegram` | Post to Telegram via Bot API | `TELEGRAM_BOT_TOKEN` |
| `send_email` | **Send Gmail via Gmail API OAuth** (no SMTP/app password) | `GMAIL_TOKEN` (gmail.send) |
| `gmail_query` | Read Gmail (list/search, read message) via Gmail API OAuth | `GMAIL_TOKEN` (gmail.readonly) |
| `gmail_refresh_token` | Renew `GMAIL_TOKEN` from a refresh token | `GMAIL_REFRESH_TOKEN`, `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET` |
| `github_query` | GitHub REST: list_repos / issues / create_issue / search_code | `GITHUB_TOKEN` |
| `slack_message` | Post to a Slack channel via Bot API | `SLACK_BOT_TOKEN` |
| `notion_query` | Notion REST: pages / databases / append | `NOTION_TOKEN` |
| `create_calendar_event` | Google Calendar event via REST | `GOOGLE_CALENDAR_TOKEN` |
| `web_search` / `fetch_url` | Tavily (primary) + DDG fallback / crawl4ai | `TAVILY_API_KEY` |
| `get_time` / `currency_convert` | Timezone / FX (frankfurter.dev) | — |
| `search_knowledge` | FAISS vector search over indexed Memory | — |
| `computer_action` | Gated computer-use (see §6) | `COMPUTER_USE_ENABLED` |

### 5.1 Gmail OAuth (passwordless email, gateway-owned)
`send_email` and `gmail_query` use the **Gmail API** with an OAuth2 Bearer
token — no SMTP server, no app password. Tokens live in
`llm_gatewayV9/.env`; the agent holds none. The one-time consent flow
(`llm_gatewayV9/gmail_oauth_setup.py`) opens a browser, captures the
redirect at `http://localhost:8080`, exchanges `code` for `access_token` +
`refresh_token`, and writes both to the gateway `.env`. The access token expires ~1h;
`gmail_refresh_token()` exchanges the long-lived refresh token for a fresh
access token and rewrites `GMAIL_TOKEN` automatically. `send_email` builds
an RFC822 message, base64url-encodes it, and POSTs to
`…/users/me/messages/send`.

### 5.2 Security posture
- No user passwords stored; all third-party auth is token/OAuth based.
- Tools are scoped (e.g. `gmail.send` + `gmail.readonly`, `chat:write`).
- Missing credentials fail soft with a clear "set X" message.
- Computer-use is disabled by default and sensitive actions require in-UI
  approval (see §6.3).

---

## 6. Computer-use (layered desktop agent)

The `computer_use/` package replaces the old gated-shell
`computer_use.py`. It drives a real desktop app via **cua-driver** using a
perception-driven cascade.

### 6.1 Modules
| File | Responsibility |
|---|---|
| `daemon.py` | cua-driver process lifecycle, JSON-RPC-over-pipe wrapper, `capabilities()` probe. Exceptions: `DaemonError`, `PreconditionError`, `PermissionsError`. |
| `safety/gates.py` (+`permissions.py`) | `SafetyGates` — enabled / mode (dry-run\|live) / allow-deny / approval store / audit log. |
| `layers/` (7 modules: deterministic, extract, goal, perception, recovery, sequencing, vision) | The build-on-top layers: A goal decomposition, B perception interpretation, C action sequencing (scan-act-verify), D error recovery, E vision fallback. |
| `prompts/__init__.py` | System + user prompts for the LLM judgment calls (a11y + vision). |
| `engine.py` | `ComputerUseSkill` — the L0→L3 cascade + scan-act-verify loop. |
| `__init__.py` | Public exports **and** the backward-compatible `ComputerUse` / `get_computer_use()` shim so the MCP tool + web endpoints keep working. |

### 6.2 Layer cascade (per goal)
- **L0 gated-shell** — fallback when the daemon is unavailable
  (`run_command` / `read_file` / `write_file` / `open_app`), behind safety
  gates.
- **L1 extract** — read AX tree text / clipboard / file directly (0 LLM).
- **L2a deterministic** — known hotkey sequences (0 LLM).
- **L2b a11y** — `get_window_state` markdown + cheap LLM → `element_index`
  act.
- **L3 vision** — screenshot + set-of-marks → V9 `/v1/vision` → click
  `(x,y)`.

The engine runs a **scan-act-verify** loop (Layer C) up to `max_turns`
(default 12), with Layer D recovery (rescan / escalate / abort) and Layer E
vision fallback when AX is insufficient.

### 6.3 Safety model
Computer-use is **disabled by default** (`COMPUTER_USE_ENABLED`). Sensitive
actions (e.g. `kill_app`, `rmdir`, `format`, `diskpart`) create a pending
approval; the web UI (`/api/computer/approvals`, `/api/computer/resolve`)
renders an approval panel **gated behind `?cu=1`** so the default public
dashboard stays clean. Protected paths (`C:\Windows`, `C:\Program Files`,
`~/.ssh`, `~/.aws`) are never writable.

### 6.4 Integration points
- **MCP** (`mcp_server.py::computer_action`): high-level `drive_app` /
  `run_command` / `read_file` / `write_file` / `open_app` + low-level
  daemon primitives (`launch_app`, `get_window_state`, `click`, `type_text`,
  `press_key`, `hotkey`, `scroll`, `get_desktop_state`, `bring_to_front`,
  `kill_app`, `start_recording`, `stop_recording`, `replay_trajectory`,
  `list_apps`).
- **Web** (`agent_server.py`): approvals + `capabilities()`; approval panel
  in `web/` behind `?cu=1`.
- **Skills dispatch** (`skills.py`): `if skill.name == "computer":` builds
  a `ComputerUseSkill` with injected V9 judge/vision callables and routes
  the goal through `run()`.

---

## 7. Runtime notes

- **Python:** the project venv is Python 3.11 (`S9SharedCode/code/.venv`);
  system `python` is 3.8 and breaks on `list[dict]` annotations. Use the
  venv or `uv run`.
- **cua-driver on Windows:** uses a **named pipe**
  `\\.\pipe\cua-driver` (not a Unix socket). Native apps need no special
  permissions. `list_apps` windows field is empty on this build — target
  acquisition uses `get_accessibility_tree` instead.
- **Console encoding:** PowerShell is cp1252; use ASCII arrows in trace
  strings and write test output to files.

---

## 8. Build / verify order

1. `computer_use/` package (daemon, safety, layers, prompts, engine).
2. `computer_use/__init__.py` shim (replaces `computer_use.py`).
3. `skills.py` computer dispatch + `_TOOL_CATALOG` update.
4. `mcp_server.py::computer_action` extension.
5. `agent_server.py` `/api/computer/*` + `web/` approval panel (`?cu=1`).
6. `tests/test_computer_use.py` (trap guard, layer selection, re-scan
   invariant, regression on `/api/chat`).
