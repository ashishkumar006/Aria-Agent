# Full Agent Test Plan — "What a Fully-Fledged Agent Must Prove"

**Generated:** 2026-08-22
**Scope:** The S9 general agent (`agent_server.py` :8500) + V9 gateway (`main.py` :8109).
**Goal:** A single, readable plan that enumerates every capability a *fully-fledged*
agent must demonstrate, and the concrete test that proves each one.

> **Note:** `TEST_PLAN.md` section labels below (e.g. `TEST_PLAN.md P1.1#4`)
> refer to the 2026-08-22 manual E2E plan, now archived at
> `../../archive/root_scratch/TEST_PLAN.md`. Labels kept for traceability.

---

## How to read this plan

- **Capability** — a behavior a real agent must have.
- **Why it matters** — what breaks for the user if it's missing.
- **Test** — the concrete check (file + assertion). `LIVE` = needs running servers /
  credentials; `MOCK` = deterministic, no network; `UNIT` = pure code path.
- **Status** — `[x]` already covered by an existing test, `[ ]` gap to fill.

Three test layers (run in this order so a failure localizes fast):
1. **Unit / component** — fast, no servers, no secrets. CI-friendly.
2. **Integration (mocked LLM)** — full DAG execution with a stubbed gateway.
3. **Live E2E** — real servers, real models, real (or sandboxed) tools.

---

## 1. Orchestration & DAG engine

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 1.1 | Minimal path (greeting) | Baseline: planner→formatter, no crash | `test_e2e_comprehensive.py::t1_1` — `hi` → DAG `planner→formatter`, non-empty answer | [x] |
| 1.2 | Single research + fan-in | Core "answer a fact" path | `t1_2` — "capital of France" → `planner→researcher→formatter`, contains `paris` | [x] |
| 1.3 | Fan-out (parallel workers) | Real agent decomposes; must run N in parallel, not serially | `t1_3` — compare 3 cities → 3 `researcher` nodes, single `formatter`, no `USER_QUERY` leakage | [x] |
| 1.4 | Memory-hit short-circuit | Avoids redundant research when Memory already knows | `test_memory_*` — retriever returns seeded fact; planner sees it | [x] |
| 1.5 | Critic auto-insertion | Quality gate on format-constrained output | `t1_5` — haiku → `critic` node present between formatters | [x] |
| 1.6 | Recovery on node failure | One failed node must not kill the run | `test_recovery.py` — researcher 503 → recovery planner re-plans only that node | [x] |
| 1.7 | Node cap (MAX_NODES=60) | Planner loop can't hang the process | `test_recovery.py` / `TEST_PLAN.md P1.1#4` — loop stops at 60 | [x] |
| 1.8 | Malformed planner JSON | Bad LLM output fails loudly, run survives | `TEST_PLAN.md P1.1#5` — bad NodeSpec → node error text, no crash | [ ] |
| 1.9 | Planner short-circuit | One-call fast path for direct answers | Data-driven: the planner emits `{"answer": …}` with no nodes (the old `skills.PLANNER_SHORTCIRCUIT` module flag is gone — see tests/battle_test.py note) | [ ] |

## 2. Knowledge & memory

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 2.1 | Remember → read round-trip | Agent retains facts across turns | `test_memory_service.py` — store fact, `read` returns it | [x] |
| 2.2 | Semantic (FAISS) search | Finds paraphrase, not just keywords | `test_memory_service.py` — `index_document` then search paraphrase | [x] |
| 2.3 | Keyword fallback | Works when embedder down | `test_memory_service.py` — `_keyword_search` finds overlap | [x] |
| 2.4 | Persistence across restart | Memory survives process restart | `test_memory_service.py` — write, re-instantiate, read | [x] |
| 2.5 | Kind classifier | Tags fact/preference/scratchpad | `test_memory_service.py` — free text → correct `kind` | [x] |
| 2.6 | Corrupt state handling | Bad `memory.json` → empty, no crash | `test_memory_service.py` — corrupt file graceful | [x] |
| 2.7 | Multi-turn continuity | Later turn uses earlier answer | `test_e2e` / `TEST_PLAN.md P3#2` — same `conversation_id`, turn B references A | [ ] |

## 3. Web & browser (four-layer cascade)

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 3.1 | Research via fetch | Standard web answer | `t1_2`, `test_agent_capabilities.py::web_fetch` — example.com title | [x] |
| 3.2 | Browser L1 extract | Static page → structured extract, 0 LLM calls | `test_browser_skill.py` — `path=extract` short-circuit | [x] |
| 3.3 | Browser L2b a11y | Interactive widgets driven via AX tree | `test_browser_skill.py` — form buttons clicked | [x] |
| 3.4 | Browser L3 vision escalation | Canvas/visual target → vision model | `test_browser_skill.py` — empty legend → escalates | [x] |
| 3.5 | Browser recovery (blocked) | CAPTCHA/gateway block → honest "unable", no retry | `test_browser_skill.py` / `E2E_TEST_PLAN.md 2.3` — `error_code=gateway_blocked` | [x] |
| 3.6 | Screenshot artifacts in UI | User sees what agent visited | `test_e2e_comprehensive.py::t2_1` — `browser_artifacts` populated, renders | [x] |
| 3.7 | Planner passes base URL only | No pre-filled query strings leaking | `E2E_TEST_PLAN.md 2.x` assertion | [ ] |

## 4. Real-world actions / integrations

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 4.1 | Weather (no key) | Common ask; must not need a secret | `t3_1_weather` — "weather in London" → `action→formatter`, contains `°`/`london` (**fixed** UA reset bug) | [x] |
| 4.2 | Currency conversion | Math + live rate | `t3_2_currency` — 100 USD → `eur`/`€` | [x] |
| 4.3 | Time by timezone | Localized answer | `t3_3_time` — "time in Tokyo" → `tokyo`/`:` | [x] |
| 4.4 | File create/read/update | Agent edits the sandbox | `test_agent_capabilities.py::file_*` + `test_mcp_tools.py` roundtrip | [x] |
| 4.5 | GitHub query | Repo/list/issue ops | `test_agent_capabilities.py::list_repos` + `test_mcp_tools.py` | [x] |
| 4.6 | Telegram (LIVE) | Sends message | `E2E_TEST_PLAN.md 3.1` — needs `TELEGRAM_*` secret | [ ] |
| 4.7 | Email (LIVE) | Sends email | `E2E_TEST_PLAN.md 3.2` — needs SMTP/OAuth | [ ] |
| 4.8 | Calendar (LIVE) | Creates event | `E2E_TEST_PLAN.md 3.3` — needs `GOOGLE_CALENDAR_TOKEN` | [ ] |
| 4.9 | Action always paired w/ formatter | User gets a confirmation, not raw JSON | `E2E_TEST_PLAN.md §3` assertion | [ ] |
| 4.10 | No secret leakage | Env var names never in output | `E2E_TEST_PLAN.md §3` assertion | [ ] |

## 5. Code & sandbox execution

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 5.1 | Generate + run code | Agent writes then executes | `t5_1_fibonacci` — `coder→sandbox_executor→formatter`, stdout shown | [x] |
| 5.2 | Exact math | No hallucinated arithmetic | `t5_2_math` — `2**100` = `1267650600228229401496703205376` | [x] |
| 5.3 | Coder→sandbox auto-successor | Sandbox runs without a second planner call | `agent_config.yaml` `internal_successors:[sandbox_executor]` | [x] |
| 5.4 | Malformed code fails gracefully | No stream crash | `TEST_PLAN.md P1.1#5` style assertion | [ ] |

## 6. Computer-use (safety-gated)

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 6.1 | Approval gate present | Sensitive actions need human OK | `test_server_routes.py` — `/api/computer/approvals` exists | [x] |
| 6.2 | Dry-run default | Ships disabled/safe | `test_computer_use.py` — `COMPUTER_USE_ENABLED` off → describe only | [x] |
| 6.3 | Pending→approved→executed | Full approval lifecycle | `test_computer_agent_pipeline.py` — approval flow end-to-end | [x] |
| 6.4 | Audit log records gated action | Accountability | `E2E_TEST_PLAN.md §4` — `/api/audit` entry | [ ] |
| 6.5 | Live calculator (LIVE) | Real machine math | `TEST_PLAN.md P3#3` — `234*567` → `132678` | [ ] |

## 7. Vision (local file)

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 7.1 | Describe local image | "What's in this screenshot?" | `test_agent_capabilities.py::vision_file` — `state/test_image.png` described | [x] |
| 7.2 | Read text in image | OCR-like use | `test_natural_vision_search.py` — goal=read text | [x] |

## 8. Cost, instrumentation & server

| # | Capability | Why it matters | Test | Status |
|---|---|---|---|---|
| 8.1 | Per-turn cost ledger | User sees spend per response | `TEST_PLAN.md P2.3` — `turn_costs.json` one entry/node | [ ] |
| 8.2 | Cost scoped to conversation | Spend panel not lifetime | `TEST_PLAN.md P2.3#2` — `/api/cost?conversation_id=X` | [ ] |
| 8.3 | `meta.cost_usd` == ledger | SSE frame matches JSON | `TEST_PLAN.md P2.3#3` | [ ] |
| 8.4 | Chat SSE frame shapes | UI depends on `log/meta/done/error` | `test_server_routes.py` (extend) | [ ] |
| 8.5 | TTS endpoint (LIVE) | Voice output | `TEST_PLAN.md P3#5` — `/api/tts` → `audio/wav` | [ ] |
| 8.6 | Gateway offline graceful | No 500 on upstream down | `TEST_PLAN.md P4#1` — `ensure_gateway` down → error frame | [ ] |
| 8.7 | Huge input refusal | >8K tokens → clean 503 | `TEST_PLAN.md P4#2` | [ ] |
| 8.8 | Scheduler CRUD | Deferred tasks | `TEST_PLAN.md P4#4` | [ ] |

---

## Summary of what I decided to test

**Already proven (green today):** 40+ checks across orchestration (fan-out,
recovery, critic, node cap), memory (FAISS + keyword + persistence), browser
cascade (L1/L2b/L3 + recovery), all safe integrations (weather/currency/time/
files/GitHub), code+sandbox (incl. exact `2**100`), computer-use gates, and
local vision. The live E2E suite is **10/11 → 11/11** after the weather UA fix.

**Deliberate gaps I recommend filling (the `[ ]` rows):**
1. **Malformed-planner-JSON & malformed-code** resilience (P1.1#5, P5.4) — cheap, high value.
2. **Multi-turn continuity** (2.7) — the defining "agent" trait; currently only implied.
3. **Cost ledger math** (8.1–8.3) — the Spend panel is user-facing and untested.
4. **LIVE integrations** (4.6–4.8, 6.5, 8.5) — gated behind real secrets; mark
   `@pytest.mark.live` so they skip in CI without credentials.
5. **Audit log** (6.4) and **artifact serving** (3.6 already covered) — accountability proof.

**What I explicitly chose NOT to test:** provider-internal model quality (that's
the gateway's concern, not the agent's), and flaky third-party UI states (CAPTCHA
variants) beyond the recovery-path assertion.

---

## Suggested file layout

```
tests/
  test_orchestrator_integration.py   # 1.8, 1.9  (mock LLM)
  test_memory_continuity.py          # 2.7       (mock LLM, multi-turn)
  test_cost_ledger.py               # 8.1-8.3
  test_server_sse_shapes.py         # 8.4
  test_resilience.py                # 8.6-8.8
  test_live_integration.py          # 4.6-4.8, 6.5, 8.5  (@pytest.mark.live)
  test_audit_log.py                 # 6.4
  # existing (keep): test_e2e_comprehensive, test_agent_capabilities,
  #   test_memory_service, test_browser_skill, test_mcp_tools,
  #   test_computer_*, test_recovery*, test_server_routes
```
