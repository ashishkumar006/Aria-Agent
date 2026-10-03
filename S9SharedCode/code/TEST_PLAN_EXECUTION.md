# S9 "Aria" Agent — Static & Logic Test Plan

**Goal:** Verify the agent's *code, logic, files, and working* across all
aspects. This plan covers both static analysis (no live agent run) AND live
testing (driving real queries through `/api/chat`). Phases 0-12 are static;
phases 13+ are live. Each phase lists concrete checks. Phases are ordered so
cheap, high-signal checks run first.

**Environment facts (captured up front):**
- S9 venv: `S9SharedCode/code/.venv` → Python **3.11.13** (core deps import OK).
- Gateway: a V9 instance was already running on **:8101** (PID 16808). S9's
  `gateway.py` hardcodes `GATEWAY_URL = "http://localhost:8109"` → **port
  mismatch (BUG-ENV-1)**. A second V9 instance was started on **:8109**.
- `uv run` failed earlier only because the gateway `.env` sets
  `GATEWAY_V3_PORT=8101` and 8101 was occupied; `import agent_server` works
  fine under the venv.
- **Provider status (as of testing):** gemini heavily rate-limited (26/27
  failed), nvidia slow (42s avg), cerebras dead (0/10). **Kilo is healthy**
  (0/60 RPM, no rate limit) → use Kilo for all live tests.

**Estimated effort:** ~6 hours across 18 phases.

---

## Phase 0 — Inventory & environment (15 min)
- [ ] List every `.py` under `S9SharedCode/code` (exclude `.venv`, `__pycache__`).
- [ ] Confirm venv Python version and that `fastapi, networkx, pydantic, yaml,
      faiss, httpx` import.
- [ ] Confirm gateway reachable on :8109 (`GET /v1/routers`, `/v1/embedders`).
- [ ] Record the port-mismatch finding (S9 expects 8109; running gateway on 8101).

## Phase 1 — Syntax / compile check (20 min)
- [ ] `python -m py_compile` every `.py` file (recursive). Expect 0 failures.
- [ ] Re-run after touching nothing to confirm determinism.
- [ ] Note any file the compiler flags; read and triage.

## Phase 2 — Import health of every module (30 min)
- [ ] Import each top-level + package module under the venv:
      schemas, gateway, memory, perception, decision, flow, recovery, skills,
      action, artifacts, persistence, scheduler, templates, report, replay,
      vector_index, mcp_runner, mcp_server, sandbox, browser.skill,
      computer_use, computer_use.engine, computer_use.safety.gates,
      computer_use.safety.permissions, computer_use.daemon, computer_use.shell.
- [ ] For each import failure: capture the traceback, classify
      (missing dep / circular import / syntax / env), and record.
- [ ] Confirm `agent_server` imports (it already did) and that FastAPI app
      object builds (import `agent_server` then `agent_server.app`).

## Phase 3 — Schema contract tests (40 min)
Pure Pydantic, no network.
- [ ] `MemoryItem`: build with/without `embedding`; round-trip
      `model_dump` → `model_validate`; verify `embedding=None` survives.
- [ ] `AgentResult`: set `error_code` to each `ErrorCode` literal; verify
      invalid code rejected; round-trip through `model_dump(mode="json")`.
- [ ] `BrowserOutput`: each `path` literal; `model_validate` rejects bad path.
- [ ] `NodeState`: build with a nested `AgentResult`; round-trip; confirm
      `result` stays typed (not dict) after `model_validate`.
- [ ] `Goal` / `Observation`: `all_done`, `next_unfinished` properties.
- [ ] `DecisionOutput`: `is_answer` true when `answer` set, false when
      `tool_call` set; both-None and both-set edge cases.
- [ ] `ToolCall`, `NodeSpec`, `Artifact`: basic construction + round-trip.

## Phase 4 — Persistence & artifacts (40 min)
- [ ] `SessionStore`: write/read `query.txt`; write/read a `NodeState` with a
      nested `AgentResult`; confirm JSON graph round-trip preserves typed
      `result` (the `_result_typed` revive path).
- [ ] Corrupt-graph handling: write a `graph.json` whose node `result` is an
      invalid dict; confirm `read_graph` raises `SessionLoadError` (not silent).
- [ ] `read_all_nodes`: drop a corrupt `n_*.json`; confirm it is skipped with
      a warning, not a crash.
- [ ] `artifacts.put`/`get_bytes`/`get_meta`/`exists`: round-trip a blob;
      confirm content-addressing dedup (same bytes → same id).
- [ ] Atomic-write helper: confirm tmp+rename leaves no `.tmp` behind.

## Phase 5 — Memory service + vector index (50 min)
- [ ] `vector_index.VectorIndex`: add vectors of fixed dim; `search` returns
      ranked ids; dim-mismatch on `add` raises; `clear`/persist/reload cycle.
- [ ] `_l2_normalize`: zero-vector returns unchanged; normalized vector has
      unit norm.
- [ ] `memory._keyword_search`: stopword stripping; score ordering.
- [ ] `memory._try_embed`: with gateway up on :8109, embed a short string →
      list[float] of length 768; confirm failure path returns None gracefully
      when gateway down (simulate by pointing at a dead port).
- [ ] `memory.read` path: build a few `MemoryItem`s in memory, confirm
      vector search returns the closest; fallback to keyword when no vectors.
- [ ] `memory.remember`/`read` round-trip via the JSON store + FAISS cache
      (use a temp STATE_PATH to avoid polluting real memory.json).

## Phase 6 — Recovery & orchestrator graph logic (60 min)
- [ ] `recovery.classify_failure`: parametrized over transient / validation /
      environmental / upstream strings (mirror test_recovery.py cases).
- [ ] `recovery.plan_recovery`: transient→retry, validation→skip,
      planner-upstream→skip, other-upstream→replan (with failure_report).
- [ ] `recovery.handle_critic_verdict`: fail-verdict splices a planner node;
      pass-verdict returns False; per-target cap (second fail → cap hit).
- [ ] `flow.Graph`: `add_node`/`mark`/`ready_nodes` (predecessor satisfaction
      incl. `skipped`); `extend_from` dynamic successors + label resolution +
      `internal_successors` + critic auto-insertion (L1/L2a skip branch).
- [ ] `flow.Executor` dry construction: `Executor()` with gateway up; confirm
      it does NOT start a run (we only construct / inspect `MAX_NODES` cap
      logic via a tiny synthetic graph, not a real query).

## Phase 7 — Skills registry, dispatch & prompt rendering (50 min)
- [ ] `SkillRegistry`: loads `agent_config.yaml`; every skill name resolvable;
      `tools_allowed`/`internal_successors`/`critic`/`temperature` parsed.
- [ ] `resolve_inputs`: `USER_QUERY`, `n:<i>` (upstream `AgentResult`),
      `art:<sha>`, literal; confirm browser `content` truncation branch.
- [ ] `render_prompt`: USER_QUERY only when wired; QUESTION scoping; memory
      hits capped at 4; INPUTS capped at 20k chars.
- [ ] `parse_skill_json`: fenced JSON, bare JSON, trailing prose extraction,
      empty → {}.
- [ ] `_extract_json` (computer judge): strict/non-strict/free-form fallback.
- [ ] Data-driven planner short-circuit sanity: planner emits `{"answer": …}`
      with no nodes (the `PLANNER_SHORTCIRCUIT` module flag no longer exists).

## Phase 8 — MCP tool schemas & server surface (40 min)
- [ ] `skills._TOOL_CATALOG`: every tool name in `agent_config.yaml`
      `tools_allowed` lists exists in the catalog; required fields present.
- [ ] `mcp_server`: import + confirm `FastMCP` app builds; list registered
      tools (introspect `mcp`); confirm `web_search`/`fetch_url`/file tools
      present. (Do NOT actually crawl the web — just introspect.)
- [ ] `mcp_server._safe`: path-traversal rejection for `../` escapes sandbox.
- [ ] `mcp_runner` tool-result cache: in-process + disk-backed (`state/
      tool_cache.json`) get/put/eviction by TTL.

## Phase 9 — Scheduler, templates, sandbox, report, replay (45 min)
- [ ] `scheduler`: `schedule` "in 10m" / "daily@09:00" / "every 30m" / epoch;
      `_next_fire_from_cron` math; `list_schedules`/`cancel`; heap re-arm for
      recurring; persistence to `state/schedules.json`.
- [ ] `templates`: save/list/get/delete/render with `{var}` substitution;
      missing template → None.
- [ ] `sandbox.run_python`: execute trivial code → exit_code/stdout/files;
      timeout path; env whitelist; truncation caps. (Use a harmless script.)
- [ ] `report.build_report`: feed a synthetic session dir (graph.json +
      nodes) and confirm all 8 sections render without crashing.
- [ ] `replay.replay`: feed a synthetic session; confirm it walks nodes and
      `p`/`o` expanders work (drive via a piped stdin script, not interactive).

## Phase 10 — Computer-use safety & engine logic (60 min)
- [ ] `SafetyGates`: `enabled` from env; `mode` dry-run vs live;
      `needs_approval` whole-word tokenization (verify `Format-Table` does NOT
      trip `format`; `rm` does); `path_blocked` for Windows/Unix protected
      paths; `cmd_blocked` allow/deny lists; approval create/list/resolve.
- [ ] `SafetyGates.redact`: API keys, Bearer, passwords, sk-/AIza- tokens
      stripped; benign text untouched.
- [ ] `export_audit` redaction path.
- [ ] `engine.ComputerUseSkill`: disabled → `ComputerResult(False,"disabled")`;
      `_goal_is_shell_task` classification; `_require_effect` re-scan loop
      logic (mock daemon). (No real desktop daemon — exercise the decision
      branches with stubs.)
- [ ] `computer_use.shell.GatedShell`: dry-run describes; live executes a
      harmless command; deny-list blocks protected paths.

## Phase 11 — Gateway bridge & agent_server endpoints (45 min)
- [ ] `gateway.ensure_gateway`: with :8109 already up, returns immediately
      (idempotent); `_is_up()` true.
- [ ] `gateway.LLM` proxy: `chat`/`embed`/`vision`/`chat_batch`/`cost_by_agent`
      delegate to the V9 client (do ONE cheap `embed` call against :8109 to
      confirm the bridge works end-to-end without a full agent run).
- [ ] `agent_server` routes: import app; use FastAPI `TestClient` to hit
      `GET /api/health` (gateway_up true), `GET /api/cost` (empty session →
      zeros), `GET /api/templates`, `GET /api/schedule`, `GET /api/audit`
      (redacted). Confirm 200s and shapes. (Do NOT POST /api/chat — that
      triggers a real agent run.)
- [ ] `agent_server.resolve_session`: new conversation_id → stable sid;
      repeated call returns same sid; persisted to `state/conversations.json`.

## Phase 12 — Test-suite health & final report (40 min)
- [ ] `pytest --collect-only` for `tests/` (excluding `-m live`/`-m stress`):
      confirm all unit tests collect without import errors; count them.
- [ ] Run the **fast, non-live** unit tests that need no network/LLM where
      possible (recovery, schemas, safety, scheduler, templates, vector math,
      persistence). Capture pass/fail. (Live/computer/integration tests will
      self-skip — that's expected.)
- [ ] Cross-check `agent_config.yaml` skills vs `prompts/*.md` files exist
      (every `prompt:` path resolves).
- [ ] Cross-check `agent_config.yaml` `tools_allowed` vs `_TOOL_CATALOG`.
- [ ] Compile the final findings `.md`: bugs, dead code, inconsistencies,
      port-mismatch, and a per-phase pass/fail table.

---

## How to run (commands reference)
- Venv python: `S9SharedCode\code\.venv\Scripts\python.exe`
- Compile all: `py_compile` loop over `Get-ChildItem -Recurse -Filter *.py`
  excluding `.venv`/`__pycache__`.
- Unit tests: `.venv\Scripts\python.exe -m pytest tests/ -q` (live/stress
  auto-skipped by conftest).
- Gateway probe: `Invoke-WebRequest http://localhost:8109/v1/routers`.

## Definition of "done"
Every checkbox above is either checked (✅) or has a recorded finding
(❌ with detail). The final report lists concrete, reproducible issues with
file:line references so they can be fixed.

---

## LIVE TEST RESULTS (phases 13+)

### Test 1 — Simple fact (planner short-circuit)
- **Query:** "What is the capital of France?"
- **Result:** ✅ Correct answer "The capital of France is Paris."
- **Latency:** 86s (planner 70.7s) — **SLOW** due to gemini rate limit
- **Frames:** log → meta → done ✅

### Test 2 — Weather (tool-use DAG)
- **Query:** "What is the current weather like in London right now?"
- **Result:** ❌ Planner chose `browser` (503) instead of `action/get_weather`
- **Recovery:** browser failed → retry → skip (correct recovery behavior)
- **Bug:** Planner ignores available tools, routes to wrong skill

### Test 3 — Explicit get_weather (Kilo)
- **Query:** "Use the get_weather tool to tell me the current temperature in Tokyo."
- **Result:** ✅ Correct — planner → action → formatter (9.6s → 236s → 26.9s)
- **Data:** 29.6°C, 65% humidity, 3.3 m/s (real tool data)
- **Cost:** $0.001203

### Test 4 — Memory continuity (follow-up)
- **Query:** "What was the temperature in Tokyo from my previous question?"
- **Result:** ❌ Formatter couldn't recall Tokyo temperature
- **Bug:** Memory continuity fails — action results not stored as retrievable facts

### Test 5 — Memory search
- **Query:** "Search your memory for anything about Tokyo temperature..."
- **Result:** ❌ Retriever found 8 hits but none contained temperature
- **Bug:** Confirms memory persistence issue

### Test 7 — Action/get_weather (Kilo, retry)
- **Query:** "Use the get_weather tool to tell me the current temperature in Tokyo."
- **Result:** ⚠️ Action node returned empty; formatter reported "no temperature data"
- **Note:** Gateway 502 on memory classifier (the BUG-502 we fixed)

### Test 8 — Memory recall (Kilo)
- **Query:** "What was the temperature in Tokyo from my previous question?"
- **Result:** ❌ Planner returned `{}` (empty JSON) as final answer
- **Bug:** Planner fails to produce valid plan for memory-recall queries

### Test 10 — Trivial math (Kilo)
- **Query:** "What is 2 plus 2?"
- **Result:** ✅ Correct "4" in 23.2s (planner 10.8s)

### Test 11 — Simple fact (Kilo)
- **Query:** "What is the capital of France?"
- **Result:** ✅ Correct in 20.9s (planner 8.6s)

### Test 12 — Multi-step research DAG (Kilo, post-fix validation)
- **Query:** "Find the current population of Tokyo and summarize it in one sentence."
- **Iterations:** t12 → t12i across 5 fix/restart cycles
- **Final result (t12i):** ✅ Correct answer in **86s**: "As of October 1, 2025, the population of Tokyo is estimated to be 14.273 million according to the Tokyo Metropolitan Government's FY2025 statistics."
- **DAG shape:** planner → researcher → summariser → critic → formatter; auto-recovery replan fired correctly on critic parse failure
- **Memory continuity retest (same conversation_id):** ✅ Follow-up "What population figure did you just tell me?" recalled 14.273M/FY2025 with sources

#### Fixes applied during Test 12
1. **BUG-PARSE-1a** (`skills.py`): `parse_skill_json` returning `{}` marked nodes `success=True` — silent data starvation. Now fails the node and dumps raw reply.
2. **BUG-PARSE-1b** (`agent_config.yaml`): planner `max_tokens` 1500→3000 — hy3 truncated multi-node plans mid-JSON at the cap.
3. **BUG-PARSE-1c** (`agent_config.yaml`): critic 500→1500, researcher 2500→4000 — hy3 CoT preamble ate budgets before JSON appeared.
4. **BUG-502-2** (`browser/skill.py`): `a11y_provider_pin` was hard-pinned to `"gemini"` (dead), bypassing failover → every browser call 502'd. Now `None` → falls back to `S9_LLM_PROVIDER`.
5. **BUG-RETRY-BACKOFF** (`flow.py`): transient retry backoff was 2/4/8s (14s total) — never outlived a ~60s free-tier cooldown. Now 10/20/30s.

---

## Additional defects discovered and fixed during live Test 12

| Bug | Description | Severity | Status |
| --- | ----------- | -------- | ------- |
| BUG-DASHBOARD-V7 | llm_gatewayV9/static/dashboard.html | MINOR | Dashboard title says "V7"; help.html references port 8099 and github worker. | HTML title "LLM Gateway V7 · Dashboard" | Update to V9. |
| BUG-PARSE-1a | S9SharedCode/code/skills.py | BLOCKER | Unparseable LLM reply became `{}` with `success=True` — downstream nodes silently starved, retry/recovery bypassed. | Live t12: summariser output `{}` despite full INPUTS; formatter said "no upstream data" | Fail node + dump raw reply. **FIXED.** |
| BUG-PARSE-1b | S9SharedCode/code/agent_config.yaml | MAJOR | Planner max_tokens=1500 truncated multi-node plans mid-JSON (hy3 verbose). | Live t12b: plan cut at `"inputs": ["` exactly 1500 tokens out | Raised to 3000. **FIXED.** |
| BUG-PARSE-1c | S9SharedCode/code/agent_config.yaml | MAJOR | Critic (500) / researcher (2500) budgets eaten by hy3 CoT preamble before JSON. | Live t12d/t12e raw dumps: pure reasoning prose, no JSON | critic→1500, researcher→4000. **FIXED.** |
| BUG-502-2 | S9SharedCode/code/browser/skill.py | BLOCKER | Browser a11y calls hard-pinned to dead gemini provider, bypassing gateway failover → instant 502/503 on every browser node. | Gateway log: 502s only during browser nodes; agent_routing.yaml intentionally empty | pin=None → S9_LLM_PROVIDER fallback. **FIXED.** |
| BUG-RETRY-BACKOFF | S9SharedCode/code/flow.py | MAJOR | Transient retry backoff 2/4/8s never outlived ~60s free-tier cooldown; all 3 retries died in same window. | Live t12c/t12g: retries at 0.0s intervals, all 503 | Ladder now 10/20/30s. **FIXED.** |
| BUG-KILO-COT | provider choice | MINOR | `tencent/hy3:free` emits long CoT prose before JSON; wastes budget, causes parse failures. | Multiple raw-reply dumps showing reasoning-only output | Switched to `stepfun/step-3.7-flash:free` — latency dropped ~200s→86s per run. **MITIGATED.** |
| BUG-MEM-CLASSIFIER | S9SharedCode/code/memory.py | MINOR | memory.remember classifier 503 falls back to fact-write (graceful, but recall quality degrades). | Live t12i log: "classifier failed... falling back to fact-write" | Acceptable degradation; classifier call could reuse pinned provider. |

---

## PRODUCTION-READINESS PASS (2026-08-23)

### Backend fixes
| ID | File | Fix | Status |
|----|------|-----|--------|
| BUG-ENV-1 | gateway.py, browser/client.py, browser/skill.py | Gateway URL overridable via `LLM_GATEWAY_V9_URL` (gateway.py honors `GATEWAY_V9_PORT` for its own bind). NOTE: the `S9_GATEWAY_PORT` → `GATEWAY_V9_PORT` → `GATEWAY_V3_PORT` chain this row originally claimed was never implemented — gateway.py hardcodes `http://localhost:8109` with only the `LLM_GATEWAY_V9_URL` override. | ✅ FIXED (as URL override; port chain never existed) |
| BUG-PLAN-JSON | flow.py | Executor detects plan-shaped JSON leaking as final answer when pipeline dies early; replaces with honest failure notice listing failed steps. | ✅ FIXED |
| BUG-PARSE-RETRY | skills.py | One corrective JSON re-ask before failing a node — rescues "answered well but in prose" replies cheaply instead of a full recovery replan. | ✅ ADDED |
| BUG-MEM-CLASSIFIER | memory.py | Classifier now temp=0.2 (was 1.0, caused empty value dicts) + one 8s-delayed retry to ride out short cooldowns. | ✅ FIXED |
| BUG-SCHED-GHOSTS | scheduler.py | `list_schedules()` hides cancelled (enabled=False) rows — they used to linger forever and made the panel look broken. | ✅ FIXED |
| BUG-TTS-500 | agent_server.py | Missing Kokoro model files now return 503 (config issue) not 500 (server fault); client can degrade gracefully. | ✅ FIXED |
| BUG-DB-VERSION | llm_gatewayV9/db.py | DB path v8→v9. | ✅ FIXED |
| BUG-DASHBOARD-V7 | llm_gatewayV9/static/*.html | Title/headline updated to V9; help.html port refs 8099→8109. | ✅ FIXED |

### New backend endpoints
| Endpoint | Purpose |
|----------|---------|
| GET `/api/memory?q=&limit=` | Memory browser for the UI panel (list latest or semantic search) |
| GET `/api/tts/voices` | Voice list for the TTS picker (reads Kokoro voices binary, static fallback) |

### Frontend revamp (web/)
**Root cause of dead panels found:** `app.js` never wired `costToggle` / `schedToggle` / `memToggle` / side panel at all — the buttons existed in HTML with zero handlers. Also unwired: `sttBtn`, `ttsVoice`, `ttsSpeed`, `audioFile`.

**Now fully wired & verified in-browser:**
- 💰 **Spend** — sliding panel, per-thread totals + per-skill token/cost table
- 🕘 **Schedule** — add ("in 10m", "daily@09:00"…), list with next-fire times, cancel (✕)
- 🧠 **Memory** — latest 60 items with kind badges + debounced live search (verified: "Tokyo" → relevant hits)
- 🎤 **Audio→text** — file transcription button wired to `/api/stt`
- 🔊 **TTS voice picker + speed slider** — populated from `/api/tts/voices`, persisted to localStorage
- Escape key closes panels; panels toggle (click again to close)

**Visual redesign (style.css):**
- Refined palette (calmer surfaces, accent glow), consistent design tokens
- Focus rings on composer, hover states on all controls, message rise-in animation
- Smart autoscroll (only follows log while user is near bottom)
- Custom scrollbars, reduced-motion support, better mobile breakpoints

### Live verification (browser)
- Chat round-trip: query → streaming reasoning → answer → meta footer (⏱/💲) ✅
- Spend panel populates after a run (planner row visible) ✅
- Schedule add + cancel round-trip ✅
- Memory browse + search ✅
- Static suite: 16/16 pass after all changes ✅

---

## E2E Playwright Edge-Case Suite (VS Code browser, live agent :8500 + gateway :8109)

Driver: injected `window.__runQuery(q)` helper that fills `#input`, submits the
real composer, and polls until the `done` frame sets `.meta` visible (or an
error/stopped frame appears). Model: `kilo` / `stepfun/step-3.7-flash:free`.

| # | Edge case | Result | Notes |
|---|-----------|--------|-------|
| 1 | Empty / whitespace input | ✅ PASS | Rejected, no message sent (busy-guard + trim) |
| 2 | Very long query (stress) | ⚠️ PASS w/ bug | Completed but **final answer leaked as raw plan JSON** (`{"skill":"formatter",...}`) — BUG-PLAN-JSON recurred through the recovery path. Also 3 node failures (formatter/distiller JSON unparsable) self-healed via recovery. |
| 3 | Non-English + unicode (FR + 春はあけぼの) | ✅ PASS | Correct French answer + Japanese haiku rendered |
| 4 | Code generation (Fibonacci + memo) | ✅ PASS | Proper fenced ```python block returned |
| 5 | Web research (NASA Artemis) | ⚠️ PASS w/ bug | Completed w/ sources, but **`fetch_url` fails encoding `→` arrow char** in page titles — BUG-FETCH-ENC. Recovered via search snippets. |
| 6 | Ambiguous ("it") | ✅ PASS | Gracefully asked for clarification |
| 7 | Math (17*23+45/5, √144) | ✅ PASS | Correct: 400 and 12 |
| 8 | Memory seed + recall | ✅ PASS | Seeded "Rust / PROJECT-QUARTZ-7741", recalled verbatim next turn |
| 9 | Stop button mid-stream | ✅ PASS | Aborted stream ("⏹ Stopped."), send re-enabled, stop hidden |
| 10 | New chat reset | ✅ PASS | 19→1 messages, new thread id, welcome shown |
| 11 | Spend / Schedule / Memory panels | ⚠️ PASS w/ bug | Panels open & populate, but **Schedule POST 500 on "in 10 minutes"** — BUG-SCHED-PARSE (fixed). After fix: 3 rows, 60 memories. |
| 12 | Injection + special chars (`<script>`, quotes, backslashes) | ✅ PASS | Refused system-prompt leak; rendered answer XSS-safe (no injected `<script>`) |

### New defects found & fixed during E2E
| ID | File | Severity | Symptom | Fix |
|----|------|----------|---------|-----|
| BUG-SCHED-PARSE | S9SharedCode/code/scheduler.py | MAJOR (500) | `schedule()` did `int(unit[:-1])` on `"10 minute"` → ValueError 500 on any human-phrased relative time. UI sends "in 10 minutes". | Regex-parse `(\d+)\s*([a-zA-Z]*)`; map s/m/h/d + plurals/abbrevs; raise clear ValueError otherwise. **FIXED** (verified 200 + id). |
| BUG-FETCH-ENC | S9SharedCode/code/mcp_server.py + mcp_runner.py | MAJOR | `fetch_url` crashed with `UnicodeEncodeError` / `WinError 6` when crawl4ai's Rich logger emitted non-ASCII (`→`) in page titles on Windows `charmap` console. | MCP subprocess now launched with `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` so the console codec is UTF-8. Verified: 304KB fetch of Wikipedia Artemis II page with `→` present, no crash. **FIXED.** |
| BUG-PLAN-JSON-RECOV | S9SharedCode/code/flow.py | MAJOR | Plan-shaped JSON leaked as final answer when pipeline died inside a *recovery* replan (Edge 2). Old guard only caught `"nodes"`/`"rationale"` keys. | Guard broadened: any JSON object/array (`stripped[:1] in ("{","[")`) or dict with internal-structure keys (`"skill"`, `"inputs"`, `"metadata"`, `"final_answer"`) is now replaced with honest failure notice. Verified Edge 2 re-run: `leakedJSON: false`, returns "(no answer)" failure notice. **FIXED.** |
