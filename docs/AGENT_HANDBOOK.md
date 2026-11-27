# Aria — Exhaustive Developer Handbook

> **Purpose.** This is the complete, no-compromise reference for Aria: what it is,
> why it is built the way it is, and how every part works. It is written so a
> developer who has never seen the repo can understand the whole system, then
> jump to any subsystem and go deep. It is long by design — it prefers
> completeness to brevity.
>
> **Companion docs.** `README.md` (narrative overview), `docs/TECHNICAL_GUIDE.md`
> (mid-depth reference), `docs/ARCHITECTURE.md` (design rationale), and the
> per-folder `README.md` files. This handbook is the superset.

---

## Table of contents

- [Part 0 — How to read this](#part-0--how-to-read-this)
- [Part 1 — What Aria is](#part-1--what-aria-is)
- [Part 2 — Architecture](#part-2--architecture)
- [Part 3 — The Gateway (V9)](#part-3--the-gateway-v9)
- [Part 4 — The Agent Core (S9)](#part-4--the-agent-core-s9)
- [Part 5 — The Executor and the DAG](#part-5--the-executor-and-the-dag)
- [Part 6 — Skills catalogue](#part-6--skills-catalogue)
- [Part 7 — Tool layer (MCP surface)](#part-7--tool-layer-mcp-surface)
- [Part 8 — Channels and integrations](#part-8--channels-and-integrations)
- [Part 9 — The Sandbox](#part-9--the-sandbox)
- [Part 10 — Computer use](#part-10--computer-use)
- [Part 11 — The Browser skill](#part-11--the-browser-skill)
- [Part 12 — Studio (coding workspace)](#part-12--studio-coding-workspace)
- [Part 13 — Memory and retrieval](#part-13--memory-and-retrieval)
- [Part 14 — Document generation](#part-14--document-generation)
- [Part 15 — State on disk](#part-15--state-on-disk)
- [Part 16 — Reliability and failure model](#part-16--reliability-and-failure-model)
- [Part 17 — Security and privacy](#part-17--security-and-privacy)
- [Part 18 — Cost model](#part-18--cost-model)
- [Part 19 — Verification](#part-19--verification)
- [Part 20 — Known issues and hard-won fixes](#part-20--known-issues-and-hard-won-fixes)
- [Part 21 — Glossary](#part-21--glossary)
- [Appendix A — Agent API route index](#appendix-a--agent-api-route-index)
- [Appendix B — Gateway endpoint index](#appendix-b--gateway-endpoint-index)
- [Appendix C — Data schemas](#appendix-c--data-schemas)
- [Appendix D — Environment variables](#appendix-d--environment-variables)

---

## Part 0 — How to read this

1. **New to the project?** Read Part 1 and Part 2, then skim Part 6 (skills) and
   Part 7 (tools) to see what the agent can actually do.
2. **Working on the agent?** Parts 4, 5, 6, 7, 16 are the core.
3. **Working on the gateway?** Part 3 plus Part 8.
4. **Working on a channel?** Part 8 and the adaptor/integration READMEs.
5. **Debugging a failure?** Part 16 (reliability) and Part 20 (known issues).
6. **Need a specific endpoint or tool?** Appendices A and B.

Conventions: `:8500` is the agent, `:8109` is the gateway. "Node" = one skill
execution in the DAG. "Skill" = a dispatch role. "Tool" = an MCP function the
model may call. `art:` = a content-addressed artifact handle; `doc-` = a stored
document id.

---

## Part 1 — What Aria is

### 1.1 The problem

Most "AI agents" are demos. They answer a question or take one action, but a task
that needs several steps — research something, write it, send it somewhere —
breaks. They lose the thread between steps, or report success for something that
never happened, or need the same context re-pasted. And they want your API keys
and your data in someone else's cloud.

Aria is a self-hosted agent built to fix that: it **finishes multi-step work**,
**tells the truth about what it did**, and **runs on your machine with your
keys**.

### 1.2 The core idea

A request is rarely one model call. Aria decomposes a request into a **DAG of
single-purpose skill nodes** (planner, researchers, retriever, author, coder,
action, formatter, …) that run in parallel where they can and feed each other,
until a formatter writes the answer. The graph is persisted, so a run can be
resumed or woken later. This is what lets a broad question get broad coverage,
and what lets a task survive a bad step.

### 1.3 Design principles

- **Honesty.** A tool that fails is reported failed; a missing credential is
  named; a step that could not complete leaves the run marked incomplete. The
  file list on a document comes from the renderer that produced the bytes, never
  from the model's claim.
- **Resilience.** A failed node triggers a re-plan rather than sinking the run.
- **Fail-closed on trust.** An unpaired sender gets no agent turn; an unreadable
  workspace is empty, not inherited.
- **Cost-awareness.** Free/cheap backends first; automatic failover; spend capped
  per call, per agent, per session.
- **Lean prompts, model decides.** Prompts carry methodology and capability
  tables, not per-case instructions; generic causes are fixed, not
  task-special-cased.
- **Documented == reachable.** A tool named in a prompt but not granted is a bug
  (enforced by tests).

### 1.4 What it can do (summary)

Conversation with cross-session memory; document ingestion + hybrid retrieval;
multi-source research; real document/deck/spreadsheet authoring; a full coding
workspace (Studio); ~20 external channels (email, calendar, GitHub, Notion, chat,
social, push); two-way chat on Telegram/Gmail/Slack; scheduling and
self-continuation; gated computer use; a 124M model showcase (Model Lab); 3D part
analysis/repair.

---

## Part 2 — Architecture

### 2.1 The three processes

| Process | Port | Role |
|---|---|---|
| **Agent server** (`S9SharedCode/code/agent_server.py`) | `8500` | Orchestration, the DAG executor, the web API + SSE, serves the console. |
| **Gateway** (`llm_gatewayV9/main.py`) | `8109` | Model providers, channel credentials, document store, memory plane, document generation, policy, cost. |
| **Console** (`S9SharedCode/code/console-frontend/`) | served by `:8500` | React SPA; optional Electron shell (`agent-desktop/`). |

### 2.2 Trust model

The agent owns orchestration and the UI; the gateway owns secrets and external
services. The agent process **never holds a channel token** — it calls the
gateway at `/v1/integrations/{service}/{op}`, where credentials live and policy is
applied. A channel is "configured" only when the gateway reports it; the agent
claims nothing it does not have. Credentials never leave the gateway process, and
the agent's own logs never contain a token.

### 2.3 Request lifecycle (end to end)

```
Console ──HTTP/SSE──▶ Agent :8500 ──/v1/*──▶ Gateway :8109 ──▶ Providers / Channels
  (React)            (flow.py DAG)              (keys, memory, docgen, policy)
```

1. Console POSTs `/api/chat` with `{query, conversation_id}` and the per-launch
   token.
2. `agent_server.py` calls `Executor().run(query)` on a worker thread and streams
   SSE frames (`log`, `done`, `error`).
3. The **planner** skill emits a DAG (JSON). The executor schedules ready nodes
   in bounded waves.
4. Each node runs one skill via `skills.py`. Tool-using skills run an MCP tool
   loop; channel tools are thin forwarders to the gateway.
5. On failure, `recovery.py` classifies and the executor splices a recovery
   subgraph.
6. The **formatter** produces the final answer; the run is persisted.

### 2.4 Repository layout

```
project3/
├── S9SharedCode/code/            # Agent core (see Part 4)
│   ├── agent_server.py, flow.py, skills.py, recovery.py, sandbox.py
│   ├── mcp_server.py, mcp_runner.py, gateway.py, action.py, channel_chat.py
│   ├── computer_use/, browser/, prompts/, console-frontend/, tests/
│   └── agent_config.yaml, flags.py, schemas.py, studio_*.py, apps/parts/memory…
├── llm_gatewayV9/                # Gateway (see Part 3)
│   ├── main.py, providers.py, router.py, cache.py, pricing.py, budget.py
│   ├── memory_api.py, docgen*.py, channels_api.py, integrations/, adaptors/
│   └── policy/, key_info.py, *_poller.py, secret_vault.py, gateway_v8.db
├── agent-desktop/                # Electron shell
├── docs/                         # This handbook + design docs
├── start_aria.ps1 / stop_aria.ps1
└── AGENTS.md
```

---

## Part 3 — The Gateway (V9)

A standalone FastAPI service on **`:8109`**. Single place for keys, providers, and
external state. The agent always goes through it.

### 3.1 Module reference

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app, `/v1/chat` (routing, empty-completion retry, failover ladder), `/v1/vision`, `/v1/embed`, `/v1/docgen`, `/v1/documents`, `/v1/memory/*`, `/v1/integrations/*`, `/v1/channels/*`, `/v1/status`, `/v1/routers`, `/v1/providers`, `/v1/calls`, `/v1/spend`, `/v1/policy`, `/v1/cost/by_agent`, `/v1/control/*`, `/health`. |
| `providers.py` | Provider adapters (Gemini, Kilo, OpenAI-compatible, …). `ProviderError(status, retryable)`. |
| `router.py` | Model routing + capability evaluation + per-provider rate state. |
| `agent_routing.yaml` | Per-agent provider preferences/pins (soft; 502 failover). |
| `cache.py` | Response cache (keyed by request shape). |
| `pricing.py` | $/token tables. |
| `budget.py` | Per-call / per-agent / per-session spend caps. |
| `embedders.py` | Embedding backends (fixed dim; do not change after indexing). |
| `memory_api.py` | Memory plane: drawers, search, record_outcome, supersede. |
| `docgen.py` | Document rendering (PDF/PPTX/DOCX/XLSX). |
| `docgen_latex.py`, `docgen_typst.py`, `docgen_markup.py` | Alternative renderers. |
| `doclayout.py`, `docpreviewimg.py` | Layout + preview images. |
| `channels_api.py` | `/v1/integrations/{service}/{op}`, `/v1/channels/{name}/send`, inbound recording. |
| `integrations_api.py` | Integration helpers. |
| `key_info.py` | Human docs for each key (names/purpose, never values). |
| `telegram_poller.py`, `gmail_poller.py`, `slack_poller.py` | Inbound pollers → `_record_inbound`. |
| `secret_vault.py` | Credential storage. |
| `gateway_auth.py` | Gateway auth. |
| `user_store.py`, `users_api.py`, `user_pools.py` | Users + pools. |
| `oauth_flows.py` | Shared OAuth. |
| `gmail_oauth_setup.py`, `calendar_oauth_setup.py`, `zoho_mail_oauth_setup.py` | One-time consent flows. |
| `refresh_tokens.py`, `gmail_refresh.py` | Token refresh. |
| `db.py` | SQLite layer (WAL, thread-local pooled connections). |
| `schemas.py` | Pydantic request/response models. |
| `client.py` | Python SDK (`LLM().chat/vision/embed`). |
| `approvals.py` | Approval store. |
| `policy/` | Policy engine + rules (see Part 8.6). |
| `safe_fetch.py` | SSRF-safe fetch. |
| `scrub.py`, `redact.py` | Secret scrubbing/redaction. |
| `security_audit.py` | Security audit route. |
| `voice_api.py`, `voice_chunking.py` | Voice endpoints. |
| `channel_probe.py`, `check_keys.py`, `deploy_api.py` | Health/probe/setup. |
| `kilo_models.py` | Kilo model catalogue. |
| `atomic_json.py` | Atomic JSON writes. |
| `run.sh`, `pyproject.toml`, `requirements.txt` | Run + deps. |
| `adaptors/`, `integrations/`, `policy/`, `docs/`, `tests/` | See their READMEs. |

### 3.2 `/v1/chat` lifecycle (detail)

1. Authenticate; parse `ChatRequest` (messages, model, tools, tool_choice,
   reasoning, response_format, images, cache_system, agent, session).
2. `router` selects a provider/model (honouring `agent_routing.yaml` soft pins).
3. Call `provider.chat(...)`. On a retryable error (5xx, 429, or an **empty
   completion**) retry/failover (see Part 16.1).
4. Record the call in `gateway_v8.db` (`calls` table): tokens, cost, status,
   error, attempted providers.
5. Return the completion (text + tool_calls) to the agent.

### 3.3 Empty-completion handling (the core reliability fix)

`main.py` treats a 200 response with **zero text and zero tool calls** as a
failed call:

```python
while not str(result.get("text") or "").strip() \
        and not (result.get("tool_calls") or []):
    if empty_retries >= 2:                      # was 1
        raise ProviderError(f"{name} returned an empty completion "
                            f"(0 text, 0 tool calls)", retryable=True)
    # else: retry the SAME provider
```

So an empty gets **two retries on the same provider**, then failover to the next.
Only when the whole free pool is momentarily empty does an empty reach the agent.

### 3.4 Data model (`gateway_v8.db`)

`calls` table (Thread-local pooled connections, WAL, `synchronous=NORMAL`,
`busy_timeout=10000` so a ledger insert never blocks `/health`):

| Column | Meaning |
|---|---|
| `id`, `ts` | Row id, timestamp. |
| `provider`, `model` | What served the call. |
| `input_tokens`, `output_tokens` | Token usage. |
| `cache_create_tokens`, `cache_read_tokens` | Prompt-cache usage. |
| `latency_ms` | End-to-end latency. |
| `status`, `error` | `ok` / error text. |
| `prompt_chars`, `response_chars` | Size. |
| `override`, `attempted` | Provider override + failover path. |
| `tool_calls` | Number of tool calls. |

Other tables (memory, documents, cost) live in the same DB; see `memory_api.py`
and `docgen*.py`.

### 3.5 Providers and routing

- **Providers** are adapters in `providers.py`; most are OpenAI-compatible.
- **Routing** (`router.py`) evaluates the requested model's capabilities and
  fails over across providers on error/rate-limit. Per-agent pins live in
  `agent_routing.yaml` and are **soft** (a 502 fails over).
- **Rate state** is tracked per provider (rpm/rpd/cooldown); `/v1/status` and
  `/v1/routers` expose it.
- **Free-tier reality:** Gemini ~15 rpm; heavy bursts strain the free pool, which
  is why the empty-completion retry and the agent-side repair exist.

### 3.6 Caching, pricing, budget

- `cache.py` caches by request shape; `cache_system` enables prompt caching
  (paid-tier only on Gemini; free keys ignore it harmlessly).
- `pricing.py` holds $/token; `budget.py` enforces per-call/agent/session caps
  (defaults in `policy.yaml`: per_call $0.50, per_agent_per_day $5.00,
  per_session_per_day $10.00).
- Spend is recorded per call and aggregated in `/v1/spend` and
  `/v1/cost/by_agent`.

### 3.7 Memory plane

`memory_api.py` exposes `/v1/memory/search`, `/v1/memory/record_outcome`,
`/v1/memory` (CRUD), drawers, and supersede. Memory items are `fact`,
`preference`, `tool_outcome`, `scratchpad`, `observation` (see Appendix C). Facts
and preferences carry an embedding for vector search; scratchpad is run-scoped
and skips embedding.

### 3.8 Document store & generation

`docgen*.py` render real bytes. The agent's author/deck skills POST a spec to
`/v1/docgen`; bytes are stored content-addressed; `/v1/documents` manages the
store; `/v1/documents/{id}/figures` serves embedded figures. See Part 14.

### 3.9 Channels

`channels_api.py` routes `/v1/integrations/{service}/{op}` and
`/v1/channels/{name}/send`. Credentials live in `integrations/` + `adaptors/`.
See Part 8.

### 3.10 Auth

`gateway_auth.py` + `secret_vault.py` + `user_store.py`. The agent authenticates
to the gateway with a user token; the console uses a per-launch token. Secrets are
stored in the vault / `.env`, never exposed to the agent.

---

## Part 4 — The Agent Core (S9)

### 4.1 `agent_server.py`

FastAPI on `:8500`. Serves the built console, the per-launch token, and the
JSON/SSE API. `/api/chat` streams a DAG run as SSE (`log`/`done`/`error`).
`/api/health` reports `gateway_up` (must be true). The full route inventory is in
Appendix A; the major groups:

- **Chat / runs** — `/api/chat`, `/api/chat/threads/*`, `/api/runs/summary`,
  `/api/sessions`, `/api/sessions/{sid}/graph`, `/api/sessions/{sid}/nodes/{nid}`,
  `/api/events`, `/api/feedback*`.
- **Documents** — `/api/documents*`, `/api/documents/search`, `/api/artifact/*`,
  `/api/doc/preview`, `/api/docgen*`.
- **Memory / profile** — `/api/memory*`, `/api/profile*`.
- **Apps / A2UI** — `/api/apps*`, `/api/a2ui/*`.
- **Code / Studio** — `/api/code/*`, `/api/studio/*` (see Part 12).
- **Scheduler / notify** — `/api/schedule*`, `/api/notify*`, `/api/ack/*`,
  `/api/channel/*`, `/api/whats-new`.
- **Parts** — `/api/parts*`.
- **Computer** — `/api/computer/*` (approvals, capabilities).
- **Voice** — `/api/tts`, `/api/stt`.
- **Config / capabilities / cost** — `/api/config*`, `/api/capabilities`,
  `/api/cost*`, `/api/nodes/health`, `/api/audit`, `/api/tools`.

### 4.2 `flow.py` — the executor

`Executor().run(query)` is the async orchestrator. See Part 5 for the full
scheduling/recovery model.

### 4.3 `skills.py` — registry + dispatch

Holds the skill registry, `_TOOL_CATALOG` (JSON schemas the model sees),
`tool_payload()`, `effective_tools()` (progressive disclosure), `CHANNEL_TOOLS`,
`render_prompt()`, `length_contract()`, the sectioned paths
(`_sectioned_document_blocks`, `_sectioned_final_answer`, `_sectioned_findings`),
and the JSON repair (`_repair_json_reply`, `_JSON_CONTRACT_SKILLS`). See Parts 6,
7, 16.

### 4.4 `recovery.py`

- `classify_failure(error_text, error_code) -> RecoveryReason` — transient vs
  environmental/deterministic.
- `RecoveryDecision` — the decision dataclass.
- `plan_recovery(...)` — ask the planner for a replacement subgraph.
- `handle_critic_verdict(...)` — apply a critic pass/fail.

### 4.5 `sandbox.py`

The resource/process boundary. See Part 9.

### 4.6 `mcp_server.py` / `mcp_runner.py`

`mcp_server.py` is the MCP tool surface (stdio): every tool the model can call
lives here as a Python function (see Part 7 for the full inventory).
`mcp_runner.py` spawns one MCP session per skill and runs the tool loop
(model → tool_call → dispatch → result → model …).

### 4.7 `gateway.py`

Thin V9 client: `LLM().chat(...)`, `embed(...)`, `ensure_gateway()` (auto-starts
the gateway if not listening).

### 4.8 `action.py`, `channel_chat.py`

`action.py` — action-skill helpers (integration confirmations).
`channel_chat.py` — inbound channel message → a normal DAG chat turn
(`run_kind="chat"`).

### 4.9 `agent_config.yaml`

The skill catalogue. Each entry: `prompt`, `description`, `tools_allowed`,
`temperature`, `max_tokens`, optional `provider_pin`, `internal_successors`,
`critic`, `dynamic_channels`, `sectioned_findings`, `sectioned`. See Part 6.

### 4.10 `flags.py`

Runtime kill-switches: `ui.code_ide` (Studio on/off), `browser.tools`,
`browser.headed`. Kept out of `agent_config.yaml` so the operator kill-switch
works even with the config service unreachable.

### 4.11 `schemas.py`

Typed contracts every layer talks in (Pydantic). `MemoryItem`, `Artifact`,
`NodeSpec`, `AgentResult`, etc. See Appendix C.

---

## Part 5 — The Executor and the DAG

### 5.1 Scheduling

- The planner emits an initial graph + recovery subgraphs.
- The executor runs **ready** nodes in bounded **waves**; a node runs when its
  predecessors are complete.
- **Mechanics limits (not plan-quality limits):**
  - `MAX_NODES = 400` — a runaway planner/recovery loop cannot schedule forever.
  - `MAX_CONCURRENCY = 8` — calls per wave. Unbounded waves `429` together on the
    free tier; deferred nodes stay `pending` and run next wave, so a 60-facet
    plan still runs all 60.
- **No fanout cap on quality.** `MAX_FANOUT=4` once threw away a 6-facet plan
  (4 facets → 17,782 chars / 2,367-word doc; 3 → 13,421 / 310-word stub). The
  facet count is the research budget and is the planner's call.

### 5.2 Soft time budget

`NODE_BUDGET_S` tells the **next** invocation of a skill how much allowance it has
spent, so it synthesises instead of starting a fourth fetch. It never cancels an
in-flight call.

| Skill | Budget |
|---|---|
| researcher | 150s |
| browser | 120s |
| action | 60s |
| planner | 30s |
| formatter | 240s |
| default | 90s |

`BUDGET_WARN_RATIO = 0.7`.

### 5.3 Cooperative Stop

`_stop_in_nodes()` publishes a stop predicate into nodes so a long author (160
section calls) or researcher (30 tool hops) notices Stop **between its own tool
calls**, not only at the batch boundary. This stops billing after "stopped" and
releases the idempotency claim so a resubmit is not rejected.

### 5.4 Recovery and splicing

- `classify_failure` tags a failure **transient** (retry) or
  **environmental/deterministic** (skip/replan). A sandbox wall-clock timeout is
  deterministic → skip, not loop.
- `plan_recovery` asks the planner for a replacement subgraph scoped to the
  missing piece (`_annotate_failure_report`).
- `_splice_recovery` rewires recovery nodes **in place of** the failed node(s),
  marks the original `skipped`, rewires waiting consumers.
- `_replacement_answered` confirms the recovery filled the hole.
- `_consumers_have_other_evidence` prevents losing a chain's only evidence when a
  minority of parallel researchers fail.

### 5.5 Graph-shape guards

- `ensure_sandbox_has_code` — drops a `sandbox_executor` no `coder` feeds.
- `ensure_authoring_research` — guarantees retriever + researcher run before an
  author node.

### 5.6 Idempotency

A per-session idempotency claim prevents duplicate concurrent runs of the same
question; it is released on Stop so a resubmit is accepted.

---

## Part 6 — Skills catalogue

A skill = one YAML entry + one prompt. Full config in `agent_config.yaml`.

### 6.1 Cognitive / control
| Skill | Tools | Temp | max_tokens | Role |
|---|---|---|---|---|
| `planner` | — | 0.4 | 65536 | Decompose into the DAG + recovery subgraphs. |
| `critic` | read_artifact | 0.0 | 1500 | Pass/fail an upstream node with rationale. |
| `distiller` | read_artifact | 0.1 | 65536 | Structured extraction (auto-inserts a Critic). |
| `summariser` | read_artifact | 0.3 | 65536 | Condense. |
| `formatter` | web_search, fetch_url, search_knowledge, read_document | — | 65536 | Final answer; `sectioned: true`. |

### 6.2 Research
| `retriever` | search_knowledge, read_document, recall_preferences, remember_preference | 0.2 | 65536 | Indexed knowledge + docs. |
| `researcher` | web_search, fetch_url, arxiv_search, wikipedia_search, openalex_search, news_search, fetch_pdf, extract_tables, wayback_fetch, remember_preference, recall_preferences, read_artifact, computer_action, search_images, fetch_image | 0.7 | 65536 | Live multi-source research; `sectioned_findings: true`. |

### 6.3 Authoring
| `author` | render_document, read_artifact, web_search, search_images, fetch_image | 0.35 | 65536 | Render real documents. |
| `deck` | render_document, read_artifact, web_search, search_images, fetch_image | 0.4 | 65536 | Render slide decks. |

### 6.4 Coding & interaction
| `coder` | read_file, list_dir, search_files, find_symbol, document_symbols, find_references, blast_radius, preview_edit, apply_symbol_edit, create_file, update_file, edit_file, delete_file, run_checks, git_inspect, index_document | 0.2 | 65536 | Writes code; `internal_successors: [sandbox_executor]`. |
| `studio_coder` | studio_coder | 0.2 | 8192 | Delegate to the full Studio coding agent. |
| `sandbox_executor` | — | 0.0 | 4000 | Runs the coder's code in the sandbox. |
| `browser` | (own cascade) | 0.0 | 1024 | 4-layer web cascade. |
| `computer` | computer_action | 0.1 | 1200 | Gated desktop control. |
| `vision_file` | — | 0.2 | 1200 | Local image understanding. |

### 6.5 Action
| `action` | (large catalogue; `dynamic_channels: true`) | 0.2 | 1500 | Real-world side effects; progressive disclosure. |

### 6.6 Specialist builders
| `part_analyst` | parts_list, part_get, part_analyse, part_set_view | 0.1 | 6000 | Inspect a 3D part (read-only). |
| `part_engineer` | parts_list, part_get, part_analyse, part_repair, part_optimise, part_start_loop, part_job_status, part_cancel_job, part_export, part_set_view | 0.2 | 8000 | Repair/optimise a mesh. |
| `part_blender` | blender_status, blender_catalog, blender_part_op, blender_part_render, blender_rna_get, blender_rna_set, blender_python | 0.2 | 9000 | Real Blender ops. |
| `app_worker` | app_list, app_get, app_build, app_action | 0.2 | 8000 | Build/change A2UI apps. |
| `app_builder` | — | 0.0 | 200 | Deterministic single-node Apps build/action. |

### 6.7 Why the token ceilings are 65536

The provider's real `maxOutputTokens`. The old 1,500–6,000 defaults truncated
reports mid-sentence (a 12-page doc is ~5,400 words + JSON overhead; 16,000 ran
out mid-`blocks`, rendering a half-empty file). Cost scales with what is written,
not the ceiling.

### 6.8 Why the researcher's citation table was removed

`verify_citations` (per-claim quote/source) cost **77%** of the findings: 255
words through the claim/quote/source schema vs 1,118 words of prose on the same
model/topic; 18 of 125 tool calls in one run verified quotes a Sources section
covers anyway. The tool still exists for other skills; it is not required here.

### 6.9 Why the author's tool list is small

Over 1,910 recorded tool uses: `render_document` 185, `web_search` 31,
everything else 0. `fetch_url`/`search_knowledge`/`arxiv_search`/… were carried
(~500 tokens of schema) on every call and never called — removed. `web_search`
stays (the only thing that stops a document being written from nothing when
research fails); `search_images`/`fetch_image` are the image supply.

### 6.10 Why the formatter is sectioned

Keyed models answer "write the report" with ~300 words regardless of the cap, so
length comes from splitting the work: one focused call per upstream result, then
assemble (`_sectioned_final_answer`).

---

## Part 7 — Tool layer (MCP surface)

### 7.1 Resolution flow
```
agent_config.yaml (tools_allowed)         # names
        │
        ▼
skills.py :: tool_payload()  ──look up──▶  _TOOL_CATALOG   # JSON schemas
        │ payload (names absent from the catalog are dropped loudly)
        ▼
mcp_runner.run_with_tools()  ──spawn──▶  mcp_server.py (stdio, 1 session/skill)
        │ model tool_call
        ▼
mcp_server tool fn  ──result──▶  fed back to model until final text
```

### 7.2 Tool inventory (by category)

**Web / research**
`web_search(query, max_results=5)`, `fetch_url(url, timeout=20, refresh=False)`,
`arxiv_search(query, max_results=5)`, `wikipedia_search(query)`,
`openalex_search(query, max_results=5)`, `news_search(query, days_back=7,
max_results=5)`, `fetch_pdf(url, timeout=30)`, `extract_tables(url, max_tables=5,
max_rows=50)`, `wayback_fetch(url, timestamp="")`, `search_images(query,
max_results=8, …)`, `fetch_image(url, credit="", filename="")`,
`verify_citations(evidence, timeout=12)`.

**Knowledge / memory**
`search_knowledge(query, k=5)`, `read_document(doc_id, start_chunk=0,
count=24)`, `remember_preference(preference, keywords=None)`,
`recall_preferences(k=10)`, `recall_observations(k=10, min_confidence=0.0)`,
`record_observation(observation, evidence, …)`.

**Files / code**
`read_file(path, offset=1, limit=0)`, `list_dir(path=".")`,
`search_files(pattern, path=".", max_hits=20, …)`, `find_symbol(name, path=".",
max_hits=40)`, `document_symbols(path)`, `find_references(name, path=".",
max_hits=200)`, `blast_radius(name, path=".", max_files=50)`,
`preview_edit(path, name, code, …)`, `apply_symbol_edit(path, name, code, …)`,
`create_file(path, content)`, `make_dir(path)`, `update_file(path, content)`,
`edit_file(path, find, replace, replace_all=False, …)`, `delete_file(path)`,
`index_document(path, chunk_size=400, overlap=80, …)`, `project_map(what="summary",
path=".", name="")`, `run_checks(path=".", timeout=0)`, `run_command(command,
cwd=".", timeout=0)`, `git_inspect(path=".", target="status", …)`,
`git_action(action, paths=None, message="")`, `studio_coder(task, paths=None, …)`.

**Documents / artifacts**
`render_document(format, title, blocks=None, …)`, `save_as_document(path,
filename="")`, `read_artifact(artifact_id, offset=0, …)`.

**Channels / actions** (forwarders to `/v1/integrations/{service}/{op}`)
`send_telegram`, `send_email`, `create_calendar_event`, `calendar_query`,
`calendar_refresh_token`, `gmail_query`, `gmail_profile`, `gmail_refresh_token`,
`zoho_mail_send/query/profile/folders/refresh_token/reply/mark/move/delete`,
`zoho_calendar_list_calendars/list_events`, `github_query` + `github_get_user/
list_repos/get_repo/read_file/list_issues/create_issue/update_issue/
comment_issue/list_prs/create_pr/search_repos`, `notion_query` + `notion_search/
get_page/get_page_markdown/get_blocks/append_blocks/query_database`,
`slack_message`, `slack_history`, `slack_refresh_token`, `discord_message` +
`discord_get_me/list_guilds/list_channels/invite_url`, `telegram_get_me/
get_updates/get_chat/send_message/edit_message/delete_message`, `ntfy_send/
history`, `zoho_cliq_message`, `bluesky_post`, `mastodon_post`,
`google_chat_message`.

**Scheduling / control**
`schedule_task(query, when, conversation_id="", …)`, `schedule_self(goal, when="",
email_from="", …)`, `list_scheduled()`, `cancel_scheduled(schedule_id)`,
`enable_scheduled(schedule_id, enabled=True)`, `schedule_wake(reason, every="1h",
surface="", …)`, `whats_new(since="", surfaces="", conversation_id="", …)`,
`list_channels()`, `check_channel(channel)`, `load_tool_bundle(bundle_id)`.

**Computer / browser / parts / apps**
`computer_action(action, params)`, `browser_open/status/snapshot/read/screenshot/
click/fill/press/scroll`, `blender_status/catalog/part_op/part_render/rna_get/
rna_set/python`, `app_list/app_get/app_build/app_action`, `parts_list/part_get/
part_analyse/part_repair/part_optimise/part_export/part_set_view/part_start_loop/
part_job_status/part_cancel_job`.

### 7.3 Progressive disclosure (action)

`effective_tools()` returns the **core** tools (non-channel) plus
`load_tool_bundle`. The node calls `load_tool_bundle("<channel>")` mid-run to
append that channel's schemas (`build_tool_bundles()`). `render_grant_header()`
tells the model exactly which tools are callable this run.

### 7.4 Channel → tools map
See Part 8.3.

### 7.5 `_JSON_CONTRACT_SKILLS`
`{planner, researcher, distiller, retriever, critic, summariser, formatter,
coder, action}` — must emit JSON; unusable output triggers repair (Part 16.2).

### 7.6 Safety in tools

`_safe()`/`_within()` confine file paths to the workspace; `_refuse_secret()`
blocks secret files; `_refuse_command()` refuses destructive shapes;
`neutralize_untrusted()` defuses prompt-injection in fetched pages;
`_guard_url()` blocks SSRF. Every command is audit-logged.

---

## Part 8 — Channels and integrations

### 8.1 Wiring (one place per concern)

| Layer | File | Role |
|---|---|---|
| Adaptor | `llm_gatewayV9/adaptors/<name>/` | `send()`, `normalize()`, `verify_webhook()`, `required_keys()`. |
| Integration | `llm_gatewayV9/integrations/<name>.py` | Functions the tools call; `{ok: …}`. |
| Dispatch | `channels_api.py` | `/v1/integrations/{service}/{op}`, `/v1/channels/{name}/send`. |
| MCP tool | `mcp_server.py` | Thin forwarder. |
| Bundle | `skills.py (CHANNEL_TOOLS)` | For `load_tool_bundle`. |
| Grant | `agent_config.yaml (action.tools_allowed)` | What action may call. |
| Docs | `key_info.py` | Names/purpose/where to get the key. |

### 8.2 Adaptors

`base.py` (`BaseAdaptor`; `AdaptorError`/`NotConfigured`/`NotIntegrated`),
`envelope.py` (`ChannelMessage`/`ChannelReply`/`TrustLevel`),
`registry.py` (lazy, env-gated), `trust.py` (pairing store), `http.py`. Packages:
bluesky, discord, gmail, google_chat, imap, line, local_mic, mastodon, matrix,
ntfy, signal, slack, teams, telegram, twilio_sms, twilio_voice, webhook, webui,
whatsapp_meta, whatsapp_twilio, zoho_cliq, zoho_mail.

### 8.3 Channel → tools map (`CHANNEL_TOOLS`)

| Channel | Tools |
|---|---|
| gmail | send_email, gmail_query, gmail_profile, gmail_refresh_token |
| zoho_mail | zoho_mail_send/query/profile/folders/refresh_token/reply/mark/move/delete |
| calendar | create_calendar_event, calendar_query, calendar_refresh_token |
| zoho_calendar | zoho_calendar_list_calendars, zoho_calendar_list_events |
| github | github_query, get_user, list_repos, get_repo, read_file, list/create/update/comment issues, list/create PRs, search_repos |
| notion | notion_query, search, get_page, get_page_markdown, get_blocks, append_blocks, query_database |
| telegram | send_telegram, get_me, get_updates, get_chat, send/edit/delete_message |
| slack | slack_message, slack_history, slack_refresh_token |
| discord | discord_message, get_me, list_guilds, list_channels, invite_url |
| ntfy | ntfy_send, ntfy_history |
| zoho_cliq | zoho_cliq_message |
| bluesky | bluesky_post |
| mastodon | mastodon_post |
| google_chat | google_chat_message |

### 8.4 Integrations

Per-service functions (fail-soft `{ok: false, error: "... not set"}`). See
`integrations/README.md`.

### 8.5 Cost policy

Free protocol first; pay only where there is no free option (Twilio SMS
~$0.008/msg is the main unavoidable cost). Free channels wired: ntfy, Zoho Cliq,
Zoho Mail, Bluesky, Mastodon, Google Chat.

### 8.6 Policy engine

`policy/engine.py` evaluates `{trust, tool, channel, agent, session, spend_usd,
over_spend_cap, destructive}` → verdict. First match wins; ties deny; dry-run
reports but allows. `policy.yaml` holds defaults (dry_run, default_action deny,
spend caps) and rules (owner-allow, paired-messaging, channel ops, untrusted
deny-all).

### 8.7 Two-way chat

Inbound pollers (`telegram_poller.py`, `gmail_poller.py`, `slack_poller.py`)
verify + trust-check the sender and turn the message into a normal chat turn
(`channel_chat.py`, `run_kind="chat"` — the full DAG runs).

---

## Part 9 — The Sandbox

`sandbox.py` is the resource/process boundary for all arbitrary code (project
checks, `run_command`, coder `run_python`).

### 9.1 Guarantees
- **Hard wall-clock timeout** that kills the whole **process tree**.
- **Windows Job Object**: `KILL_ON_CLOSE`, `LIMIT_ACTIVE_PROCESS`,
  `LIMIT_PROCESS_MEMORY`, `LIMIT_JOB_MEMORY` (defaults 24 procs, 1024 MB/proc,
  2× job).
- **POSIX**: new session/process group, SIGKILL.
- **Bounded output buffer** that keeps draining pipes.
- **Environment scrub** (`clean_env`) removes credential-shaped names and
  `ARIA_*`.

### 9.2 `run_python` contract
Writes `main.py` to a fresh temp dir, runs with a whitelisted env
(`PATH, HOME, LANG, LC_ALL, LC_CTYPE`), `PYTHONIOENCODING=utf-8`,
`PYTHON_TIMEOUT_S=30`, 1,000,000-byte stdout/stderr caps. Returns
`{exit_code, stdout, stderr, files_written, timed_out, timeout_s, cwd}`; temp dir
deleted before return.

### 9.3 What it is NOT
A filesystem or network jail. The command guard is heuristic; network remains
possible. That boundary is the approval gate (and, for hard isolation, a
container/AppContainer backend).

### 9.4 `sandbox_executor` handoff
`skills.py` lifts `code` from the upstream coder node and calls `run_python`
directly (bypasses the gateway). A deterministic timeout is tagged
`[deterministic-timeout]` so recovery skips rather than loops. **Known bug:** the
coder sometimes emits `script_content` instead of `code` (Part 20).

---

## Part 10 — Computer use

The `computer_use/` package drives a real desktop app via **cua-driver**. Disabled
by default (`COMPUTER_USE_ENABLED`), dry-run by default; sensitive actions require
in-UI approval.

### 10.1 Modules
`engine.py` (`ComputerUseSkill`, the cascade), `daemon.py` (shim → `core.daemon`),
`core/` (canonical daemon), `tool.py` (in-process `computer_action` dispatch),
`shell.py` (L0), `private_desktop.py`, `safety/`, `layers/`, `prompts/`, `apps/`.

### 10.2 Layer cascade
- **L0 gated-shell** — fallback when the daemon is unavailable.
- **L1 extract** — AX tree / clipboard / file (0 LLM).
- **L2a deterministic** — known hotkey sequences (0 LLM).
- **L2b a11y** — `get_window_state` markdown + cheap LLM → `element_index`.
- **L3 vision** — screenshot + set-of-marks → `/v1/vision` → click `(x,y)`.

Scan-act-verify up to `max_turns` (12) with recovery and vision fallback.
Judge/vision default to a fast model (`COMPUTER_USE_JUDGE_MODEL` /
`COMPUTER_USE_VISION_MODEL` / `COMPUTER_USE_REASONING`). Harness: `open_app`,
change-detection settle, no-relaunch on stale, action history to judge,
malformed-click re-ask.

### 10.3 Layers
`goal.py` (A), `extract.py` (1), `deterministic.py` (2a), `perception.py` (B),
`sequencing.py` (C), `recovery.py` (D), `vision.py` (E). See
`computer_use/layers/README.md`.

### 10.4 Safety
`gates.py` (ENABLED → MODE → ALLOW/DENY → APPROVAL → AUDIT), `permissions.py`
(pre-flight). Protected paths never writable; approvals resolved in-UI
(`?cu=1`).

---

## Part 11 — The Browser skill

Framework-free (Playwright + Pillow + httpx). Four-layer cascade in `skill.py`:
1. HTML extract (`trafilatura`, no LLM).
2. Deterministic selectors (if `metadata.selectors`).
3. `A11yDriver` (`/v1/chat`, text-only).
4. `SetOfMarksDriver` (`/v1/vision`).

Escalates on empty/insufficient; stops at the first useful answer.
`gateway_blocked` on CAPTCHA/login-wall → recovery re-invokes the planner.
Switches in `flags.py` (`browser.tools`, `browser.headed`). See
`browser/README.md`.

---

## Part 12 — Studio (coding workspace)

A full coding agent + workspace, separate from the DAG sandbox coder.

| Module | Concern |
|---|---|
| `studio_workspace.py` | Sessions + declared roots (allow-list). |
| `studio_review.py` | Baselines, content-fingerprinted hunks, checkpoints. |
| `studio_runs.py` | Run registry + JSONL transcripts + replay. |
| `studio_composer.py` | `@` mentions, `/` commands, commit messages. |
| `studio_files.py` | File ops, search/replace plans, git argv — planners only. |
| `browser_session.py` | The session's persistent Chromium. |

- Reads before it writes; runs the project's own checks; iterates.
- Every change is a reviewable hunk (Keep/Revert) in a restorable checkpoint.
- Prompt `prompts/studio_coder.md`, re-read on mtime change.
- Default workspace `state/code_workspace` (empty scratch); with a session bound
  it is that session's roots.
- Routes: `/api/studio/*` and `/api/code/*` (Appendix A).

---

## Part 13 — Memory and retrieval

- **Drawers**: working (per-run), episodes, facts, documents.
- **Hybrid retrieval**: dense vectors + BM25, fused with **RRF**.
- Memory read once at session start, shared into every skill's prompt; memory
  (facts) and history (what was said) kept distinct.
- `remember_preference`/`recall_preferences` for mid-run corrections.
- Each chat turn is a fresh graph; the session keeps a stable identity.
- `MemoryItem` kinds: `fact`, `preference`, `tool_outcome`, `scratchpad`,
  `observation` (Appendix C).

---

## Part 14 — Document generation

1. Model produces a structured **spec** (sections/slides/sheets).
2. `render_document` renders real **PDF/PPTX/DOCX/XLSX** (reportlab /
   python-pptx / python-docx / openpyxl; LaTeX/Typst/markup paths exist).
3. Bytes stored **content-addressed** (`art:` / `doc-`).
4. Receipt + file list from the **renderer**, never the model.

Long documents are written section-by-section (`_sectioned_document_blocks`); the
formatter assembles (`_sectioned_final_answer`). `length_contract()` gives writers
a concrete length target.

---

## Part 15 — State on disk

`S9SharedCode/code/state/`: `sessions/`, `memory/` (+ vector index),
`runs/<bucket>/<run>.jsonl` + `index.json`, `code_workspace/`, `agent.token`.
Gateway `llm_gatewayV9/`: `gateway_v8.db`, `documents/`, `memory/`, `parts/`,
`state/`.

---

## Part 16 — Reliability and failure model

### 16.1 Empty completions
Gateway retries the same provider **twice** then fails over (Part 3.3).

### 16.2 Unusable / mismatched JSON
Every `_JSON_CONTRACT_SKILLS` node that returns empty/malformed JSON gets **one
corrective call** (`_repair_json_reply`) with its schema. The
`not reply.get("error")` gate was removed so empty/error replies are repaired, not
skipped.

### 16.3 Re-plan on failure
`plan_recovery` + `_splice_recovery` (Part 5.4). Deterministic failures are
classified environmental and skipped.

### 16.4 Critic loop
A `critic: true` skill auto-inserts a Critic; a `fail` with a specific rationale
drives targeted recovery.

### 16.5 Budgets, Stop, idempotency
Soft per-skill budgets; cooperative Stop inside nodes; per-session idempotency
released on Stop.

### 16.6 Failure classification table
| Failure | Class | Action |
|---|---|---|
| 429 / 5xx from provider | transient | retry / failover |
| Empty completion (200, 0 text) | transient | retry 2× then failover |
| Unusable JSON | recoverable | repair call, then fail+replan |
| Sandbox wall-clock timeout | deterministic | skip (no loop) |
| Missing credential | environmental | fail-soft `{ok:false}` |
| Gateway blocked (CAPTCHA) | environmental | re-plan via planner |

---

## Part 17 — Security and privacy

- Loopback + per-launch token; cross-origin writes rejected.
- Credentials in the gateway, never the agent.
- File tools confined to declared roots; escaping paths refused.
- Commands/writes approval-gated; every attempt audit-logged.
- Code execution resource-bounded + env-scrubbed.
- Secrets never committed (`.env` reads blocked by tooling).
- Fail-closed on trust (unpaired sender → no turn).
- Tool hardening: `_safe`/`_within`, `_refuse_secret`, `_refuse_command`,
  `neutralize_untrusted`, `_guard_url`.

---

## Part 18 — Cost model

Free-first; automatic failover; caps per call/agent/session; ledger
(`/v1/spend`, `/v1/cost/by_agent`); progressive disclosure keeps tool schema
small; prompt-cache reuse via provider pinning.

---

## Part 19 — Verification

```
# Agent (from S9SharedCode/code — NOT bare `uv run pytest`)
uv run python -m pytest tests/ -q

# Gateway (from llm_gatewayV9 — NOT `tests/`; most suites live outside it)
uv run python -m pytest -q

# Frontend (from console-frontend)
npx tsc --noEmit
npm run build

# Live
curl -s localhost:8500/api/health      # gateway_up: true
curl -s localhost:8109/health
```

`TEST.md` is the Studio checklist. Restart both services to pick up changes;
verify with `/api/health` + `/health`.

---

## Part 20 — Known issues and hard-won fixes

1. **Coder → sandbox key mismatch.** `sandbox_executor` reads only
   `output["code"]`; the coder sometimes returns `script_content` (valid JSON,
   node "complete") → "no code in upstream coder output", code dropped. **Fix:**
   normalize the coder output (`script_content`/`script`/`content` → `code`) and
   let the sandbox accept the synonym.
2. **Vision on an artifact handle.** `vision_file` got `"n:2"` instead of a path →
   `unsupported image type ''`. **Fix:** resolve `n:`/`art:` handles to a file
   first.
3. **Documented vs reachable.** Action prompt named `list_channels`/`schedule_wake`
   not in the grant. **Fix:** `test_prompt_truth.py` enforces grant direction.
4. **Orphan sandbox executor.** `planner → sandbox_executor → formatter` with no
   coder re-planned twice (3 planner nodes, 59.4s). **Fix:**
   `ensure_sandbox_has_code`.

Earlier fixes in the tree: empty-completion retry 1→2; repair-on-empty for all
JSON skills; computer-use `open_app` + settle + no-relaunch + history +
malformed-click re-ask; `if/then` schema reverted (Gemini strict-mode 400).

---

## Part 21 — Glossary

| Term | Meaning |
|---|---|
| **DAG** | Directed acyclic graph of skill-nodes. |
| **Node / skill** | One unit of work. |
| **Planner** | Decomposes a request into the graph + recovery. |
| **Formatter** | Terminal node; writes the final answer. |
| **Wave** | A bounded-parallel batch (`MAX_CONCURRENCY = 8`). |
| **Recovery / splice** | Re-plan that replaces a failed node. |
| **Critic** | Auto-inserted pass/fail reviewer. |
| **Facet** | One research angle (the research budget). |
| **Drawer** | A scope of memory. |
| **RRF** | Reciprocal Rank Fusion. |
| **Artifact** | Content-addressed rendered file (`art:`/`doc-`). |
| **Progressive disclosure** | Action loads a channel's tools on demand. |
| **Grant** | The exact tool list a skill may call this run. |
| **Sandbox** | The resource/process boundary. |
| **L0–L3** | Computer-use cascade. |
| **Studio** | The full coding workspace. |

---

## Appendix A — Agent API route index

**Pages / static:** `/`, `/console`, `/console.js`, `/console.css`, `/app.js`,
`/style.css`, `/shared.css`, `/runs`, `/memory`, `/scheduler`, `/skills`,
`/ledger`, `/settings`, `/mission`, `/research`, `/apps`, `/assets/{path}`.

**Chat / runs:** `POST /api/chat`, `GET /api/chat/threads`, `…/prefs`,
`GET /api/runs/summary`, `GET /api/sessions`, `GET /api/sessions/{sid}/graph`,
`GET /api/sessions/{sid}/nodes/{nid}`, `DELETE /api/sessions/{sid}`,
`GET /api/sessions/{sid}/produced`, `GET /api/sessions/{sid}/browser-shots`,
`GET /api/events`, `GET/POST /api/feedback`, `GET /api/feedback/learning`,
`GET /api/feedback/rollup`.

**Documents / artifacts:** `GET/POST /api/documents`, `/folders`,
`/{doc_id}`, `/enabled`, `/reindex`, `/content`, `/preview`, `/slides`,
`/slide/{n}`, `DELETE /api/documents/{doc_id}`, `POST /api/documents/search`,
`GET /api/artifacts/{sid}/{path}`, `GET /api/artifact/{id}`,
`/preview`, `/slides`, `/slide/{n}`, `POST /api/artifact/{id}/documents`,
`POST /api/doc/preview`, `GET/POST /api/docgen`, `GET /api/docgen/formats`.

**Memory / profile:** `GET /api/memory`, `/stats`, `POST /api/memory/remember`,
`DELETE /api/memory`, `/{id}`, `POST /api/memory/policy`, `GET/POST /api/profile`,
`/persona`, `/persona/evolution`, `/persona/regenerate`, `/persona/document`,
`/persona/sync`, `POST /api/profile/document`.

**Apps / A2UI:** `GET/POST /api/apps`, `/build`, `/{app_id}`, `/actions`,
`/refresh`, `/a2ui`, `/prefab`, `/prefab-token`, `DELETE /api/apps/{app_id}`,
`GET /api/apps/flags`, `POST /api/apps/flags`, `GET /api/a2ui/catalog`,
`POST /api/a2ui/validate`, `POST /api/a2ui/generate`.

**Code / Studio:** `/api/code/*` (roots, workspace, tree, files, file, check,
checks, git, intel, write, newfile, permission, agent, agent/changes, keep,
revert, revert-hunk, ask, search), `/api/studio/*` (roots, fs, pick-folder,
sessions, runs, live, mentions, commands, commit-msg, files, search, replace,
journal, git, changes, checkpoints, checkpoint/restore, browser/*).

**Scheduler / notify:** `GET/POST /api/schedule`, `DELETE /api/schedule/{sid}`,
`/{sid}/enabled`, `/purge`, `GET /api/whats-new`, `GET/POST /api/notifications`,
`GET /api/ack/threads`, `POST /api/ack/inbound`, `POST /api/channel/inbound`,
`GET /api/channel/threads`, `GET /api/notify/channels`, `POST /api/notify/refresh`.

**Parts:** `GET /api/parts`, `POST /api/parts`, `/passes`, `/geometry`,
`/optimise`, `/capabilities`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/cancel`.

**Voice / config / misc:** `POST /api/tts`, `POST /api/stt`,
`GET/POST /api/config`, `/api/config/tools`, `GET /api/capabilities`,
`GET /api/cost`, `/by_skill`, `GET /api/nodes/health`, `GET /api/audit`,
`GET /api/tools`, `GET /api/health`, `GET /api/model-lab/status`,
`GET/POST/DELETE /api/gateway/user`, `POST /api/agui`, `GET /api/mcp/stats`.

## Appendix B — Gateway endpoint index

`/v1/chat`, `/v1/vision`, `/v1/embed`, `/v1/embedders`, `/v1/docgen`,
`/v1/documents` (+ `/{id}/figures`), `/v1/memory/search`,
`/v1/memory/record_outcome`, `/v1/memory` (CRUD), `/v1/integrations/{service}/{op}`,
`/v1/channels`, `/v1/channels/{name}/send`, `/v1/config/keys`, `/v1/status`,
`/v1/routers`, `/v1/providers`, `/v1/capabilities`, `/v1/calls`, `/v1/spend`,
`/v1/policy`, `/v1/cost/by_agent`, `/v1/control/*`, `/health`.

## Appendix C — Data schemas

**MemoryItem** — `id`, `kind` (`fact|preference|tool_outcome|scratchpad|
observation`), `keywords[]`, `descriptor`, `value{}`, `artifact_id?`,
`embedding?`, `source`, `run_id`, `goal_id?`, `confidence`, `created_at`,
`drawer?`, `session_id?`, `superseded_by?`.

**Artifact** — content-addressed handle + metadata (bytes live in the artifact
store, not in memory).

**NodeSpec / AgentResult** — the planner's node contract and each node's result
(`success`, `agent_name`, `output`, `error`, `elapsed_s`, `cost`).

**ChatRequest (gateway)** — messages, model, tools, tool_choice, reasoning,
response_format, images, cache_system, agent, session.

## Appendix D — Environment variables

**Agent:** `COMPUTER_USE_ENABLED`, `COMPUTER_USE_MODE` (`dry-run|live`),
`COMPUTER_USE_JUDGE_MODEL`, `COMPUTER_USE_VISION_MODEL`, `COMPUTER_USE_REASONING`.
**Gateway:** provider keys (`GEMINI_API_KEY`, `GROQ_API_KEY`, `NVIDIA_API_KEY`,
`CEREBRAS_API_KEY`, `OPEN_ROUTER_API_KEY`, `GITHUB_ACCESS_TOKEN`, `OLLAMA_URL`, …),
channel tokens (`TELEGRAM_BOT_TOKEN`, `GMAIL_TOKEN`, `SLACK_BOT_TOKEN`,
`NOTION_TOKEN`, `DISCORD_*`, `NTFY_TOKEN`, `BSKY_HANDLE`, `MASTODON_TOKEN`,
`GOOGLE_CHAT_WEBHOOK`, `ZOHO_*`), `EMBED_OLLAMA_MODEL`, `EMBED_ORDER`,
`LLM_ORDER`, `GATEWAY_V9_PORT`. **Never commit `.env`** — only `.env.example`.

---

*Aria runs on your machine. This handbook is the exhaustive reference; the
per-folder READMEs and `docs/` cover narrower slices.*

---

# Appendices (expanded)

## Appendix E — Full MCP tool signature reference

Every tool the model can call, with its exact signature (from `mcp_server.py`).
Grouped by category. Optional params shown with defaults.

### E.1 Web & research
```
web_search(query, max_results=5)
fetch_url(url, timeout=20, refresh=False)
arxiv_search(query, max_results=5)
wikipedia_search(query)
openalex_search(query, max_results=5)
news_search(query, days_back=7, max_results=5)
fetch_pdf(url, timeout=30)
extract_tables(url, max_tables=5, max_rows=50)
wayback_fetch(url, timestamp="")
search_images(query, max_results=8, ...)
fetch_image(url, credit="", filename="")
verify_citations(evidence, timeout=12)
```

### E.2 Knowledge & memory
```
search_knowledge(query, k=5)
read_document(doc_id, start_chunk=0, count=24)
remember_preference(preference, keywords=None)
recall_preferences(k=10)
recall_observations(k=10, min_confidence=0.0)
record_observation(observation, evidence, ...)
```

### E.3 Files & code
```
read_file(path, offset=1, limit=0)
list_dir(path=".")
search_files(pattern, path=".", max_hits=20, ...)
find_symbol(name, path=".", max_hits=40)
document_symbols(path)
find_references(name, path=".", max_hits=200)
blast_radius(name, path=".", max_files=50)
preview_edit(path, name, code, ...)
apply_symbol_edit(path, name, code, ...)
create_file(path, content)
make_dir(path)
update_file(path, content)
edit_file(path, find, replace, replace_all=False, ...)
delete_file(path)
index_document(path, chunk_size=400, overlap=80, ...)
project_map(what="summary", path=".", name="")
run_checks(path=".", timeout=0)
run_command(command, cwd=".", timeout=0)
git_inspect(path=".", target="status", ...)
git_action(action, paths=None, message="")
studio_coder(task, paths=None, ...)
```

### E.4 Documents & artifacts
```
render_document(format, title, blocks=None, sheets=None, subtitle="",
                author_name="", toc=False, running_header="", page_size="",
                orientation="", margins="", columns=0, style="",
                citation_style="", references=None, slide_size="", typeset="")
save_as_document(path, filename="")
read_artifact(artifact_id, offset=0, ...)
```
`blocks` item shapes (the authoring contract):
```
{"type":"heading","level":1-3,"text":...}
{"type":"paragraph","text":"... **bold**, `code`, [link](url)"}
{"type":"bullets"|"numbers","items":[...]}
{"type":"quote","text":...}
{"type":"table","header":[...],"rows":[[...]]}
{"type":"chart","kind":"bar|line|pie","categories":[...],
 "series":[{"name":...,"data":[...]}],"title":...}
{"type":"image","document":"<doc id>","page":1,"caption":...}
{"type":"cover","title":...,"subtitle":...,"meta":[...]}
{"type":"pagebreak"}
```
Formats: `pdf, pptx, docx, xlsx` (layout engines) and `md, html, csv, json, epub`
(serialisations of the same block model).

### E.5 Channels & actions
```
send_telegram(chat_id, message, document="", ...)
send_email(to, subject, body, document="", ...)
create_calendar_event(summary, start, end, description="", ...)
calendar_query(time_min="", time_max="", ...)
calendar_refresh_token(write_env=True)
gmail_query(api_method, query="", max_results=5)
gmail_profile()
gmail_refresh_token(write_env=True)
zoho_mail_send(to, subject, body, document="", ...)
zoho_mail_query(api_method, query="", max_results=5)
zoho_mail_profile()
zoho_mail_folders()
zoho_mail_refresh_token(write_env=True)
zoho_mail_reply(message_id, body, to="", subject="")
zoho_mail_mark(message_id, mode="read")
zoho_mail_move(message_id, dest_folder_id)
zoho_mail_delete(message_id)
zoho_calendar_list_calendars()
zoho_calendar_list_events(calendar_id="primary", ...)
github_query(api_method, owner="", repo="", ...)
github_get_user()
github_list_repos(limit=30)
github_get_repo(owner, repo)
github_read_file(owner, repo, path, ref="")
github_list_issues(owner, repo, state="open", limit=30)
github_create_issue(owner, repo, title, body="")
github_update_issue(owner, repo, number, state="closed", ...)
github_comment_issue(owner, repo, number, body)
github_list_prs(owner, repo, state="open", limit=30)
github_create_pr(owner, repo, title, head, base, body="")
github_search_repos(query, limit=20)
notion_query(api_method, page_id="", database_id="", ...)
notion_search(query="", page_size=20)
notion_get_page(page_id)
notion_get_page_markdown(page_id)
notion_get_blocks(block_id, page_size=100)
notion_append_blocks(block_id, text="", children=None)
notion_query_database(database_id, filter=None)
slack_message(channel, text)
slack_history(channel, limit=20)
slack_refresh_token()
discord_message(channel, text)
discord_get_me()
discord_list_guilds()
discord_list_channels(guild_id)
discord_invite_url(permissions="0")
telegram_get_me()
telegram_get_updates(offset=0, limit=100)
telegram_get_chat(chat_id)
telegram_send_message(chat_id, text)
telegram_edit_message(chat_id, message_id, text)
telegram_delete_message(chat_id, message_id)
ntfy_send(topic, message, title="")
ntfy_history(topic, since="", max_results=20)
zoho_cliq_message(target, text)
bluesky_post(text)
mastodon_post(text, visibility="public")
google_chat_message(text)
```

### E.6 Scheduling & control
```
schedule_task(query, when, conversation_id="", ...)
schedule_self(goal, when="", email_from="", ...)
list_scheduled()
cancel_scheduled(schedule_id)
enable_scheduled(schedule_id, enabled=True)
schedule_wake(reason, every="1h", surface="", ...)
whats_new(since="", surfaces="", conversation_id="", ...)
list_channels()
check_channel(channel)
load_tool_bundle(bundle_id)
```

### E.7 Computer / browser / parts / apps
```
computer_action(action, params)
browser_open(url, headed=False)
browser_status()
browser_snapshot(max_elements=120)
browser_read(kind="text", limit=6000)
browser_screenshot(full_page=False)
browser_click(mark=0, selector="", epoch=-1, ...)
browser_fill(text, mark=0, selector="", epoch=-1, ...)
browser_press(key="Enter")
browser_scroll(dx=0, dy=600)
blender_status()
blender_catalog(query="", limit=30)
blender_part_op(part, operator, version="", ...)
blender_part_render(part, version="", width=1024, ...)
blender_rna_get(path)
blender_rna_set(path, value)
blender_python(code)
app_list(...) / app_get(...) / app_build(...) / app_action(...)
parts_list / part_get / part_analyse / part_repair / part_optimise /
part_export / part_set_view / part_start_loop / part_job_status / part_cancel_job
```

### E.8 Tool safety wrappers
`_safe()`/`_within()` (path confinement), `_refuse_secret()` (secret files),
`_refuse_command()` (destructive shapes), `neutralize_untrusted()` (prompt
injection in fetched pages), `_guard_url()` (SSRF), `_audit_exec()` (audit log).

---

## Appendix F — Key endpoint contracts

### F.1 Agent `POST /api/chat`
Request:
```json
{ "query": "Research X and email me a PDF", "conversation_id": "cx-123" }
```
Response: `text/event-stream` with `data: {json}` frames:
```
{"type":"log","text":"[n:1] planner running · …"}
{"type":"log","text":"[n:1] planner complete (2.5s)"}
{"type":"done","answer":"…","session_id":"s8-…"}
{"type":"error","text":"…"}
```

### F.2 Agent `GET /api/health`
```json
{ "agent": "ready", "gateway_up": true, "spa_built": true }
```

### F.3 Agent `GET /api/runs/summary?limit=&offset=`
```json
{ "runs": [ { "session_id":"s8-…", "conversation_id":"…", "query":"…",
  "topic":"…", "nodes":7, "status_counts":{"complete":7},
  "skills":["planner","researcher",…], "usd":0.0, "updated":…, "has_graph":true } ],
  "total_runs":342, "has_more":true, "totals":{…} }
```

### F.4 Agent `GET /api/sessions/{sid}/graph?light=true`
```json
{ "nodes": [ { "id":"n:1", "skill":"planner", "status":"complete",
  "inputs":["USER_QUERY"], "metadata":{…} }, … ] }
```

### F.5 Agent `GET /api/sessions/{sid}/nodes/{nid}`
```json
{ "node_id":"n:2", "skill":"researcher", "status":"complete",
  "result": { "success":true, "agent_name":"researcher",
    "output": { "question":"…", "sources":[…], "findings":"…" },
    "cost":0.0, "elapsed_s":41.2, "input_tokens":…, "output_tokens":… } }
```

### F.6 Gateway `POST /v1/chat`
Request:
```jsonc
{ "messages":[{"role":"user","content":"…"}], "model":"…", "provider":"g",
  "max_tokens":2048, "temperature":0.7, "tools":[…], "tool_choice":"auto",
  "reasoning":"medium", "response_format":{"type":"json_schema","schema":{…}},
  "cache_system":true, "agent":"researcher", "session":"s8-…" }
```
Response:
```jsonc
{ "provider":"gemini", "model":"…", "text":"…", "tool_calls":[],
  "stop_reason":"end_turn", "input_tokens":1240, "output_tokens":87,
  "cache_read_input_tokens":0, "latency_ms":1834, "tool_call_dialect":"native",
  "reasoning_applied":false, "parsed":null, "attempted":[] }
```

### F.7 Gateway `POST /v1/docgen`
Request: `{ "format":"pdf", "title":"…", "blocks":[…], "toc":true, … }`.
Response: `{ "ok":true, "artifact":"art:…", "filename":"….pdf", "bytes":…,
"sections":[…] }`.

### F.8 Gateway `POST /v1/memory/search`
Request: `{ "query":"…", "k":5, "drawers":["working","facts"] }`.
Response: `{ "hits":[ { "id":"…", "kind":"fact", "descriptor":"…",
"value":{…}, "score":… } ] }`.

---

## Appendix G — Full database schema (`gateway_v8.db`)

### G.1 `calls` (one row per model call)
| Column | Type | Meaning |
|---|---|---|
| `id` | INTEGER PK | Row id. |
| `ts` | REAL | Timestamp. |
| `provider`, `model` | TEXT | What served the call. |
| `input_tokens`, `output_tokens` | INTEGER | Token usage. |
| `cache_create_tokens`, `cache_read_tokens` | INTEGER | Prompt-cache usage. |
| `latency_ms` | INTEGER | End-to-end latency. |
| `status`, `error` | TEXT | `ok` / error (redacted). |
| `prompt_chars`, `response_chars` | INTEGER | Size. |
| `override`, `attempted` | TEXT | Provider override + failover path. |
| `tool_calls` | INTEGER | Tool-call count. |
| `reasoning_applied` | INTEGER | Whether reasoning was applied. |
| `tool_dialect` | TEXT | `native` / prompted. |
| `call_role` | TEXT | `worker` / `router_*`. |
| `router_decision` | TEXT | Tier label / `fallback` / `error`. |
| `embed_dim` | INTEGER | Embedding dimension. |
| `agent`, `session` | TEXT | Attribution. |
| `retries` | INTEGER | Retry count. |
| `channel`, `trust_level` | TEXT | Channel + trust (V10). |
| `policy_verdict`, `policy_rule` | TEXT | Policy decision. |
| `user_id` | TEXT | User. |
| `thinking_tokens` | INTEGER | Reasoning tokens. |
| `output_tps` | REAL | Tokens/sec incl. thinking. |

Indexes: `idx_ts`, `idx_prov_ts`, `idx_role_ts`, `idx_agent_ts`, `idx_session_ts`,
`idx_channel_ts`. WAL + `synchronous=NORMAL` + `busy_timeout=10000`; thread-local
pooled connections. `log_call(...)` redacts `error`/`attempted` before insert and
computes `output_tps = (output+thinking)/latency`.

### G.2 `tool_uses` (one row per completed tool call)
`id`, `ts`, `tool`, `provider`, `model`, `agent`, `session`,
`call_role` (default `worker`), `status` (default `ok`). Indexes: `idx_tool_ts`,
`idx_toolprov_ts`. This is what lets the ledger name *which* tool ran, not just a
count.

---

## Appendix H — Provider & model catalogue

### H.1 Provider classes (`providers.py`)
| Class | Notes |
|---|---|
| `BaseProvider` | Interface. |
| `OpenAICompatProvider` | Most providers (OpenAI-compatible). |
| `GroqProvider`, `CerebrasProvider`, `NvidiaProvider`, `OpenRouterProvider`, `GitHubProvider`, `KiloProvider` | Thin subclasses. |
| `GeminiProvider` | Google; schema cleaning, thinking config, streaming. |
| `OllamaProvider` | Local; native tools. |

Helpers: `model_capabilities(provider, model, default_caps)`,
`route_for_model(model, agent)` (honours `agent_routing.yaml`),
`register_model_routes(...)`, `register_kilo_model_routes(...)`.

### H.2 Capability model
Each model advertises tools / caching / reasoning / structured / parallel-tools.
The router filters by capability + rate state. `reasoning` knob (`low|medium|high`)
maps to provider-specific thinking config (Gemini `thinking`, others
`reasoning_effort`).

### H.3 Free-tier notes
Gemini ~15 rpm / 1,000 rpd; heavy bursts 429 → failover. `cache_system` is
paid-only on Gemini (free keys ignore). Gemini 3.x prefers `temperature≈1.0`
(low temp can loop on schema-constrained calls).

---

## Appendix I — Console pages (deep dive)

`console-frontend/src/views/` (React):
| Page | File | What it shows |
|---|---|---|
| Mission | `Mission.tsx` | Landing surface. |
| Chat | `Chat.tsx` | Quick answers + conversation, memory + document grounding (no DAG). |
| Research | `Research.tsx` | Submit a brief, watch the DAG live (graph + inspector + timeline). |
| Runs | `Runs.tsx` | Every stored run: status, node counts, cost, age. |
| Memory | `Memory.tsx` | Browse/search/write memory (facts, prefs, notes, drawers). |
| Documents | `Documents.tsx` | Upload, enable/disable, re-index, preview, search. |
| Authoring | `Authoring.tsx` | Ask for a document in prose, get a rendered file. |
| Code | `Code.tsx` | Read-only workspace browser. |
| Studio | `studio/` | The coding workspace (explorer, editor, diff, terminal, checkpoints). |
| Model Lab | `ModelLab.tsx` | 124M model showcase + playground. |
| Scheduler | `Scheduler.tsx` | Scheduled tasks + self-wakes. |
| Skills | `Skills.tsx` | Capability packs + per-tool enable/disable. |
| Apps | `Apps.tsx` | Generated app surfaces + prefabs. |
| Ledger | `Ledger.tsx` | Spend by skill, turn history, reliability. |
| Architecture | `Architecture.tsx` | The system's own architecture. |
| Parts | `Parts.tsx` | 3D mesh analysis + repair reports. |
| Computer | `Computer.tsx` | Desktop-control actions + approval queue (`?cu=1`). |
| Settings | `Settings.tsx` | Env + read-only config status. |
| DocSetup | `DocSetup.tsx` | Authoring document setup. |

Stack: React 19, React Router 7, `@xyflow/react` (DAG), `monaco-editor`,
`@xterm/xterm`, `three`, `pdfjs-dist`, `@tanstack/react-virtual`, `motion`,
`sonner`, `lucide-react`, `cmdk`. Theme: violet accent (shared with the desktop
shell). API client: `src/api.ts` (token header + SSE).

---

## Appendix J — Subsystem deep dives

### J.1 Apps / A2UI
`apps.py`, `apps_a2ui.py`, `a2ui_catalog.py`, `a2ui_surfaces.py`,
`app_builder.py`, `prefab_views.py`. The app builder owns the surface; `app_worker`
builds/changes it; `app_builder` is a deterministic single-node DAG skill for the
Apps endpoints. Routes: `/api/apps*`, `/api/a2ui/*`. Prefabs are reusable surface
templates with a token (`/api/apps/{id}/prefab-token`).

### J.2 Parts / Blender
`parts_service.py`, `parts_store.py`, `blender_service.py`, `blender_bridge/`.
Skills: `part_analyst` (read-only verdict/issues/metrics), `part_engineer`
(repair/optimise, versions every result), `part_blender` (real operators, renders,
RNA, Python). Routes: `/api/parts*`. The headless worker and live GUI share one
bridge catalog.

### J.3 Voice
`voice_api.py`, `voice_chunking.py`; Kokoro ONNX TTS in `models/`
(`kokoro-v1.0.onnx`, `voices-v1.0.bin`), warmed at import; `/api/tts` returns a
WAV; `/api/stt` proxies speech-to-text. Chunking splits long text for TTS.

### J.4 Scheduler & self-continuation
`scheduler.py`, `sleeps.py`, `notify.py`. `schedule_task` (one-shot/recurring),
`schedule_self` (hand a goal + summary to a future turn; timer or email-event
wake), `schedule_wake` (self-poll watch, flagged). On wake, recent research is
prepended inline. Routes: `/api/schedule*`, `/api/whats-new`.

### J.5 Persona & feedback
`persona.py` (persona document + evolution), `feedback_learning.py`
(`/api/feedback`, `/api/feedback/learning`, `/api/feedback/rollup`) — per-node
feedback that tunes future runs.

### J.6 Notifications & templates
`notify.py` (`/api/notify/*`, `/api/notifications`, `/api/ack/*`),
`templates.py` (`/api/templates*` — saved query templates you can run).

### J.7 Document generation internals
`render_document` block model (Appendix E.4). Engines: reportlab (PDF),
python-pptx (PPTX), python-docx (DOCX), openpyxl (XLSX); LaTeX/Typst/markup
alternatives. `toc=True` numbers level-1 sections; `running_header` adds a header;
charts are native/editable; images resolve from an uploaded `document` id + `page`.

---

## Appendix K — How to extend

### K.1 Add a skill
1. Add a prompt file in `prompts/`.
2. Add an entry in `agent_config.yaml` (`prompt`, `tools_allowed`, `temperature`,
   `max_tokens`, optional `internal_successors`/`critic`/`dynamic_channels`).
3. Ensure every tool in `tools_allowed` exists in `_TOOL_CATALOG` (else dropped).
4. Add tests; update `test_prompt_truth.py` expectations if the prompt names tools.

### K.2 Add a channel
One place per concern (Part 8.1): adaptor (`adaptors/<name>/`), integration
(`integrations/<name>.py`), dispatch (auto via `channels_api`), MCP tool
(`mcp_server.py`), bundle (`CHANNEL_TOOLS`), grant (`action.tools_allowed`), docs
(`key_info.py`). Inbound also needs a poller.

### K.3 Add a tool
1. Write the function in `mcp_server.py`.
2. Add its JSON schema to `_TOOL_CATALOG` in `skills.py`.
3. Grant it to the skill(s) in `agent_config.yaml`.
4. Add a test that the schema is valid and the tool is reachable.

### K.4 Add an endpoint
Add a route in `agent_server.py` (or `main.py` for the gateway), guard with the
token, add a test, document it in Appendix A/B.

### K.5 Add a provider
Subclass `OpenAICompatProvider` (or `BaseProvider`), register in
`build_providers()`/`register_model_routes()`, add `LIMITS`, add to `LLM_ORDER`.

---

## Appendix L — Runbook / troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/api/health` `gateway_up:false` | Gateway down | Start `llm_gatewayV9` (`uv run main.py`); check `:8109`. |
| Node fails "empty reply" | Free-tier empty completion | Gateway retries 2× + fails over; agent repairs. If persistent, use a paid model. |
| "no code in upstream coder output" | Coder emitted `script_content` not `code` | Part 20 #1 (normalize). |
| `unsupported image type ''` | vision_file got a node handle | Part 20 #2 (resolve handle). |
| Channel `invalid_auth` / "not set" | Credential missing | Configure the key in `llm_gatewayV9/.env` (names in `key_info.py`). |
| Run "incomplete" | A node failed and recovery could not fill it | Read the node error; the run log has detail. |
| `429` storms | Free-tier burst | Reduce concurrency / add a paid provider. |
| Stale code after edit | Process not restarted | Restart agent + gateway; verify `/api/health`. |

---

## Appendix M — Prompt catalogue (one line each)

`planner` (decompose), `researcher` (multi-source), `retriever` (indexed),
`distiller` (extract + critic), `critic` (pass/fail), `summariser` (condense),
`formatter` (final answer), `author`/`author_section` (documents),
`deck` (slides), `coder` (sandbox code), `studio_coder`/`studio_coder_delegate`
(full-workspace), `sandbox_executor` (run code), `browser` (web cascade),
`computer`/`computer_use_a11y`/`computer_use_vision`/`computer_use_user_*`
(desktop), `vision_file` (local image), `action` (side effects),
`part_analyst`/`part_engineer`/`part_blender` (3D), `app_worker`/`app_builder`/
`app_builder_skill` (apps), `compaction` (context compaction).

---

## Appendix N — Design decision log (the "why")

| Decision | Evidence / reason |
|---|---|
| Token ceilings = 65536 | 1,500–6,000 truncated reports; 16,000 ran out mid-`blocks`. |
| Remove researcher citation table | 255 vs 1,118 words; 77% of findings; 18/125 tool calls. |
| Shrink author tools | 1,910 uses: render 185, web_search 31, rest 0. |
| Section the formatter | Keyed models answer ~300 words regardless of cap. |
| No fanout cap | `MAX_FANOUT=4` discarded a 6-facet plan; facets = research budget. |
| `MAX_CONCURRENCY=8` | Unbounded waves 429 together on free tier. |
| Empty-completion retry 2× | Free Gemini 200-with-0-text; one retry was not enough. |
| Repair-on-empty for all JSON skills | Unusable JSON should be corrected, not skipped. |
| Progressive disclosure | Two-service request pays for two channels, not twenty. |
| Remove `if/then` schema | Gemini strict mode 400 ("Unknown name if"). |
| Receipts from renderer | A model claiming an artifact it did not produce is untrusted. |

---

## Appendix O — Worked examples

### O.1 A research → author → email run
```
planner → [researcher ×N (parallel)] + retriever → author (PDF) → action (email) → formatter
```
Each researcher files findings as a document; author renders from them; action
emails the artifact; formatter confirms. If a researcher returns empty, the
gateway retries, the agent repairs, and on still-fail the executor splices a
recovery researcher.

### O.2 A code → run → fix run
```
planner → coder → sandbox_executor → (critic/checks) → coder (fix) → formatter
```
`coder` has `internal_successors:[sandbox_executor]`; the sandbox runs the code;
`run_checks` failures send it back to the coder. A deterministic sandbox timeout
is skipped, not looped.

### O.3 A multi-channel read + create run
```
planner → action(calendar) + action(gmail) + action(slack) → summariser → action(notion) → action(telegram) → formatter
```
Each action loads only its channel's tools (progressive disclosure); a missing
credential fails soft (`{ok:false}`) and is reported honestly.

---

## Appendix P — Full endpoint reference

Every route, grouped. (Contracts for the key ones are in Appendix F.)

### P.1 Pages & static
| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Root (console). |
| GET | `/console`, `/console.js`, `/console.css` | Console shell. |
| GET | `/app.js`, `/style.css`, `/shared.css` | Static assets. |
| GET | `/runs`, `/memory`, `/scheduler`, `/skills`, `/ledger`, `/settings`, `/mission`, `/research`, `/apps` (+ `.js`) | Page stubs. |
| GET | `/assets/{path}` | Bundled assets. |

### P.2 Chat / runs / sessions
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/chat` | Stream a DAG run (SSE). |
| GET | `/api/chat/threads` | List threads. |
| GET/POST | `/api/chat/threads/{cid}/prefs` | Thread preferences. |
| GET | `/api/runs/summary` | Runs list + totals. |
| GET | `/api/sessions` | Sessions. |
| GET | `/api/sessions/{sid}/graph` | Graph (light). |
| GET | `/api/sessions/{sid}/nodes/{nid}` | Node detail. |
| DELETE | `/api/sessions/{sid}` | Forget a session. |
| GET | `/api/sessions/{sid}/produced` | Produced artifacts. |
| GET | `/api/sessions/{sid}/browser-shots` | Browser screenshots. |
| GET | `/api/events` | Event feed. |
| GET/POST | `/api/feedback`, `/api/feedback/learning`, `/api/feedback/rollup` | Feedback. |

### P.3 Documents / artifacts / docgen
| GET/POST | `/api/documents`, `/api/documents/{doc_id}`, `/enabled`, `/reindex`, `/content`, `/preview`, `/slides`, `/slide/{n}` | Document CRUD + preview. |
| GET/POST | `/api/documents/folders`, `/{folder_id}`, `/{doc_id}/folder` | Folders. |
| DELETE | `/api/documents/{doc_id}` | Delete. |
| POST | `/api/documents/search` | Search. |
| GET | `/api/artifacts/{sid}/{path}`, `/api/artifact/{id}`, `/preview`, `/slides`, `/slide/{n}` | Artifacts. |
| POST | `/api/artifact/{id}/documents` | Index an artifact. |
| POST | `/api/doc/preview` | Preview a spec. |
| GET/POST | `/api/docgen`, `GET /api/docgen/formats` | Render. |

### P.4 Memory / profile
| GET | `/api/memory`, `/api/memory/stats` | Memory. |
| POST | `/api/memory/remember`, `/api/memory/policy` | Write. |
| DELETE | `/api/memory`, `/api/memory/{id}` | Delete. |
| GET/POST | `/api/profile`, `/api/profile/document` | Profile. |
| GET/POST | `/api/profile/persona`, `/evolution`, `/regenerate`, `/document`, `/sync` | Persona. |

### P.5 Apps / A2UI
| GET/POST | `/api/apps`, `/api/apps/build`, `/api/apps/{id}`, `/actions`, `/refresh`, `/a2ui`, `/prefab`, `/prefab-token` | Apps. |
| PATCH/DELETE | `/api/apps/{id}` | Update/delete. |
| GET/POST | `/api/apps/flags` | Kill-switches. |
| GET | `/api/a2ui/catalog` | Catalog. |
| POST | `/api/a2ui/validate`, `/api/a2ui/generate` | Validate/generate. |

### P.6 Code / Studio
| GET/POST | `/api/code/roots`, `/workspace`, `/workspace/reset`, `/tree`, `/files`, `/file`, `/check`, `/checks`, `/git`, `/intel`, `/write`, `/newfile`, `/permission/{pid}`, `/agent`, `/agent/changes`, `/agent/keep`, `/agent/revert`, `/agent/revert-hunk`, `/ask`, `/search` | Code workspace. |
| GET/POST | `/api/studio/roots`, `/roots/remove`, `/fs/list`, `/pick-folder`, `/sessions`, `/session/rename`, `/session/archive`, `/session/forget`, `/runs`, `/runs/{id}`, `/runs/{id}/cost`, `/run/stop`, `/live`, `/playground`, `/workspace/reset`, `/mentions`, `/commands`, `/commit-msg`, `/files`, `/search`, `/replace`, `/journal`, `/git`, `/changes`, `/changes/apply`, `/checkpoints`, `/checkpoints/{id}`, `/checkpoint/restore` | Studio. |
| GET/POST | `/api/studio/browser/status`, `/events`, `/shots`, `/page`, `/navigate`, `/tab`, `/input`, `/close`, `/view` | Studio browser pane. |

### P.7 Scheduler / notify / channel
| GET/POST | `/api/schedule`, `DELETE /api/schedule/{sid}`, `/{sid}/enabled`, `/purge` | Schedule. |
| GET | `/api/whats-new` | What changed. |
| GET/POST | `/api/notifications` | Notifications. |
| GET | `/api/ack/threads` | Ack threads. |
| POST | `/api/ack/inbound`, `/api/channel/inbound` | Inbound. |
| GET | `/api/channel/threads` | Channel threads. |
| GET | `/api/notify/channels` | Notify channels. |
| POST | `/api/notify/refresh` | Refresh. |

### P.8 Parts / voice / config / misc
| GET/POST | `/api/parts`, `/passes`, `/geometry`, `/optimise`, `/capabilities`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/cancel` | Parts. |
| POST | `/api/tts`, `/api/stt` | Voice. |
| GET/POST | `/api/config`, `/api/config/tools` | Config. |
| GET | `/api/capabilities`, `/api/cost`, `/api/cost/by_skill`, `/api/nodes/health`, `/api/audit`, `/api/tools`, `/api/health`, `/api/model-lab/status`, `/api/mcp/stats` | Status/ops. |
| GET/POST/DELETE | `/api/gateway/user` | Gateway user. |
| POST | `/api/agui` | AG-UI protocol. |
| POST | `/api/templates`, GET `/api/templates`, `/{name}`, DELETE `/{name}`, POST `/{name}/run` | Templates. |

---

## Appendix Q — `flow.py` walkthrough (the executor)

### Q.1 `Graph`
A `networkx.DiGraph` wrapper. Nodes carry `skill`, `inputs`, `metadata`,
`status`, `error`, `started_at`, `completed_at`.
- `add_node(skill, inputs, metadata)` → `n:<counter>`; edges from `n:` inputs.
- `mark(nid, status, error)` — stamps timing; `started_at` once, `completed_at`
  on every terminal mark.
- `ready_nodes()` — `pending` nodes whose predecessors are all `complete` or
  `skipped` (a Critic-fail `skip` removes a child from the critical path).
- `has_running()`.
- `extend_from(src_nid, result, registry)` — splice dynamic successors + static
  `internal_successors` + critic auto-insertion.

### Q.2 Input resolution (the subtle part)
`extend_from` resolves `inputs` in two passes:
1. Add new nodes; build `label_to_id` (explicit `metadata.label` **and** skill
   name) and `idx_to_id` (sibling position).
2. Resolve each input: `n:<label>` → id; numeric `n:<i>` → id; `USER_QUERY` /
   `art:` pass through; a bare digit is a sibling position.
**Position convention:** the planner emits 0-based *or* 1-based positions. Resolve
0-based first; if any node ends up waiting on itself (a self-loop), the plan was
1-based → re-resolve with a `-1` shift. Whichever is self-loop-free wins. (A
self-loop means a node can never become ready — the author/action/formatter were
silently skipped and the run "finished" having only done research.)

### Q.3 The wave loop (Executor.run)
1. Planner node runs first → `extend_from` adds the initial graph.
2. Loop: `ready_nodes()` → run up to `MAX_CONCURRENCY` in parallel (asyncio
   gather); `mark` each; `extend_from` on dynamic successors; repeat until no
   ready/running nodes.
3. `MAX_NODES` stops scheduling (not quality). `NODE_BUDGET_S` informs the next
   call. `_stop_in_nodes()` publishes the stop predicate.
4. On node failure: `classify_failure` → transient (retry) or environmental
   (skip/replan via `plan_recovery` + `_splice_recovery`).
5. Terminal formatter → final answer.

### Q.4 Recovery splice
`_splice_recovery(graph, added, failed)` marks the failed original `skipped`,
rewires the recovery nodes in its place, and rewires waiting consumers.
`_replacement_answered` confirms the hole was filled. `_consumers_have_other_evidence`
avoids replanning when survivors are a majority.

---

## Appendix R — `skills.py` walkthrough (dispatch)

`run_skill(...)` is the per-node entry. Order of branches:
1. **Bypass branches** (no LLM tool loop):
   - `sandbox_executor` — lift `code` from upstream coder → `run_python`.
   - `browser` — build a `NodeSpec`, run `BrowserSkill` (knobs clamped via
     `metadata`: `max_steps_a11y`, `max_steps_vision`, `wall_clock_s`, pins).
   - `vision_file` — read `metadata.path`, validate (type/size), base64, one
     `LLM().vision()` call. (This is where the `unsupported image type ''`
     bug lives — a node handle instead of a path.)
   - `computer` — honour the tool guard, then `ComputerUseSkill`.
2. **Tool-using skills** — render prompt, run the MCP tool loop
   (`mcp_runner.run_with_tools`), parse JSON, repair if needed.
3. **Sectioned paths** — `_sectioned_findings` (researcher),
   `_sectioned_document_blocks` (author/deck), `_sectioned_final_answer`
   (formatter): one call per section, then assemble.
4. **Length contract** — `length_contract(query, skill)` gives writers a target.
5. **Figures** — `_figures_context(doc_ids)` + `_acquire_figures` for image
   blocks.

### R.1 JSON parse + repair
`_JSON_CONTRACT_SKILLS` must emit JSON. If `parsed` is falsy:
1. `_repair_json_reply` (one corrective call with the schema) — now fires even
   when the reply carried an error/empty (the gate was removed).
2. Still unusable → node fails; executor re-plans.

### R.2 Prompt rendering
`render_prompt(skill, query, resolved, failure_report, memory_hits, question,
prior_turns, policy_notes, budget_note, tools)` — injects the per-node
`question`, memory, the granted tools (`render_grant_header`), and the
format-specific author fragment.

---

## Appendix S — `mcp_server.py` internals

- **Path safety:** `_safe()`/`_within()` confine to workspace roots;
  `_refuse_secret()` blocks secret files; `_walk_files()` enumerates.
- **Fetch/extract:** `_extract_page` (async), `_fetch_uncached`, `_page_text`,
  `_cache_read`/`_cache_write`/`_cache_evict` (LRU), `cache_stats`.
- **Citations:** `verify_citations(evidence)` checks quotes against sources.
- **Source health:** `_record_source`, `_health_score`, `source_health`,
  `_health_order` — routes multi-source research by live health.
- **Doc store:** `_store_page_as_document`, `_store_hits_as_document`,
  `_document_stem`, `_page_document_name` — persist fetched pages/hits as
  documents (folder "Web sources").
- **Untrusted input:** `neutralize_untrusted(text, url)` defuses prompt injection;
  `_strip_untrusted_envelope`.
- **Command guard:** `_refuse_command`, `_posix_orientation`, `_audit_exec`.
- **Tool stats:** `_bump_stat`, `_stats_path`.

---

## Appendix T — The planner contract

The planner must emit a JSON graph. Each node (`NodeSpec`):
```json
{ "skill": "researcher", "inputs": ["USER_QUERY"], "goal": "…",
  "question": "…", "depth": "…", "label": "r1",
  "metadata": { "channels": ["gmail"], "format": "pdf", "url": "…" } }
```
- `inputs` reference `USER_QUERY`, `art:` handles, `n:<label>`/`n:<id>`, or
  sibling positions.
- `metadata.question` scopes a fan-out worker to its slice.
- The planner also emits **recovery subgraphs** for likely failures.
- **Facets** = research budget (no cap; the model decides).
- Node labels let the planner reference siblings without knowing integer ids.

---

## Appendix U — Action skill deep dive

- `dynamic_channels: true` → `effective_tools` returns core + `load_tool_bundle`.
- The node calls `list_channels()`/`check_channel()` before choosing a target,
  then `load_tool_bundle("<channel>")` to append schemas.
- Document-aware delivery: `send_email`/`send_telegram` accept a `document`
  (doc id, `art:` handle, or sandbox path) → `_document_blob` resolves bytes.
- Scheduling: `schedule_task`, `schedule_self`, `schedule_wake`, `list/cancel/
  enable_scheduled`.
- Fail-soft: a missing credential returns `{ok:false, error:"… not set"}`.

---

## Appendix V — Memory & retrieval internals

- `MemoryItem` kinds: `fact`, `preference`, `tool_outcome`, `scratchpad`,
  `observation`. Facts/preferences carry an `embedding` (FAISS); scratchpad is
  run-scoped and skips embedding.
- Hybrid: dense (vectors) + BM25 (keyword) fused with **RRF**.
- Drawers: working / episodes / facts / documents; recall scoped per drawer.
- Supersede: `superseded_by` links a correction to the old value.
- `remember_preference`/`recall_preferences` write/read durable prefs mid-run.

---

## Appendix W — Authoring internals

- Spec → `render_document` → bytes → content-addressed artifact (`art:`/`doc-`).
- Sectioned: `_sectioned_document_blocks` writes page-by-page from fetched pages;
  `_sectioned_final_answer` assembles.
- Figures: `_figures_context` lists handles; `_acquire_figures` fetches web
  images when the request wants pictures.
- `length_contract` gives a concrete target (a 300-page request otherwise ships
  16 words).
- Receipt from the renderer, never the model.

---

## Appendix X — Persistence & state

- Sessions: graph + per-node `NodeState` (`prompt_sent` is load-bearing for
  replay — the exact bytes that hit the gateway).
- Runs: `studio_runs.py` writes `state/runs/<bucket>/<run>.jsonl` + `index.json`.
- Checkpoints: `studio_review.py` baselines + content-fingerprinted hunks.
- Artifacts: `state/artifacts` (inert bytes) + a `document_id` back to the
  chunked/embedded copy.
- Gateway: `gateway_v8.db` (calls, tool_uses, memory, documents, cost).

---

## Appendix Y — Acronyms

DAG (directed acyclic graph), AX (accessibility tree), SoM (set-of-marks), RRF
(reciprocal rank fusion), BM25 (sparse keyword ranking), SSE (server-sent events),
MCP (Model Context Protocol), A2UI (agent-to-UI), TTS/STT (text/speech-to-text),
VLM (vision language model), WAL (write-ahead log), tpm/rpm/rpd (tokens/requests
per minute/day), SSRF (server-side request forgery).

---

## Appendix Z — Whole-repo file index (agent core)

| File | One-line |
|---|---|
| `agent_server.py` | FastAPI server + routes + SSE. |
| `flow.py` | DAG executor. |
| `skills.py` | Skill registry + dispatch + tool catalog + repair. |
| `recovery.py` | Failure classification + recovery planning. |
| `sandbox.py` | Resource/process boundary. |
| `mcp_server.py` / `mcp_runner.py` | MCP tools + tool loop. |
| `gateway.py` | V9 client. |
| `action.py` | Action helpers. |
| `channel_chat.py` | Inbound channel → DAG. |
| `agent_config.yaml` | Skill catalogue. |
| `flags.py` | Kill-switches. |
| `schemas.py` | Typed contracts. |
| `studio_workspace.py` / `studio_review.py` / `studio_runs.py` / `studio_composer.py` / `studio_files.py` / `browser_session.py` | Studio. |
| `apps.py` / `apps_a2ui.py` / `a2ui_catalog.py` / `a2ui_surfaces.py` / `app_builder.py` / `prefab_views.py` | Apps. |
| `parts_service.py` / `parts_store.py` / `blender_service.py` | Parts. |
| `memory.py` / `vector_index.py` | Memory + FAISS. |
| `scheduler.py` / `sleeps.py` / `notify.py` | Scheduling + notify. |
| `persona.py` / `feedback_learning.py` / `templates.py` | Persona/feedback/templates. |
| `code_index.py` / `code_intel.py` / `terminal.py` | Code workspace. |
| `docpreview.py` / `pptx_raster.py` / `report.py` | Preview/report. |
| `voice_api.py` (gateway) / `models/` | Voice. |
| `computer_use/` / `browser/` / `prompts/` / `console-frontend/` / `tests/` | Subsystems (own READMEs). |

---

# Deep dives (area by area)

> These appendices go one level deeper than Parts 0–21: the actual algorithms,
> constants, data structures, and edge cases, with the reasoning that produced
> them. Read Part 5/6/7 first for the model, then the matching deep dive here.

## Appendix AA — The tool loop (`mcp_runner.py`)

When a skill declares `tools_allowed`, dispatch goes through `run_with_tools`,
which drives the conversation until the model stops asking for tools:

```
1. chat(messages, tools=schemas)
2. if reply.tool_calls:
     for each tc: dispatch via MCP, append role="tool" message
     append assistant message with tool_calls
     goto 1
   else: return reply.text
```

One stdio MCP session per skill invocation (spawn ~100 ms; short-lived = no shared
mutable state between skills).

### AA.1 The constants (and why each exists)
| Constant | Value | Reason |
|---|---|---|
| `MAX_TOOL_HOPS` | 6 | Hard cap so a tool-loving model can't cost a fortune. |
| `MAX_SAME_CALL_REPEATS` | 2 | Same tool + same args more than twice = stuck rewording a failed verify. |
| `MAX_STALL_RECOVERIES` | 2 | When a repeat happens but the last check still failed, hand back the failing output + a corrective instruction instead of dead-ending. |
| `MAX_PARALLEL_TOOLS` | 8 | Tool calls from one assistant turn run concurrently (a "read these files" turn is the sum of reads serially otherwise). |
| `MAX_NO_PROGRESS_HOPS` | 8 | A run of hops with no NEW successful call is not progress (varying args defeat the repeat guard). |
| `PROGRESS_LEDGER_CALLS` | 12 | How many completed calls (names + args, NOT reasoning) ride back as a progress ledger, so the loop isn't amnesiac. |
| `MAX_TOOL_RESULT_MESSAGE_CHARS` | 32,000 | How much of a tool result re-enters context (fits typical files whole; distinct from the ~400-char display copy). |

### AA.2 Write tools vs repeat-exempt reads
- `_WRITE_TOOLS` = create/edit/update/write/apply_symbol_edit/delete/make_dir/
  apply_rename. A successful write invalidates the repeat counters (edit → check →
  edit → check is three meaningful checks, not a stall).
- `_REPEAT_EXEMPT_READS` = list_dir, read_file, search_files, read_artifact,
  git_inspect, document_symbols, find_references, blast_radius. Re-reading is
  orientation, not a stall — and aborting on a repeated read used to discard the
  productive writes beside it in the same turn.

### AA.3 Context compaction / folding
- Tool results ≥ ~6,000 chars are folded into artifact handles (the newest round
  is never compacted — it's what the model is writing from).
- Older rounds: tool results → head + tail + byte count; long assistant "said"
  text is elided. The progress ledger is names+args only (echoing reasoning made
  the model copy it back out, each thinking block a superset of the last).

### AA.4 Why the ledger is names, not reasoning
Observed live: four identical README reads in one turn, each preceded by the same
"The user is asking…" reasoning. Echoing the reasoning produced 252 → 1138 → 1272
→ 4320-char thinking blocks. Facts about what ran answer "what have I already
done?" without restating it as prose.

---

## Appendix AB — The executor run loop & recovery (`flow.py`)

### AB.1 `extend_from` (dynamic graph growth)
After a node completes, `extend_from(src_nid, result, registry)`:
1. **Add new nodes** from `result.successors`; build `label_to_id` (explicit
   `metadata.label` AND skill name) and `idx_to_id` (sibling position).
2. **Resolve inputs** (two-pass, Appendix Q.2): `n:<label>`, numeric `n:<i>`,
   `USER_QUERY`, `art:` pass-through, bare digit = sibling position; 0-based then
   `-1` shift if a self-loop appears.
3. **Structural parent edge** even for fan-out workers (`inputs=[]`) so topology
   and replay stay coherent.
4. **Static `internal_successors`** (e.g. coder → sandbox_executor) added with
   `inputs=[src_nid]`.
5. **Critic auto-insertion**: for a `critic: true` skill, every outgoing edge to a
   non-critic child is regated through a new `critic` node with
   `inputs=["USER_QUERY", src_nid]` (USER_QUERY so the critic judges against the
   real ask, not stale FAISS hits). Covers both new and pre-planned edges.

### AB.2 Skill sets that shape the graph
- `_RESEARCH_SKILLS` = retriever, researcher, browser, distiller, summariser.
- `_AUTHORING_SKILLS` = author, deck (must have research above them).
- `_CODE_PRODUCING_SKILLS` = coder (the only thing `sandbox_executor` can run).

### AB.3 Findings persistence
`_research_findings_text(out)` turns a researcher's whole deliverable (question +
findings + sources) into one readable document filed under "Web sources", so the
next question doesn't re-research the same topic. Measured: four researchers
produced 3.6k/7.3k/17.8k/19.1k chars that were previously unretrievable.

### AB.4 The wave loop
Planner first → loop { ready_nodes → run ≤ MAX_CONCURRENCY in parallel → mark →
extend_from } until none ready/running → formatter. `MAX_NODES` stops scheduling;
budgets inform the next call; `_stop_in_nodes` publishes Stop.

---

## Appendix AC — Authoring length & sectioning (`skills.py`)

### AC.1 `length_contract(query, skill)` — three modes
- **Explicit short cap** (`cap > floor`): "keep under ~N" — the user's limit.
- **No floor** (`floor <= 0`): impose nothing; "write as much as the material
  supports" (the old "and no more" made every brief ~700 words).
- **Floor derived**: "treat N as a FLOOR, not a ceiling; below ~0.75N is a failed
  document."
Shared in all modes: reach length with ANALYSIS (mechanisms, trade-offs, worked
examples, edge cases, numbers), sections carry the document, `render_document`
reports the delivered count, don't pad or invent.

### AC.2 Why section depth, not a document target
The measured cause of short documents was 140-word *sections* (five stubs + cover
= a thin 5–6 pages), not a missing document-level number.

### AC.3 Sectioned paths
- `_sectioned_findings` (researcher): write findings page-by-page from the pages
  actually fetched.
- `_sectioned_document_blocks` (author/deck): one call per section, then assemble.
- `_sectioned_final_answer` (formatter): one focused call per upstream result,
  then assemble (keyed models answer ~300 words regardless of cap).

---

## Appendix AD — Memory & the vector index

### AD.1 Architecture
Durable state, FAISS indexes, embed calls, and retrieval live in the **gateway**
(`memory/` + `memory_api.py`). The agent's `memory.py` is a thin client: the LLM
classifier prompt for ambiguous writes, a deterministic keyword extractor fallback,
and typed forwarding to `/v1/memory/*`.

### AD.2 Drawers
`SESSION_DRAWERS = ["policy", "profile", "fact", "playbook", "document",
"episode", "legacy"]` (working notes are run-scoped and excluded from session
recall). Every write declares its principal role (`remember`→agent,
`record_outcome`→runtime, `add_fact`→indexer); the gateway enforces the per-drawer
writer map fail-closed.

### AD.3 Failure contract
`read` is fail-soft (gateway down → `[]` + log; session start must never die).
Writes raise on transport failure so callers decide (`flow._safe_remember`
swallows + logs; MCP tools surface the error honestly).

### AD.4 `VectorIndex` (FAISS)
`faiss.IndexFlatIP` on L2-normalized vectors (= cosine). Parallel `ids` list maps
integer position → string id. Persists to `state/index.faiss` + `state/index_ids.json`.
Dimension fixed on first `add` (the gateway pins the embedding model). If FAISS is
missing, degrade to keyword search (never kill the agent).

### AD.5 Retrieval
Hybrid: dense (vectors) + BM25 (keyword) fused with RRF. Facts/preferences carry
embeddings; scratchpad skips. Supersede links a correction to the old value.

---

## Appendix AE — The gateway request path

1. **Auth** → parse `ChatRequest`.
2. **Route** (`router.py`): pick provider/model by capability + rate state;
   `agent_routing.yaml` soft pins; `route_for_model`.
3. **Call** `provider.chat(...)`. Providers: `BaseProvider` → `OpenAICompatProvider`
   (+ Groq/Cerebras/Nvidia/OpenRouter/GitHub/Kilo subclasses), `GeminiProvider`,
   `OllamaProvider`.
4. **Empty-completion guard**: 200 + 0 text + 0 tool_calls → retry same provider
   (up to 2×) → failover.
5. **Retry/failover** on 5xx/429 (`ProviderError(status, retryable)`).
6. **Record** via `log_call(...)` (redacts error/attempted; computes `output_tps`).
7. **Return** completion (+ `router_decision` if routed).

---

## Appendix AF — Computer-use engine loop

`ComputerUseSkill.run(goal)`:
1. **L0** if daemon down → gated shell.
2. **L1 extract** (AX/clipboard/file, 0 LLM) — try first.
3. **L2a deterministic** hotkeys (0 LLM).
4. **L2b a11y**: `get_window_state` → `filter_ax_markdown` → cheap LLM →
   `element_index` act.
5. **L3 vision**: screenshot + set-of-marks → `/v1/vision` → click `(x,y)`.
Scan-act-verify up to `max_turns` (12): scan before act (build element cache),
re-scan after every state-changing action. Recovery (rescan/escalate/abort) with
minimal carried state. Change-detection settle waits for the AX tree to change;
no-relaunch on a stale window; malformed/targetless click → re-ask.

---

## Appendix AG — Browser cascade

`BrowserSkill.run(NodeSpec)`:
1. **L1 extract** (trafilatura, no LLM).
2. **L2a deterministic** (if `metadata.selectors`).
3. **L2b `A11yDriver`** (`/v1/chat`, text-only).
4. **L3 `SetOfMarksDriver`** (`/v1/vision`, annotated screenshot).
Escalate when a layer is empty/insufficient; stop at first useful answer.
`gateway_blocked` (CAPTCHA/login wall) → `error_code` → recovery re-plans.
Knobs clamped from metadata (`max_steps_a11y`, `max_steps_vision`, `wall_clock_s`).
Screenshots/legends saved under `state/sessions/<sid>/browser` and surfaced as
`artifact_files`.

---

## Appendix AH — Channels (end to end)

- **Adaptor** (`BaseAdaptor`): `send`/`normalize`/`verify_webhook`/`required_keys`.
  `NotConfigured` (missing keys) vs `NotIntegrated` (scaffolded, disabled).
- **Envelope**: inbound → `ChannelMessage` (with `TrustLevel`) before policy/ledger.
- **Trust** (`state/pairing.json`): owner/paired/untrusted; owner pairing via
  `/v1/control/pair`, never chat.
- **Policy** (`policy/engine.py`): first-match-wins, ties deny, dry-run reports.
- **Poller** (telegram/gmail/slack): read → verify → trust-check → `_record_inbound`
  → a normal DAG chat turn.
- **Outbound**: action tool → `/v1/integrations/{service}/{op}` (credentials in the
  gateway).

---

## Appendix AI — Studio

- **Workspace** (`studio_workspace.py`): session roots (allow-list; empty if roots
  vanish — never the process default).
- **Review** (`studio_review.py`): baselines + content-fingerprinted hunks
  (Keep/Revert) + checkpoints.
- **Runs** (`studio_runs.py`): `state/runs/<bucket>/<run>.jsonl` + `index.json`;
  stop/list/replay.
- **Composer** (`studio_composer.py`): `@` mentions, `/` commands, commit messages.
- **Files** (`studio_files.py`): file ops/search-replace/git argv — planners only,
  never writes.
- **Browser** (`browser_session.py`): the session's persistent Chromium.
- Prompt `prompts/studio_coder.md` re-read on mtime change.

---

## Appendix AJ — Documents & artifacts

- `render_document(format, title, blocks, …)` → real bytes (reportlab/pptx/docx/
  openpyxl; LaTeX/Typst/markup) → content-addressed artifact.
- Artifact bytes in `state/artifacts` (inert) + `document_id` back to the
  chunked/embedded copy.
- Receipt + file list from the renderer.
- Long docs: sectioned blocks; figures via `_figures_context` + `_acquire_figures`.

---

## Appendix AK — Console frontend

- `src/api.ts` — typed client (per-launch token header, SSE chat).
- `src/App.tsx` + `react-router-dom` — one route per view.
- `src/views/*` — the pages (Appendix I).
- `src/lib/studioSession.ts` — Studio session state.
- State via React hooks; `@xyflow/react` for the DAG; Monaco editor; xterm
  terminal; three.js for parts; pdfjs for previews.

---

## Appendix AL — End-to-end trace (one research→email request)

```
1. Console POST /api/chat {query, conversation_id} + token.
2. agent_server → Executor.run on a worker thread; SSE opened.
3. planner node → LLM (gateway /v1/chat) → JSON graph (researcher×N + retriever
   → author → action → formatter). extend_from wires edges (labels/positions).
4. Wave 1: researchers + retriever run in parallel (≤8). Each researcher:
   tool loop (web_search/fetch_url/…), folds big results, files findings as a
   document. Empty completion? gateway retries 2× then fails over; agent repairs.
5. Wave 2: author (research above it) → render_document → PDF artifact.
6. Wave 3: action → load_tool_bundle("gmail") → send_email(document=art:…) →
   gateway /v1/integrations/gmail/send (credential in gateway).
7. Wave 4: formatter → final answer.
8. done frame → console shows the answer + the artifact download.
   Any failed node → classify_failure → recovery splice; run marked incomplete
   honestly if a hole remains.
```

---

*End of handbook.*