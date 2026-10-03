# Agent Test Plan — Gap Analysis & Roadmap

**Generated:** 2026-08-22
**Baseline:** Full source re-read of `S9SharedCode/code/` (all `.py`/`.md`/`.js`/`.html` except `.env`/secrets/venv/caches/state/archive).
**Current suite:** `pytest tests/` → 73 passed, 6 skipped (live), 0 failed, 0 errors.

The existing suite is strong on *unit* layers (recovery classifier, critic splice,
computer-use layers, calculator pipeline) but has **no integration tests** for the
behaviors that actually make this an agent. The table below ranks every missing test
by impact (does it guard a real user-facing behavior?) and effort.

---

## Coverage map (existing vs needed)

| Area | Existing tests | What's missing |
|---|---|---|
| Orchestrator (`flow.Executor`) | Graph unit ops only | **Full DAG execution with a mock LLM** — planner→worker→formatter, fan-out, recovery, node cap |
| Memory (`memory.py`) | none | remember / read / search / FAISS+keyword fallback / persistence across restart |
| MCP tools (`mcp_server.py` 22 tools) | none | Each tool callable, returns expected shape, fail-soft on error |
| Cost ledger | none (manual only) | per-turn `turn_costs.json` accumulates; `/api/cost` scoped to conversation |
| Multi-turn continuity | none | `conversation_id` → same session_id; memory hits visible to later turns |
| Browser skill cascade | none | L1 extract → L2b a11y → L3 vision escalation with mocked gateway |
| Safety gates (approval flow) | unit (cmd_blocked/path_blocked) | dry-run vs live, pending→approved→executed, audit log records it |
| Server routes | route presence only | chat SSE frame shapes, `/api/cost` math, artifact serving |
| TTS | none | `/api/tts` returns audio/wav, non-empty |
| Scheduler | none | schedule list/get/run |
| Capability sweep | standalone script (not pytest) | convert `test_agent_capabilities.py` tasks into `@pytest.mark.network` tests |

---

## Priority 1 — integration (highest value, run in CI)

These guard the core "is this an agent" behaviors with a **mock LLM** so they're
deterministic and need no credentials.

### P1.1 Orchestrator integration (`test_orchestrator_integration.py`)
Mock `skills.LLM` so `.chat()` returns canned JSON per skill name, then drive
`flow.Executor.run()` end-to-end.

| # | Test | Assert |
|---|---|---|
| 1 | Minimal query | planner→formatter DAG; `done` answer non-empty; 2 nodes executed |
| 2 | Fan-out query | planner emits N parallel workers; all N complete; single formatter; no `ExceptionGroup` |
| 3 | Recovery on transient failure | one researcher fails `503`; recovery planner re-plans only that node; run still completes |
| 4 | Node cap (`MAX_NODES=60`) | a planner that loops adding nodes stops at 60 cleanly, no infinite loop |
| 5 | Malformed planner JSON | planner emits bad NodeSpec → node fails loudly with error text; run does not crash |
| 6 | Memory hits threaded through | memory pre-seeded; every skill prompt in the run contains the hit text |

### P1.2 Memory service (`test_memory_service.py`)
No LLM needed (memory has its own lightweight embed path; mock `gateway.embed`).

| # | Test | Assert |
|---|---|---|
| 1 | remember→read round-trip | store a fact; `read(similar_query)` returns it |
| 2 | FAISS vector path | after `index_document`, semantic search finds paraphrase (not just keyword) |
| 3 | Keyword fallback | with embed disabled, `read` still finds by keyword overlap |
| 4 | Persistence | write, re-instantiate service (new process simulation), read still returns it |
| 5 | Kind classifier | free-form text → `fact`/`preference`/`scratchpad` tagged correctly |
| 6 | Empty/corrupt state file | corrupt `memory.json` → graceful empty list, no crash |

---

## Priority 2 — component (run in CI, some need mock gateway)

### P2.1 MCP tool smoke tests (`test_mcp_tools.py`)
Call each of the 22 `mcp_server` tools directly (they're fail-soft).

| # | Test | Assert |
|---|---|---|
| 1 | `get_time` | returns a timestamp string, no error |
| 2 | `get_weather` (mocked) | returns structured weather or fail-soft error, never raises |
| 3 | `read_file` / `list_dir` | read a real sandbox file; list returns array |
| 4 | `web_search` (mocked) | returns results array or fail-soft |
| 5 | All 22 tools callable | iterate `__all__`/`tools` list; each returns a dict with `status` |

### P2.2 Browser skill cascade (`test_browser_skill.py`, mocked gateway)
Inject a fake `V9Client` returning canned HTML/AX/vision.

| # | Test | Assert |
|---|---|---|
| 1 | L1 extract short-circuits | static page → `path=extract`, 0 LLM calls |
| 2 | L2b a11y drives interactive page | form buttons clicked via AX tree |
| 3 | L3 vision escalation | canvas-only target (empty legend) → escalates to vision |
| 4 | `gateway_blocked` → error_code | CAPTCHA page → returns `error_code="gateway_blocked"`, no crash |

### P2.3 Cost ledger (`test_cost_ledger.py`)
| # | Test | Assert |
|---|---|---|
| 1 | Per-turn write | after a run, `turn_costs.json` has one entry per completed node |
| 2 | Conversation scoping | `/api/cost?conversation_id=X` sums only X's turns, not lifetime |
| 3 | `meta.cost_usd` == ledger sum | the SSE `meta` frame matches the JSON ledger |

---

## Priority 3 — live integration (needs running server, marked `@pytest.mark.live`)

These require `agent_server` on :8500 and gateway on :8109. Convert the existing
`test_agent_capabilities.py` standalone script into pytest form so it's runnable.

| # | Test | Assert |
|---|---|---|
| 1 | Simple chat | `/api/chat` → `done` frame, non-empty answer |
| 2 | Multi-turn memory | turn B references turn A's answer (same `conversation_id`) |
| 3 | Computer-use calculator | live: `compute 234 * 567` → answer contains `132678` |
| 4 | Browser (real) | fetch example.com → answer contains "Example Domain" |
| 5 | TTS | `/api/tts` → `audio/wav`, >0 bytes |
| 6 | Artifact serving | browser run → `browser_artifacts` populated → artifact URL returns image |

---

## Priority 4 — resilience & edges

| # | Test | Assert |
|---|---|---|
| 1 | Gateway offline | `ensure_gateway` down → graceful error frame, no 500 |
| 2 | Huge input (>8K tokens) | returns 503-style refusal, stream ends clean |
| 3 | MAX_NODES cap | loop stops at 60 |
| 4 | Scheduler CRUD | create/get/run a scheduled task |

---

## Recommended execution order

1. **P1.1 + P1.2** first — they're pure-mock, run in CI, guard the most behavior.
2. **P2.1 + P2.3** next — no credentials, fast.
3. **P2.2** — needs the mocked-`V9Client` harness.
4. **P3** — only when a developer has the server running locally.
5. **P4** — stretch; most logic already unit-covered.

All Priority 1–2 tests use a **mock LLM** so they're deterministic and CI-safe.
