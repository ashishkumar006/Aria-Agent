# Aria — System Architecture V2

> **Status:** living reference. `ARCHITECTURE.md` remains the S9/computer-use
> design record; this file is the whole-system reference (agent + gateway +
> console + flags + tracker apps + roadmap) as of 2026-09-28.
>
> **Two Prefabs — do not confuse them:**
> - `prefab.cloud` (proprietary SaaS): feature flags + dynamic config.
>   Evaluated via `prefab-cloud-python`. See §5.
> - `PrefectHQ/prefab` (Apache 2.0, free, maintained by Prefect): generative
>   UI framework (Python DSL → JSON protocol → React renderer), used for
>   tracker-app mini-UIs and MCP Apps. See §5b. No account, key, or payment.

---

## 1. System map

Two services, strict secret split. The agent never calls provider APIs
directly; the gateway never serves the console UI.

```
┌─────────────────────────────┐        ┌──────────────────────────────┐
│ AGENT  127.0.0.1:8500       │        │ GATEWAY 127.0.0.1:8109       │
│ S9SharedCode/code/          │ HTTP   │ llm_gatewayV9/               │
│  agent_server.py (FastAPI)  ├───────►│  main.py (FastAPI)           │
│  flow.py (Executor DAG)     │ /v1/*  │  router.py (pick+limits)     │
│  skills.py (13 skills)      │        │  providers.py (9 providers)  │
│  mcp_runner → mcp_server.py │        │  pricing.py + SQLite ledger  │
│  scheduler / apps / flags   │        │  memory/*, voice, channels   │
└──────────────┬──────────────┘        └──────────────────────────────┘
               │ serves bundle
               ▼
        React console (SPA, lazy routes)
        Chat Runs Memory Research Scheduler Skills Apps Ledger Console Settings
```

| Item | Agent | Gateway |
|---|---|---|
| Start | `uv run agent_server.py` in `S9SharedCode/code` | `uv run main.py` in `llm_gatewayV9` |
| Health | `GET /api/health` → `{agent: ready, gateway_up}` | `GET /v1/providers`, `GET /v1/status` |
| Secrets | none in code (env-inherited only) | all keys in `llm_gatewayV9/.env` |
| State | `S9SharedCode/code/state/` | `llm_gatewayV9/state/` (+ `gateway_v8.db`) |
| Contract | `gateway_up: true` required for LLM paths | pool visible in `/v1/providers` |

**Startup order:** gateway first (agent `ensure_gateway()` auto-starts it
via `Popen` + 45s poll if absent). Both start background workers at import:
agent warms gateway + Ollama embeddings, starts scheduler heap worker and
apps refresh worker; all wrapped `try/except: pass` so boot never blocks.

---

## 2. Agent core (`S9SharedCode/code/`)

### 2.1 Executor (`flow.py`)
`Executor().run(query, session_id, resume, should_cancel)`:
1. New run: `write_query` + seed `planner(USER_QUERY)` node. Resume: load
   `graph.json`, reset `running→pending`, reuse stored query.
2. Once per run: `memory.read` + `memory.policies()` + recent turnlog.
3. Loop: cancel-check → `ready_nodes()` (pending, preds `complete|skipped`)
   → mark running + `write_graph` → `asyncio.gather(_run_one…)` →
   persist `NodeState` → `extend_from` (successors, fan-out, critic
   auto-insert, planner short-circuit, formatter capture) or
   `plan_recovery` (skip→`skipped`, else capped replan, max 2/node).
4. Final: `formatter.final_answer`, else last readable
   `final_answer|answer|text|content`. Cancelled + empty → "Stopped by
   operator…". Daemon turnlog append + working-drawer purge.

Node lifecycle: `pending → running → complete | failed | skipped`.
Cancel is polled **between batches** via `Event.is_set` (never `.set` —
that bug cost a full E2E). Graph persisted twice: `graph.json` (NetworkX
node-link) + `nodes/n_001.json` (`n:<i>` → zero-padded files).

### 2.2 Skills (`skills.py`, `agent_config.yaml`)
13 skills: planner, retriever, researcher, distiller (critic:true),
summariser, critic, formatter, coder→sandbox_executor, sandbox_executor,
browser, action, vision_file, computer. Each declares prompt file,
`tools_allowed`, provider pin, temperature/max_tokens.

Dispatch branches in `run_skill`:
- **Gateway-backed** (default): `render_prompt` (template + USER_QUERY only
  if wired + per-node QUESTION + FAILURE + policies always + memory except
  formatter/coder/critic/distiller/summariser/sandbox) → `LLM().chat` or
  `mcp_runner.run_with_tools` when `tools_allowed` non-empty.
- **Bypass gateway chat**: `sandbox_executor` (runs coder output via
  `sandbox.run_python`; timeout tagged `[deterministic-timeout]`),
  `browser` (4-layer cascade), `vision_file` (direct `LLM().vision`),
  `computer` (`ComputerUseSkill`, own cost tally).

Tool resolution: `agent_config.yaml` names → `tool_payload()` filters
`_disabled_tools()` (`state/tools_disabled.json`, live) → `_TOOL_CATALOG`
schemas (37 tools). Unknown names warn + skip loudly. Malformed model
successors fail the node (never silent).

### 2.3 MCP tool loop (`mcp_runner.py`)
`run_with_tools(prompt|messages, tools_payload, agent, session_id, …)`:
one MCP stdio session per skill invocation (`sys.executable mcp_server.py`
via `stdio_client`), then `run_tool_loop` (max 6 hops): chat → dispatch
tool_calls via MCP (120s cap, 8KB result cap) → append assistant + tool
messages → repeat. Same `name:args` >2× aborts as verifier-stall.

### 2.4 Scheduler vs tracker-apps workers
- `scheduler.py`: `state/schedules.json` + in-memory heap, 1s-tick thread.
  Fires **agent queries** through `Executor().run` (derived `s8-<short>`
  sid, cost snapshot → ledger, optional Telegram notify). Language:
  `daily@HH:MM`, `every Nm/Nh/Ns`, `tomorrow HH:MM`, ISO, epoch.
- `apps.py`: `state/apps/<id>.json` spec + `<id>.data.json` snapshot,
  30s-tick thread. Fires **code refreshes** (HTTP fetch → filter → id
  diff → history), reusing the scheduler's cron parser. Failures recorded
  per-app, never raised. Seeds `free-openrouter-models daily@09:00`.

### 2.5 Chat paths
- `POST /api/chat` → full DAG run, SSE frames
  (`started/log/meta/done`, `media_type="text/event-stream"` — required,
  else gzip middleware buffers). Disconnect still records cost.
- `POST /api/chat/simple` → one gateway call + **read-only tools**
  (web_search, fetch_url, get_time, search_knowledge, currency_convert)
  via the MCP loop, billed to `ct-*` threads; tool failure falls back to
  plain text (logged as `[chat] tool loop failed`). Gated by
  `chat.tools` flag. No graph, never appears in Runs.
- `POST /api/chat/cancel` → sets cancel event; `flagged` in response.

### 2.6 Persistence (`persistence.py`)
`state/sessions/<sid>/`: `query.txt`, `graph.json`, `nodes/n_*.json`,
`turn_costs.json`, `browser/`. Atomic writes (tmp + `os.replace`).
IDs `^[A-Za-z0-9][A-Za-z0-9_-]{0,64}$`. `S9_STATE_DIR` env overrides the
state root (tests isolate here — never hardcode `state/`).

### 2.7 Server hardening notes (earned the hard way)
- `_Tee` mirrors stdout/stderr → `logs/agent.out|err` (4MB rotate) for the
  Console feed; must proxy the full TextIO surface (`fileno`, `encoding`,
  `writelines`, `__getattr__`) or MCP stdio spawn dies with AttributeError.
- Bearer gate (`ARIA_API_TOKEN`), loopback CORS, traversal-confined
  artifact paths, atomic JSON writes, query length caps, XSS-escaped
  markdown renderer, cost-never-fabricated (`None`, not `$0`, on failure).

---

## 3. Gateway (`llm_gatewayV9/`, `:8109`)

### 3.1 `/v1/chat` pipeline
1. Normalize (`prompt`→messages), pre-resolve `http(s)` images to `data:`.
2. `est = chars//4 + min(max_tokens,8192)`; capability requirements from
   tools/reasoning/response_format/images.
3. Precedence: `MODEL_ROUTES[model]` > explicit `provider=` >
   `agent_routing.yaml[agent]` (live pool only) > default order.
4. `auto_route` (no explicit provider): router-LLM tier classify
   (TINY <1000 / LARGE / HUGE >8000 → **503, no summarizer yet**) with
   deterministic count fallback; tier orders intersected with live pool.
5. Explicit single provider blocked only by cooldown → wait ≤30s, not 503.
6. Failover loop: `Router.pick` → `record` → `chat|stream`. One
   same-provider retry on 5xx/408/timeout. Structured-output validation +
   one corrective retry. Success updates token meters + SQLite row.
7. `ProviderError` → `_backoff_for`: 429→15s (queue) /60s (RPM) /3600s
   (RPD); 5xx→20s; 408/timeout→10s; 402→300s; 401/403→600s (0s with
   model override — bad model id, don't blackball). Non-retryable or
   explicit pin → 502; else drop candidate, continue. Exhausted → 503.

### 3.2 Router (`router.py`)
`pick()` returns the **least-loaded usable** candidate (fewest calls last
60s, then longest idle) — concurrent calls fan out across providers, no
priority-order queueing. Guards (unchanged): capabilities, `max_ctx`,
RPM/RPD/TPM, daily token caps. Failover stays sequential (racing would
pay N× cost).

| Provider | rpm | rpd | tpm | cooldown | max_ctx |
|---|---|---|---|---|---|
| ollama | ∞ | ∞ | ∞ | 0 | 32k |
| cerebras | 30 | 9999 | 60k | 2s | 8k (+1M/day cap) |
| groq | 30 | 1000 | 6k | 2s | 100k |
| nvidia | 40 | 9999 | 100k | 2s | 100k |
| gemini/gemini35lite | 15 | 1000 | 250k | 4s | 1M |
| openrouter | 20 | 50 | ∞ | 3s | 100k |
| github | 10 | 50 | ∞ | 6s | 8k |
| kilo | 60 | 9999 | ∞ | 1s | 262k |

Key-pool siblings (`gemini-2…-4`, `gemini35lite-2…-4`) share canonical
limits but track rate-state **per key** — this is what parallel Gemini
throughput is. `GATEWAY_GEMINI_ONLY=true` keeps only the Gemini family
(current production setting: all-$0 spend).

### 3.3 Providers & pricing
OpenAI-compatible base (groq/cerebras/nvidia/openrouter/github/kilo) +
native Gemini (function calling, thought signatures, prompt caching) +
Ollama (native or prompted-fallback tools). Pricing: **everything $0
except groq (0.15/0.75) and cerebras (0.50/0.50)** per MTok — the reason
Gemini-only mode exists. Ledger dollars are readability; tokens are
authoritative. SQLite `calls` (+`tool_uses` names-only) backs
`/v1/calls`, `/v1/tools/usage`, `/v1/cost/by_agent`.

### 3.4 Other gateway surfaces
`POST /v1/chat/batch` (bounded concurrency), `/v1/vision`, `/v1/embed`
(Ollama-only, fail-closed), `/v1/memory/*` (drawer cabinet: policy, fact,
playbook, document, episode, working; RRF vector+keyword fusion),
`/v1/tts|stt|voices` (Kokoro ONNX + faster-whisper), channels/voice/
integrations/deploy routers, `/v1/providers|status|routers|capabilities|
embedders`, static dashboard.

---

## 4. Console (React 19 + Vite 8 + Tailwind v4 + xyflow 12)

Lazy-routed SPA (`/`→`/research`): **Chat** (lightweight threads, SSE-less
POST), **Runs** (summaries + DAG + Inspector + stop), **Memory**
(drawers + remember/wipe), **Research** (SSE DAG + auto Report tab +
history via formatter node), **Scheduler** (adaptive polling), **Skills**
(catalog + live guard toggles), **Apps** (overview + tracker apps +
flags), **Ledger** (`s8-*` + `ct-*` picker, apply-to-fetch pattern),
**Console/Mission** (pausable event feed, ANSI-stripped server tail),
**Settings** (read-only config + gateway deep links).

Shared shell: `Rail` (Core/Automate/Insight/System), `TopBar`, `Pill`,
`Stat`, `Empty`. `api.ts` `CONF` holds ports/poll intervals — never
hardcode. `renderMarkdown` (escape-first: h1-3, ul/ol, bold, code,
http(s) links) + `classifyStatus` shared everywhere.

`DagCanvas`: topological-depth columns, wrap at 8 rows with reserved
sub-column widths, smoothstep edges, **fit only on structure change**
(sorted ids+edges signature; status flips never yank viewport),
user-drag preservation, minZoom 0.15. Shared by Research + Runs.

---

## 5. Flags — prefab.cloud SDK (proprietary SaaS)

`flags.py`: `prefab-cloud-python` when `PREFAB_API_KEY` is set, else local
`state/flags.json`. Precedence: **local override > Prefab cloud >
definition default**; reads never raise. Definitions: `chat.tools`,
`apps.views.table`, `apps.views.cards` (all bool, default true).
Board: `GET/POST /api/apps/flags` (null clears), source badges
`prefab|override|default`, bool switches + revert. Cloud needs an account
+ key; local mode is fully functional at $0. Free tier covers our scale.

## 5b. Mini-UI — PrefectHQ/prefab OSS (Apache 2.0, free forever)

Python DSL (`prefab_ui.PrefabApp`, 100+ components) → JSON protocol →
bundled React renderer (`app.html`, offline) or CDN ESM. No account, key,
or payment. Agent skill at `skills/prefab-ui/SKILL.md`. FastMCP-native
MCP Apps: tools returning component trees render in any MCP Apps host.
Adoption: `prefab_views.py` converts tracker snapshots to component
trees (Metric row + DataTable + history Sparkline + refresh-via-
Call-MCP-Tool); served side-by-side at `GET /api/apps/{id}/prefab`;
console embeds bundled renderer. Specs stay source of truth; Prefab is
the render layer. (Status: spike complete 2026-09-28 — renders via
bundled `app.html`; notes: default light theme, ~6.6MB page, cosmetic
bridge error with no MCP host. Decision pending §8 item 2.)

---

## 6. Tracker apps (config-driven)

Spec (JSON, validated → 400/409): `{id, name, kind, schedule,
id_field, fields, max_items, history, ui{view, columns, sort,
highlight_new}, kind-fields}`. Kinds: `openrouter_free` (public models
endpoint, `pricing.prompt=="0"`, zero-config) and `json_feed` (URL +
dot-path + `{field, equals}` + allowlist). Runner diffs id sets
(+added/−removed), keeps capped history, records failures per-app.
Schedules reuse scheduler cron (`daily@HH:MM`, `every Nm/Nh`, `manual`).
Seed: `free-openrouter-models daily@09:00` on first boot. UI: sidebar
boards with status pills, detail (stats, filter, table/cards, history),
create form, refresh-now, delete. (Edit = delete + recreate, for now.)

---

## 7. Security posture

Secret split (agent code holds none; gateway `.env` holds all); fail-soft
tools with "set X" messages; computer-use disabled by default with
approval-gated sensitives + protected paths; `.opencode/plugins/
env-protection.js` structurally blocks `.env` reads; Prefab **write**
tools (when wired) require per-call user confirmation — reads run free.

---

## 8. Extension roadmap (design + acceptance)

| # | Item | Design | Done when |
|---|------|--------|-----------|
| 1 | Prefect Prefab spike | §5b steps 1–4 | OpenRouter tracker renders via bundled renderer, screenshot, eval note |
| 2 | Renderer decision | keep third view vs full switch | explicit decision recorded here |
| 3 | MCP Apps exposure | `mcp_server.py` tools return Prefab trees | renders in an external MCP Apps host |
| 4 | Renderer growth | sparklines, bar charts, sort control, per-app interval | each behind spec fields + tests |
| 5 | Tracker kinds: RSS, webpage-change | new `apps.py` kinds + fixtures | live seed + diff proof |
| 6 | Change notifications | per-app `notify` → Telegram path | message received on added/removed |
| 7 | Spec edit UI | detail-panel editor (validated) | round-trip edit without recreate |
| 8 | HUGE-tier summarizer | chunk→map→reduce skill, tier order entry | >8000-token query returns cited summary |
| 9 | Provider spend alerts | Ledger thresholds on non-$0 providers | alert row + feed event on breach |
| 10 | Credentials | GitHub PAT (`repo`), Slack reinstall + scopes, Gmail refresh | live probes green, suites back to 0 fail |
| 11 | prefab.cloud sync | key → restart → `prefab` sources on board | cloud value flows end-to-end |
| 12 | Prefab MCP (cloud) | 2nd stdio session, read tools free, writes confirmed | toggle-via-chat demo |
| 13 | Prefab React SDK | `PrefabProvider`, frontend-enabled flags | cloud-evaluated UI flag demo |
| 14 | LSP/pyright | deferred; CLI loop suffices | only if diagnostics gap proven |
| 15 | Dynamic log levels + secrets via Prefab | after 11 stable | `.env` count reduced, timed debug demo |

## 9. Verify matrix

| Area | Command (cwd) | Green bar |
|---|---|---|
| Agent suite | `uv run python -m pytest tests/ -q` (`S9SharedCode/code`) | 372 passed, 36 skipped (~6 min) |
| Gateway suite | `uv run python -m pytest tests/ -q` (`llm_gatewayV9`) | 174 passed + 2 known cred fails (~4 min) |
| Frontend | `npx tsc --noEmit` + `npm run build` (`console-frontend/`) | 0 errors |
| Live | `/api/health` (`gateway_up:true`), `/v1/providers` pool, chat+tools probe, research E2E | answer + complete graph |
| note | bare `uv run pytest` resolves the wrong interpreter (numpy fail) — always `python -m pytest` | — |

PowerShell 5.1 (`;` not `&&`), `python -c` quoting breaks (temp scripts
instead), scratch scripts live outside the repo and die after use.

## 10. Glossary

`s8-*` research/executor session · `ct-*` lightweight chat thread ·
`sch-*` schedule id · `n:<i>` DAG node · turn = one billed reply ·
drawers (policy/fact/playbook/document/episode/working) · tiers
(TINY/LARGE/HUGE) · dialects (native/prompted_fallback) · sources
(prefab/override/default) · kinds (openrouter_free/json_feed) ·
`ui.view` (table/cards).
