# LLM Gateway V9 — Architecture

> **Scope:** this file documents `llm_gatewayV9/` only — the FastAPI process
> on `:8109` that owns every secret, every model call, every channel, and
> all durable memory. For the agent orchestrator see the workspace
> `ARCHITECTURE.md`; for desktop control see `COMPUTER_USE_ARCHITECTURE.md`.
>
> **Status:** reflects the tree as of the memory-drawers build (legacy store
> frozen, 7 drawers live, per-folder test suites). If code and doc disagree,
> code wins — fix this file.
>
> Visual version: `static/architecture.html` (served at
> `/static/architecture.html`) renders this same architecture as four flow
> charts (system map, chat lifecycle, memory write/recall, channel
> send/inbound).

## 1. What it is

A single Python process (`main.py`, FastAPI + uvicorn) that rents
capabilities to credential-free callers over HTTP/JSON. Callers (the S9
agent on `:8500`, dashboards, hooks) never hold provider keys, bot tokens,
or OAuth tokens — all of those live in `llm_gatewayV9/.env` and never
leave the process. Every route is fail-soft outward (`{ok:false}` shapes,
never tracebacks) and loud inward (structural problems raise, never
silently degrade).

Design rules enforced across the tree:

1. **Secrets never leave.** Only key *names* appear in inventory, errors,
   ledger rows, and UI. Two scrubbers (`channels_api`, `integrations_api`)
   redact token shapes from anything stored or shown.
2. **One writer per state file.** SQLite ledger (append-only rows), locked
   JSON stores, FAISS indexes mutated in place by this process alone.
3. **Quota is defended centrally.** Every call path — chat, embed,
   channels, integrations — passes per-provider rate state. Callers keep
   no pools of their own.
4. **Unknown names fail closed.** Unknown channel/service/drawer →
   404/400, never silent. Unknown writer role for a drawer → 403.

## 2. Boot and configuration

- Entry: `uv run main.py` → uvicorn on `0.0.0.0:$GATEWAY_V9_PORT`
  (default **8109**). Only `GATEWAY_V9_PORT` is honored; a stale
  `GATEWAY_V3_PORT` line is deliberately ignored.
- `.env` load order: `llm_gatewayV9/.env` first (provider + channel keys),
  then the parent project `.env` as non-overriding fallback.
- `lifespan()` builds once: SQLite (`db.init`), Gemini prompt cache,
  worker providers, router pool, embedders. Key list (see `.env.example`):
  `GEMINI_API_KEY`, `NVIDIA_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`,
  `OPEN_ROUTER_API_KEY`, `GITHUB_ACCESS_TOKEN`, `KILO_API_KEY`, plus
  channel/integration keys (`TELEGRAM_BOT_TOKEN`, `GMAIL_TOKEN` (+ refresh
  triple), `GOOGLE_CALENDAR_TOKEN`, `GITHUB_TOKEN`, `SLACK_BOT_TOKEN`,
  `NOTION_TOKEN`, `TAVILY_API_KEY`, …). `check_keys.py` audits presence
  by name. `gmail_oauth_setup.py` is the one-time Gmail consent helper.
- Order vars: `LLM_ORDER` (worker failover ladder), `ROUTER_ORDER`
  (router-LLM ladder), `EMBED_ORDER`. `agent_routing.yaml` maps
  `agent:` tags to preferred providers — **intentionally empty**: all
  traffic routes freely through the natural ladder.
- Static UI: `/` → `static/dashboard.html` (pools, channels, policy,
  memory, spend, integrations, voice, quick test, calls), `/help` →
  `static/help.html` (endpoint reference table).

## 3. Chat pipeline (`POST /v1/chat`)

In order, per request:

1. **Normalize** — `prompt`→messages, system blocks, token estimate.
   Any http(s) image URL is fetched once and inlined as a data: URL, so
   providers only ever see data: URLs.

   > **Two system-prompt channels, and only one of them is safe to ignore.**
   > A request may carry its instructions *inside* `messages` (as
   > `role: "system"` turns) *or* in the top-level `system` field, which
   > becomes `system_blocks` → `systemInstruction`. They are **not**
   > interchangeable. `OpenAICompatProvider` and `OllamaProvider` forward
   > inline system turns when `system_blocks` is absent, but
   > **`GeminiProvider` cannot**: Gemini's `contents` array accepts only
   > `user`/`model`, so every inline system turn is dropped. Anything sent
   > that way — a persona, or the `REFERENCE MATERIAL` block Aria injects
   > before a user's question — reaches the provider, gets counted in
   > `prompt_chars`, gets billed, and is **never seen by the model**.
   > `GeminiProvider._inline_system()` therefore re-attaches them to
   > `systemInstruction`, after `system_blocks`. If you add a provider,
   > assert on the outbound body, not on the request you sent.

   > Related, in `main.py`: `_system_blocks` reads `req.system` only, and
   > `_normalize_messages` passes `messages` through untouched — so a
   > client that hoists its system prompt out of `messages` into `system`
   > is relying on the rule above holding for its provider.
2. **Provider resolution** — explicit `provider=` → `model=` via
   `MODEL_ROUTES` → `agent_routing.yaml` pin → `auto_route` tier → default
   ladder. Single-candidate explicit pins wait up to 30s on cooldown
   instead of 503-ing.
3. **`auto_route`** (`perception`|`memory`|`decision`) — a *separate*
   RouterPool of small LLMs classifies TINY/LARGE/HUGE from an ~800-char
   envelope (never the full payload). `>8000` tokens short-circuits to a
   HUGE 503 (no summarizer exists). All-router failure → deterministic
   token-count rule with `fallback_used=true`. Tier selects failover
   order (`TIER_TO_ORDER`: TINY favors small/fast, LARGE favors
   long-context Gemini).
4. **Candidate pick** — capability filter (tools/reasoning/structured/
   vision), `max_ctx` fit, then RPM/RPD/TPM/cooldown/backoff. Every skip
   lands in `attempted[]` so callers see *why*.
5. **Execute** — one same-provider retry on transient 5xx/timeout (≤2s
   backoff, surfaced as `retries`), then provider failover. Backoffs:
   429→15s–1h by flavor, 5xx→20s, timeout→10s, 401/403→10-min blackball
   (skipped when the caller pinned a model: "model unavailable", not
   "key dead"). Explicit-provider failures surface as 502, no silent
   fallthrough.
6. **Structured output** — JSON-schema validated with one corrective
   retry, else 503. Streaming supported (SSE `delta`/`tool_call_delta`/
   `done`/`error`; streamed tokens estimated chars//4 in the ledger).
7. **Ledger write** — every attempt logged with `call_role`, agent/session
   tags, tokens, latency, attempts, retries.

Siblings: `POST /v1/chat/batch` (bounded parallelism, input order kept),
`POST /v1/vision` (single-image shim forcing vision-capable routing),
`POST /v1/embed` (413 over 8000 chars — callers chunk; pinned provider
errors surface faithfully as 429/400/502).

## 4. The three pools

**Workers** (`providers.py`) — built only when their key exists:

| Name | Class | Default model | Notes |
|---|---|---|---|
| gemini / gemini35lite | GeminiProvider | gemini-2.5-flash / gemini-3.5-flash-lite | tools, vision, prompt cache, thinking knobs |
| nvidia | NvidiaProvider | deepseek-ai/deepseek-v3.2 | OpenAI-compat |
| groq | GroqProvider | openai/gpt-oss-120b | OpenAI-compat |
| cerebras | CerebrasProvider | gpt-oss-120b (zai-glm-4.7 archived 2026-09; account currently 402s on all Cerebras models → 5-min backoff + failover covers) | OpenAI-compat |
| openrouter | OpenRouterProvider | nvidia/nemotron-3-super-120b-a12b:free | OpenAI-compat |
| github | GitHubProvider | openai/gpt-4.1-mini | OpenAI-compat |
| kilo | KiloProvider | tencent/hy3:free | reasoning forced OFF unless asked |
| ollama | OllamaProvider | `$OLLAMA_MODEL`, gated by `ENABLE_OLLAMA_LLM` | local, no tools-parallel/cache |

Each class normalizes its dialect to one result shape (`text`,
`tool_calls`, token counts, `tool_call_dialect` native|prompted_fallback|
none, `reasoning_applied`). Degraded upstreams get automatic retries
*without* `reasoning_effort` / strict `json_schema` before failing.

**Routers** (`router.py` `RouterPool`: cerebras/groq/nvidia/github)
— own rate state, so router quota never eats worker quota. Router model
defaults live in `ROUTER_DEFAULTS` (cerebras: qwen-3.8-27b — the old
llama3.1-8b left the account catalogue in 2026; verify against the live
`/v1/models` catalogue if routing degrades). Per-provider
limits live in `LIMITS` (rpm/rpd/tpm/cooldown/max_ctx + cerebras daily
token cap); shortcuts in `SHORTCUTS` (`g`, `n`, `o`, `gr`, `c`, `or`,
`gh`, `k`, …). `DEFAULT_ROUTER_ORDER` names only builders that exist
(a name without a builder is silently filtered — keep them in sync).

**Embedders** (`embedders.py`) — Ollama `nomic-embed-text` only
(**768-dim pinned**; the Gemini fallback was removed as dead weight),
`keep_alive=-1` so the model stays in VRAM, per-embedder backoff
(5/10/15s sticky). `embed_with_failover` gates on rate state first.

## 5. Ledger, cost, observability

- `db.py` → `gateway_v8.db`, table `calls`: every call from every plane
  (`call_role`: worker/router_*/embed/channel/integration/control/memory/
  voice) with agent/session/channel/trust/policy columns. Self-migrating
  schema. (The 0-byte `gateway_v9.db` stub was removed; the v8 filename is
  historical — do not rename without migrating readers.)
- `pricing.py` list-rates → dollars on `/v1/cost/by_agent` and unified
  `/v1/spend` (rows + totals + cap comparison). Tokens are truth; dollars
  are readability.
- `cache.py`: Gemini-only prompt cache (SHA-256 of system text, 5-min
  TTL); OpenAI-compat relies on byte-stable system blocks upstream.
- Read APIs: `/v1/calls`, `/v1/status`, `/v1/routers`, `/v1/providers`,
  `/v1/capabilities`, `/v1/embedders`. Reference client: `client.py`
  (`LLM` class the agent loads).

## 6. Adaptor plane (channels outward, policy inward)

- **16 adaptors**, one interface (`adaptors/base.py`): `send` /
  `normalize` / `verify_webhook` / `health`, capability flags,
  `required_keys()`. `NotConfigured` (no creds) vs `NotIntegrated`
  (scaffolded, deliberately unwired). Lazy `registry.py` — one broken
  import can't kill the rest; `inventory()` leaks values never.
  Transports share `adaptors/http.py` (one timeout/UA/redirect policy);
  stdlib seams (smtplib, subprocess) stay per-adaptor.
- **Outbound** (`POST /v1/channels/{name}/send`): policy check →
  deny=403 / approve→pending approval (retry later) / proceed → send in a
  worker thread → ledger with channel+trust+policy columns. Token-expiry
  maps to `PermissionError` → refresh hint, never a raw 401.
- **Inbound** (`/v1/hooks/{name}`): signature verify → normalize to a
  trust-stamped envelope → policy check → ledger. Handshakes (Meta
  hub-mode, Slack url_verification) answered before signature checks so
  operator setup can complete.
- **Trust** (`adaptors/trust.py`): `pairing.json` maps sender→role;
  unlisted senders are `untrusted`; pairing happens only via loopback
  control plane, never chat. IDs masked (last-4) on display.
- **Policy** (`policy/`): first-match-wins, default-deny, dry-run
  globally or per-rule, hot-reloaded on file mtime; `evaluate` + `reload`
  endpoints; fail-closed wrapper (engine error = deny). Spend caps feed
  `over_cap`. `approvals.py`: pending-approval store for `approve`
  verdicts (file-backed, human resolves, caller retries).
- **Integrations** (gmail/calendar/github/notion/websearch):
  same policy gate, thin dispatch to fail-soft modules (`{ok:false}` +
  key name, never raise), scrubbed errors, ledger-logged. Shapes are
  byte-identical to the old agent-local tools.
- **Control** (loopback-only): presence, pairing ceremony, armless-by-
  default kill switch (two separate opt-ins).
- **Voice** (`voice_api.py`): Kokoro TTS + Whisper STT, lazy-loaded so
  boot never needs model files; endpoints 503 with a clear message when
  absent. TTS/STT successes and failures ledger-log (`call_role="voice"`).
  Agent voice routes are thin proxies.

## 7. Memory plane (`memory/` + `memory_api.py`)

Single-writer home for durable records. Layout under `state/`:

```
state/memory.json + index.faiss   legacy (pre-drawer items, FROZEN:
                                  reads only + global clear; stored bytes
                                  are never rewritten)
state/memory/<drawer>/           one JSON + FAISS per drawer; all new
                                  writes land here
```

- **Record** (`models.py`): `MemoryRecord` = agent `MemoryItem` fields +
  `drawer`, `scope{tenant,project,user,agent}` (single-scope defaults),
  `sources[]`, `principal{id,role}`, `expires_at`,
  `supersedes/superseded_by`, doc span. `kind` stays the agent-facing
  vocabulary; drawer is governance. Pre-drawer items validate with
  `drawer=None`.
- **Drawers**: working (run-scoped, 60-min default TTL) · episode (run
  events, recency queries) · fact (stable claims, supersede chains) ·
  playbook (promotion-only) · policy (operator/system only) · audit
  (gateway-append-only, survives wipes) · document (spans with
  doc_id/version/chunk_index, version flips, neighbor previews).
- **Cabinet** (`plane.py`): kind→drawer routing (chunk payloads always
  land in document), fail-closed writer map
  (remember→agent, outcomes→runtime, indexer→document, operator→policy,
  gateway→audit, system→playbook), one query embed fanned out across
  drawers, drawer-first merged recall, RRF-fused vector+keyword per store
  (k=60), TTL sweeps (startup/per-write/endpoint), run-end purge support,
  orphan-index reconcile at boot, dim-drift refusal.
- **Routes** (`memory_api.py`): search/remember/record_outcome/list/
  stats/clear/sweep/episodes/playbook propose+approve. Permission
  failures → 403 (audited), ghost supersede targets → 404, oversize
  payloads → 413 (100KB structured cap), dim drift → 503. Search `top_k`
  clamps to 100. All mutating/search ops ledger-logged
  (`call_role="memory"`); list/stats/episodes are read-cheap and
  unlogged. State dir overridable via `GATEWAY_MEMORY_STATE` (tests +
  ops).

## 8. Failure map

`{ok:false}` = expected-unconfigured · 400 = bad shape/unknown drawer ·
404 = unknown name / ghost link · 422 = schema/normalize fail ·
429 = rate-limited · 502 = pinned provider died · 503 = all unavailable /
dim drift / models missing · 501 = scaffolded-not-wired.

## 9. File map + tests

```
main.py · schemas.py · client.py · check_keys.py · gmail_oauth_setup.py
router.py · providers.py · embedders.py · cache.py · db.py · pricing.py
channels_api.py · integrations_api.py · voice_api.py · memory_api.py
approvals.py · scrub.py · atomic_json.py · _adaptor_kit.py
adaptors/{base,envelope,http,registry,trust}.py + 16 × {adapter,schemas,verify?,test_adapter}.py
integrations/{gmail,calendar,github,notion,websearch,slack}.py + test_integrations.py
policy/{engine.py,policy.yaml} + test_policy.py
memory/{models,store,vector,service,plane}.py
tests/{test_memory,test_drawers,test_adaptors,test_adaptor_contract,test_embed,test_vision_*}.py
static/{dashboard.html,help.html} · agent_routing.yaml · state/ · logs/
```

Every folder carries its own fundamental suite, runnable without servers
(`pytest adaptors/telegram`, `pytest integrations`, `pytest policy`,
`pytest memory`…); `tests/` holds the integration tiers. Markers:
`local` (needs Ollama), `network` (needs internet/keys). Known-stale:
`tests/test_all_providers.py` (missing fixture) and the `network`
Gemini-embed tests (fallback removed) — both pre-date current work.
