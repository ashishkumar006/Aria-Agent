# Aria — Technical Guide

> A complete, module-by-module reference for the Aria self-hosted agent: the
> gateway, the agent core (DAG orchestrator), the skills, the tool and channel
> layers, the sandbox, computer use, the Studio coding workspace, the reliability
> model, security, cost, and verification.
>
> For a narrative overview start with [`README.md`](../README.md). For the
> original architecture notes see [`docs/ARCHITECTURE.md`](ARCHITECTURE.md). This
> document is the deep technical reference.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Repository layout](#2-repository-layout)
3. [The Gateway (V9)](#3-the-gateway-v9)
4. [The Agent Core (S9)](#4-the-agent-core-s9)
5. [The Executor and the DAG](#5-the-executor-and-the-dag)
6. [Skills catalogue](#6-skills-catalogue)
7. [Tool layer](#7-tool-layer)
8. [Channels and integrations](#8-channels-and-integrations)
9. [The Sandbox](#9-the-sandbox)
10. [Computer use](#10-computer-use)
11. [The Browser skill](#11-the-browser-skill)
12. [Studio (the coding workspace)](#12-studio-the-coding-workspace)
13. [Memory and retrieval](#13-memory-and-retrieval)
14. [Document generation](#14-document-generation)
15. [State on disk](#15-state-on-disk)
16. [Reliability and failure model](#16-reliability-and-failure-model)
17. [Security and privacy](#17-security-and-privacy)
18. [Cost model](#18-cost-model)
19. [Verification](#19-verification)
20. [Known issues and hard-won fixes](#20-known-issues-and-hard-won-fixes)
21. [Glossary](#21-glossary)

---

## 1. System overview

Aria is three cooperating processes plus an optional desktop shell:

| Process | Port | Role |
|---|---|---|
| **Agent server** (`S9SharedCode/code/agent_server.py`) | `8500` | Orchestration, the DAG executor, the web API + SSE, serves the console. |
| **Gateway** (`llm_gatewayV9/main.py`) | `8109` | Model providers, channel credentials, document store, memory plane, document generation, policy. |
| **Console** (`S9SharedCode/code/console-frontend/`) | (served by `:8500`) | React SPA; optional Electron shell in `agent-desktop/`. |

**Trust model.** The agent owns orchestration and the UI; the gateway owns
secrets and external services. The agent process never holds a channel token —
it calls the gateway at `/v1/integrations/{service}/{op}`, where credentials
live and policy is applied. A channel is "configured" only when the gateway
reports it; the agent claims nothing it does not have.

**Request shape.** A chat turn is not one model call. The planner decomposes the
request into a **DAG of skill-nodes**; the executor runs ready nodes in bounded
parallel waves, splices in recovery nodes on failure, and a terminal **formatter**
produces the answer. The graph and per-node results are persisted, so a run can
be resumed or woken later.

```
Console ──HTTP/SSE──▶ Agent :8500 ──/v1/*──▶ Gateway :8109 ──▶ Providers / Channels
   (React)            (flow.py DAG)              (keys, memory, docgen, policy)
```

---

## 2. Repository layout

```
project3/
├── S9SharedCode/code/            # The agent core
│   ├── agent_server.py           # FastAPI server (:8500) + SSE chat + routes
│   ├── flow.py                   # Executor().run(query) — the DAG orchestrator
│   ├── skills.py                 # Skill registry, _TOOL_CATALOG, dispatch, JSON repair
│   ├── recovery.py               # Failure classification + recovery planning
│   ├── sandbox.py                # Resource/process boundary (run, run_python)
│   ├── mcp_server.py             # MCP tool surface (stdio)
│   ├── mcp_runner.py             # Spawns MCP sessions, runs the tool loop
│   ├── gateway.py                # V9 client wrapper (LLM / embed / ensure_gateway)
│   ├── action.py                 # Action-skill helpers (confirmations)
│   ├── channel_chat.py           # Inbound channel → DAG mapping
│   ├── computer_use/             # Layered desktop agent (see §10)
│   ├── browser/                  # Browser skill (4-layer cascade, §11)
│   ├── studio_*.py               # Studio workspace/review/runs/composer/files
│   ├── prompts/                  # Per-skill system prompts + planner.md
│   ├── agent_config.yaml         # Skill catalogue (prompt, tools, temp, tokens)
│   ├── flags.py                  # Runtime kill-switches (ui.code_ide, browser.*)
│   ├── models/                   # Kokoro ONNX TTS assets
│   ├── state/                    # Sessions, memory, vector index, runs (runtime)
│   ├── console-frontend/         # React console (Vite/TS)
│   └── tests/                    # Pytest suite + TEST.md checklist
├── llm_gatewayV9/                # The gateway
│   ├── main.py                   # FastAPI app (:8109), /v1/chat, empty-completion retry
│   ├── providers.py              # Provider adapters (Gemini, Kilo, OpenAI, …)
│   ├── router.py                 # Model routing + capability eval
│   ├── agent_routing.yaml        # Per-agent provider pins / preferences
│   ├── cache.py                  # Response cache
│   ├── pricing.py / budget.py    # Pricing + spend caps
│   ├── embedders.py              # Embedding providers
│   ├── memory_api.py             # Memory plane (drawers)
│   ├── docgen*.py                # Document generation (PDF/PPTX/DOCX/XLSX/LaTeX/Typst)
│   ├── channels_api.py           # /v1/integrations + /v1/channels dispatch
│   ├── integrations/             # Per-service functions (send/query)
│   ├── adaptors/                 # Per-channel adaptors (send/normalize/verify)
│   ├── policy/                   # Policy engine
│   ├── key_info.py               # Key docs (names, purpose, where to get them)
│   ├── *_poller.py               # Inbound pollers (telegram/gmail/slack)
│   ├── secret_vault.py           # Credential storage
│   └── gateway_v8.db             # SQLite: calls, memory, documents, cost
├── agent-desktop/               # Optional Electron shell (build_desktop.ps1)
├── docs/                        # Deep-dive docs (this file lives here)
├── start_aria.ps1 / stop_aria.ps1
└── AGENTS.md                    # Working rules for agents in this repo
```

---

## 3. The Gateway (V9)

A standalone FastAPI service on **`:8109`**. It is the single place that holds
keys, talks to providers, and owns external state. The agent always goes through
it; it never calls a provider API directly.

### 3.1 Responsibilities
- **Model routing & failover** — pick a provider/model per call, fail over on
  error/rate-limit, pin a node's hops to one provider for prompt-cache reuse.
- **Credentials** — provider keys and channel tokens, held in `secret_vault.py`
  / `.env`, never exposed to the agent.
- **Memory plane** — durable facts/preferences (drawers), recall + record.
- **Document store & generation** — content-addressed artifacts; render real
  PDF/PPTX/DOCX/XLSX bytes.
- **Channels** — outbound send + inbound normalize, policy-gated.
- **Policy** — allow/deny rules, approvals, dry-run.
- **Cost** — pricing, per-call/agent/session caps, ledger.

### 3.2 Key files
| File | Role |
|---|---|
| `main.py` | App, `/v1/chat`, empty-completion retry loop, failover ladder. |
| `providers.py` | Provider adapters; `ProviderError` with `status`/`retryable`. |
| `router.py` | Capability evaluation + model selection; tpm limits (e.g. gemini 250k). |
| `agent_routing.yaml` | Per-agent provider preferences/pins (soft; 502 failover). |
| `cache.py` | Response cache keyed by request shape. |
| `pricing.py`, `budget.py` | $/token tables; per-call/agent/session caps. |
| `embedders.py` | Embedding backends for the vector index. |
| `memory_api.py` | `/v1/memory/*` — search, record_outcome, drawers, supersede. |
| `docgen.py`, `docgen_latex.py`, `docgen_typst.py`, `docgen_markup.py` | Renderers. |
| `channels_api.py` | `/v1/integrations/{service}/{op}`, `/v1/channels/{name}/send`. |
| `integrations/` | `gmail.py`, `calendar.py`, `github.py`, `notion.py`, `telegram.py`, `slack.py`, `discord.py`, `zoho_mail.py`, `ntfy.py`, `bluesky.py`, `mastodon.py`, `google_chat.py`, … |
| `adaptors/` | Per-channel `send()/normalize()/verify_webhook()/required_keys()`. |
| `policy/` | Policy engine + rules. |
| `key_info.py` | Human docs for each key (never values). |
| `*_poller.py` | Inbound long-poll/read loops feeding `channels_api._record_inbound`. |
| `secret_vault.py`, `gateway_auth.py`, `user_store.py` | Secrets, auth, users. |
| `oauth_flows.py`, `*_oauth_setup.py` | One-time OAuth consent (Gmail/Calendar/Zoho). |

### 3.3 `/v1/chat` lifecycle
1. Authenticate, load `ChatRequest` (messages, model, tools, response_format,
   reasoning, images, cache_system, agent).
2. `router` selects a provider/model (honouring `agent_routing.yaml` soft pins).
3. Call `provider.chat(...)`. On a retryable error (5xx, 429, or an **empty
   completion**) retry/failover (see §16.1).
4. Record the call in `gateway_v8.db` (`calls` table) with tokens, cost, status.
5. Return the completion (text + tool_calls) to the agent.

### 3.4 Notable endpoints (observed)
`/v1/chat`, `/v1/vision`, `/v1/docgen`, `/v1/documents` (+ `/figures`),
`/v1/memory/search`, `/v1/memory/record_outcome`, `/v1/memory` (CRUD),
`/v1/integrations/{service}/{op}`, `/v1/channels`, `/v1/config/keys`,
`/v1/status`, `/v1/routers`, `/v1/embedders`, `/v1/calls`, `/v1/spend`,
`/v1/policy`, `/v1/cost/by_agent`, `/v1/control/notify-target`, `/health`.

---

## 4. The Agent Core (S9)

### 4.1 `agent_server.py`
FastAPI app on **`:8500`**. Responsibilities:
- Serve the built console (`spa_built`), the per-launch token, and the JSON API.
- `/api/chat` — stream a DAG run as **SSE** frames: `log`, `done`, `error`.
- Session/graph/node routes: `/api/sessions/{sid}/graph?light=true`,
  `/api/sessions/{sid}/nodes/{nid}`, `/api/feedback`, `/api/runs/summary`.
- Studio, Apps, Computer, Scheduler, Memory, Documents, Ledger routes.
- `/api/health` — reports `gateway_up` (must be true).

Runs execute via `Executor().run(query)` driven inside `asyncio.run()` on a
worker thread; frames are `data: {json}\n\n`.

### 4.2 `gateway.py`
Thin client wrapper: `LLM().chat(messages=…, response_format=…, agent=…,
model=…, tools=…, images=…)`, `embed(...)`, and `ensure_gateway()` (auto-starts
the gateway if it is not listening).

### 4.3 `prompts/` and `agent_config.yaml`
Each skill = one YAML entry + one prompt markdown (no Python class per skill).
The YAML declares `prompt`, `description`, `tools_allowed`, `temperature`,
`max_tokens`, optional `provider_pin`, `internal_successors`, `critic`,
`dynamic_channels`, `sectioned_findings`, `sectioned`. The prompt is rendered
per node with the resolved inputs, memory hits, the per-node `question`, and the
granted tools.

---

## 5. The Executor and the DAG

`flow.py :: Executor().run(query)` is the async orchestrator.

### 5.1 Scheduling
- The planner emits an initial graph plus recovery subgraphs.
- The executor runs **ready** nodes in bounded **waves**; a node runs when its
  predecessors are complete.
- **Mechanics limits (not plan-quality limits):**
  - `MAX_NODES = 400` — a runaway planner/recovery loop cannot schedule forever.
  - `MAX_CONCURRENCY = 8` — how many calls run in one wave. Unbounded waves
    `429` together on the free tier and every sibling fails; deferred nodes stay
    `pending` and run on the next wave, so a 60-facet plan still runs all 60.
- There is **no fanout cap** on plan quality: an earlier `MAX_FANOUT=4` threw
  away a 6-facet plan (4 facets → 17,782 chars / 2,367-word doc; 3 → 13,421 /
  310-word stub). The facet count is the research budget and is the planner's call.

### 5.2 Soft time budget
`NODE_BUDGET_S` tells the **next** invocation of a skill how much of its
allowance it has already spent, so the model synthesises instead of starting a
fourth fetch. It never cancels an in-flight call (asyncio cannot interrupt an
LLM call safely).

| Skill | Budget |
|---|---|
| researcher | 150s |
| browser | 120s |
| action | 60s |
| planner | 30s |
| formatter | 240s |
| default | 90s |

`BUDGET_WARN_RATIO = 0.7` — the note is injected once a node crosses 70% of budget.

### 5.3 Cooperative Stop
`_stop_in_nodes()` publishes a stop predicate into nodes so a long author (160
section calls) or researcher (30 tool hops) notices Stop **between its own tool
calls**, not only at the batch boundary (which only arrives after all of them).
This stops billing after the console says "stopped" and releases the idempotency
claim so a resubmit is not rejected as "an identical run is already in progress".

### 5.4 Recovery and splicing
- `recovery.py :: classify_failure` tags a failure **transient** (retry) or
  **environmental/deterministic** (skip/replan) — e.g. a sandbox wall-clock
  timeout is deterministic and must not loop.
- `plan_recovery` asks the planner for a replacement subgraph scoped to the
  missing piece (`_annotate_failure_report`).
- `_splice_recovery` rewires the recovery nodes **in place of** the failed
  node(s) (not beside them), marks the original `skipped`, and rewires waiting
  consumers. `_replacement_answered` confirms the recovery actually filled the
  hole.
- `_consumers_have_other_evidence` prevents a chain from losing its only
  evidence when a minority of parallel researchers fail (two failed of three
  queued two recovery planners; survivors are a majority → no replan).

### 5.5 Graph-shape guards
- `ensure_sandbox_has_code` — drops a `sandbox_executor` no `coder` feeds
  (the "25 × 4" orphan: planner → sandbox_executor → formatter re-planned twice
  into the same impossible shape, 3 planner nodes, 59.4s).
- `ensure_authoring_research` — guarantees a retriever + researcher fan-out runs
  **before** an author node, so a document is written from findings.

---

## 6. Skills catalogue

A skill is one dispatch role. Full config in `agent_config.yaml`. Grouped:

**Cognitive / control**
| Skill | Tools | Temp | max_tokens | Role |
|---|---|---|---|---|
| `planner` | — | 0.4 | 65536 | Decompose into the DAG + recovery subgraphs. |
| `critic` | read_artifact | 0.0 | 1500 | Pass/fail an upstream node with rationale. |
| `distiller` | read_artifact | 0.1 | 65536 | Structured extraction (auto-inserts a Critic). |
| `summariser` | read_artifact | 0.3 | 65536 | Condense. |
| `formatter` | web_search, fetch_url, search_knowledge, read_document | — | 65536 | Final answer; `sectioned: true`. |

**Research**
| `retriever` | search_knowledge, read_document, recall_preferences, remember_preference | 0.2 | 65536 | Indexed knowledge + docs. |
| `researcher` | web_search, fetch_url, arxiv_search, wikipedia_search, openalex_search, news_search, fetch_pdf, extract_tables, wayback_fetch, remember_preference, recall_preferences, read_artifact, computer_action, search_images, fetch_image | 0.7 | 65536 | Live multi-source research; `sectioned_findings: true`. |

**Authoring**
| `author` | render_document, read_artifact, web_search, search_images, fetch_image | 0.35 | 65536 | Render real documents. |
| `deck` | render_document, read_artifact, web_search, search_images, fetch_image | 0.4 | 65536 | Render slide decks. |

**Coding & interaction**
| `coder` | read_file, list_dir, search_files, find_symbol, document_symbols, find_references, blast_radius, preview_edit, apply_symbol_edit, create_file, update_file, edit_file, delete_file, run_checks, git_inspect, index_document | 0.2 | 65536 | Writes code; `internal_successors: [sandbox_executor]`. |
| `studio_coder` | studio_coder | 0.2 | 8192 | Delegate to the full Studio coding agent on real roots. |
| `sandbox_executor` | — | 0.0 | 4000 | Runs the coder's code in the sandbox. |
| `browser` | (own cascade) | 0.0 | 1024 | 4-layer web cascade. |
| `computer` | computer_action | 0.1 | 1200 | Gated desktop control. |
| `vision_file` | — | 0.2 | 1200 | Local image understanding via V9 vision. |

**Action**
| `action` | (large catalogue; `dynamic_channels: true`) | 0.2 | 1500 | Real-world side effects; progressive disclosure. |

**Specialist builders**
| `part_analyst` | parts_list, part_get, part_analyse, part_set_view | 0.1 | 6000 | Inspect a 3D part (read-only). |
| `part_engineer` | parts_list, part_get, part_analyse, part_repair, part_optimise, part_start_loop, part_job_status, part_cancel_job, part_export, part_set_view | 0.2 | 8000 | Repair/optimise a mesh. |
| `part_blender` | blender_status, blender_catalog, blender_part_op, blender_part_render, blender_rna_get, blender_rna_set, blender_python | 0.2 | 9000 | Real Blender ops on a part. |
| `app_worker` | app_list, app_get, app_build, app_action | 0.2 | 8000 | Build/change A2UI apps. |
| `app_builder` | — | 0.0 | 200 | Deterministic single-node Apps build/action. |

---

## 7. Tool layer

### 7.1 Resolution flow
```
agent_config.yaml (tools_allowed)         # names
        │
        ▼
skills.py :: tool_payload()  ──look up──▶  _TOOL_CATALOG   # JSON schemas
        │ payload (names absent from catalog are dropped loudly)
        ▼
mcp_runner.run_with_tools()  ──spawn──▶  mcp_server.py (stdio, 1 session/skill)
        │ model tool_call
        ▼
mcp_server tool fn  ──result──▶  fed back to model until final text
```
`_TOOL_CATALOG` is the single source of truth for what the model may call; a
name not in the catalog warns and is skipped.

### 7.2 Progressive disclosure (action)
`effective_tools()` returns the **core** tools (non-channel) plus
`load_tool_bundle`. The node calls `load_tool_bundle("<channel>")` mid-run to
append that channel's schemas (`build_tool_bundles()`). `render_grant_header()`
tells the model exactly which tools are callable this run; every other tool in
the catalogue returns an error if called.

### 7.3 Channel → tools map (`CHANNEL_TOOLS`)
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

`channels_for_goal()` maps keywords → channels (word-boundary matched so "repo"
does not fire on "report"); explicit `metadata.channels` wins.

### 7.4 `_JSON_CONTRACT_SKILLS`
`{planner, researcher, distiller, retriever, critic, summariser, formatter,
coder, action}` — these must emit JSON; unusable output triggers repair (§16.2).

---

## 8. Channels and integrations

A channel is added in one place per concern, so a credential lives in exactly
one process:

| Layer | File | Role |
|---|---|---|
| Adaptor | `llm_gatewayV9/adaptors/<name>/` | `send()`, `normalize()`, `verify_webhook()`, `required_keys()`. |
| Integration | `llm_gatewayV9/integrations/<name>.py` | Functions the tools call; return `{ok: …}`. |
| Dispatch | `channels_api.py` | Routes `/v1/integrations/{service}/{op}` and `/v1/channels/{name}/send`. |
| MCP tool | `mcp_server.py` | Thin forwarder (e.g. `telegram_send_message`). |
| Bundle | `skills.py (CHANNEL_TOOLS)` | Grouped for `load_tool_bundle`. |
| Grant | `agent_config.yaml (action.tools_allowed)` | What the action skill may call. |
| Docs | `key_info.py` | Names/purpose/where to get the key (never values). |

**Adaptors present:** bluesky, discord, gmail, google_chat, imap, line,
local_mic, mastodon, matrix, ntfy, signal, slack, teams, telegram, twilio_sms,
twilio_voice, webhook, webui, whatsapp_meta, whatsapp_twilio, zoho_cliq,
zoho_mail.

**Cost policy:** free protocol first; pay only where there is no free option
(Twilio SMS ~$0.008/msg is the main unavoidable cost). The six free channels
wired this round: **ntfy, Zoho Cliq, Zoho Mail, Bluesky, Mastodon, Google Chat**.

**Two-way chat:** inbound pollers (`telegram_poller.py`, `gmail_poller.py`,
`slack_poller.py`) verify + trust-check the sender and turn the message into a
normal chat turn (`channel_chat.py`, `run_kind="chat"` — the full DAG runs).

---

## 9. The Sandbox

`sandbox.py` is the resource/process boundary for **all** arbitrary code the
agent runs: the project's own checks (`run_checks`), a PowerShell command
(`run_command`), and coder Python (`run_python`, used by `sandbox_executor`).

### 9.1 Guarantees
- **Hard wall-clock timeout** that kills the whole **process tree** (PowerShell
  starts native children; killing only the shell leaves `pytest`/`npm` running).
- **Windows Job Object** with `KILL_ON_CLOSE`, `LIMIT_ACTIVE_PROCESS`,
  `LIMIT_PROCESS_MEMORY`, `LIMIT_JOB_MEMORY` (defaults: 24 processes, 1024 MB
  per process / 2× job) — a fork bomb cannot take the machine down.
- **POSIX**: new session/process group, killed with SIGKILL.
- **Bounded output buffer** that keeps draining pipes (a gigabyte of output
  cannot deadlock or OOM the server).
- **Environment scrub** (`clean_env`) removes credential-shaped names
  (`TOKEN|SECRET|PASSWORD|API_KEY|…`) and `ARIA_*` before the child starts.

### 9.2 `run_python` contract (sandbox_executor)
- Writes `main.py` into a fresh temp dir, runs it with a whitelisted env
  (`PATH, HOME, LANG, LC_ALL, LC_CTYPE`), `PYTHONIOENCODING=utf-8`.
- `PYTHON_TIMEOUT_S = 30`, stdout/stderr caps 1,000,000 each.
- Returns `{exit_code, stdout, stderr, files_written, timed_out, timeout_s, cwd}`;
  the temp dir is deleted before returning (`files_written` = names + sizes).
- **What it is NOT:** a filesystem or network jail. The command guard refuses
  common destructive/credential-read shapes (heuristic), and network remains
  possible — that boundary is the approval gate (and, for hard isolation, a
  container/AppContainer backend).

`skills.py` `sandbox_executor` bypasses the gateway: it lifts `code` from the
upstream coder node and calls `run_python` directly. A deterministic timeout is
tagged `[deterministic-timeout]` so recovery skips rather than loops.

---

## 10. Computer use

The `computer_use/` package drives a real desktop app via **cua-driver** with a
perception-driven cascade. Disabled by default (`COMPUTER_USE_ENABLED`), dry-run
by default; sensitive actions require in-UI approval.

### 10.1 Modules
| File | Responsibility |
|---|---|
| `daemon.py` | cua-driver lifecycle, JSON-RPC over a named pipe (`\\.\pipe\cua-driver`), `capabilities()`. |
| `safety/gates.py`, `permissions.py` | Enabled / mode (dry-run\|live) / allow-deny / approvals / audit. |
| `layers/` | Goal decomposition, perception, sequencing (scan-act-verify), recovery, vision. |
| `prompts/__init__.py` | Judge (a11y) + vision prompts. |
| `engine.py` | `ComputerUseSkill` — the L0→L3 cascade + scan-act-verify loop. |
| `__init__.py` | Exports + backward-compatible `ComputerUse` shim. |

### 10.2 Layer cascade
- **L0 gated-shell** — `run_command` / `read_file` / `write_file` / `open_app` behind gates (fallback when the daemon is unavailable).
- **L1 extract** — read AX tree text / clipboard / file directly (0 LLM).
- **L2a deterministic** — known hotkey sequences (0 LLM).
- **L2b a11y** — `get_window_state` markdown + cheap LLM → `element_index` act.
- **L3 vision** — screenshot + set-of-marks → `/v1/vision` → click `(x,y)`.

The engine runs **scan-act-verify** up to `max_turns` (default 12) with
recovery (rescan / escalate / abort) and vision fallback when AX is insufficient.
Judge/vision default to a fast model, overridable via
`COMPUTER_USE_JUDGE_MODEL` / `COMPUTER_USE_VISION_MODEL` / `COMPUTER_USE_REASONING`.
Harness features: `open_app`, change-detection settle (wait for the AX tree to
change), no relaunch on a stale window (`allow_launch=False`), action history to
the judge, and a re-ask on a malformed/targetless click.

### 10.3 Safety
Protected paths (`C:\Windows`, `C:\Program Files`, `~/.ssh`, `~/.aws`) are never
writable. Sensitive commands (`kill_app`, `rmdir`, `format`, `diskpart`) create a
pending approval; the UI approval panel is gated behind `?cu=1`.

---

## 11. The Browser skill

`browser/` owns its own cascade (it does not use the standard LLM dispatch) and
routes each layer's gateway call internally:

1. **Extract** — HTTP HTML → readable text.
2. **Deterministic** — known interactions.
3. **a11y** — accessibility-tree text + cheap LLM.
4. **Vision** — screenshot + set-of-marks → `/v1/vision` → click.

Switches live in `flags.py` (`browser.tools`, `browser.headed`), not
`agent_config.yaml`, so the operator's kill-switch works even with a config
service unreachable. Output surfaces the chosen layer as `output.path`.

---

## 12. Studio (the coding workspace)

A full coding agent + workspace, separate from the DAG's sandbox coder. One
concern per module:

| Module | Concern |
|---|---|
| `studio_workspace.py` | Sessions + declared roots (allow-list; empty if roots vanish). |
| `studio_review.py` | Baselines, content-fingerprinted hunks, checkpoints. |
| `studio_runs.py` | Run registry + JSONL transcripts + replay (`state/runs/<bucket>/<run>.jsonl`). |
| `studio_composer.py` | `@` mentions, `/` commands, commit messages. |
| `studio_files.py` | File ops, search/replace plans, git argv — **planners only, never writes**. |
| `browser_session.py` | The session's persistent Chromium. |

- The coder reads before it writes (list/read/search/symbol), makes targeted
  edits, runs the project's own checks, iterates until they pass.
- Every change is a reviewable hunk (Keep/Revert) inside a restorable checkpoint.
- The system prompt is `prompts/studio_coder.md`, re-read on mtime change (edits
  apply next message, no restart). `prompts/coder.md` is the **different** DAG
  Coder skill; Studio does not use it.
- `tests/test_studio_prompt_truth.py` fails if the Studio prompt names a tool the
  payload does not carry.
- Default code workspace is `state/code_workspace` (empty scratch); with a
  session bound it is that session's roots — never the process default.

---

## 13. Memory and retrieval

- **Drawers** scope recollection: working (per-run), episodes, durable facts,
  documents. Recall is scoped to the right drawer rather than dumping all.
- **Hybrid retrieval**: dense vectors (semantic) + BM25 (exact terms), fused with
  **Reciprocal Rank Fusion (RRF)** — "payment terms" matches by meaning while
  "Article 14(b)" matches the clause. The same retrieval backs agent memory and
  the Documents section.
- Memory is read once at session start and shared into every skill's prompt for
  that run; memory (facts about you) and conversation history (what was said) are
  kept distinct.
- `remember_preference` / `recall_preferences` let a mid-run correction
  ("always give the publication date") be written and honoured.
- Each chat turn is a fresh graph (no stale replay) but the session keeps a
  stable identity so memory/cost/history accumulate.

---

## 14. Document generation

The authoring pipeline renders **real bytes**, not descriptions:

1. The model produces a structured **spec** (sections / slides / sheets) as data.
2. `render_document` renders actual **PDF / PPTX / DOCX / XLSX** via the gateway
   (`reportlab` / `python-pptx` / `python-docx` / `openpyxl`; LaTeX/Typst/markup
   paths also exist) — tables, images, native editable charts (vector in PDF).
3. Bytes are stored **content-addressed** (`art:` handles / `doc-` ids).
4. The receipt and file list come from the **renderer**, never the model's claim;
   only an id the store can resolve is ever shown or linked.

A long document is written **section-by-section** (`_sectioned_document_blocks`)
and the formatter assembles a multi-section final answer
(`_sectioned_final_answer`); a single-call document fetches its own figures when
the request asks for pictures. `length_contract()` gives writers a concrete
length target computed from the request (a 300-page request otherwise ships 16
words).

---

## 15. State on disk

`S9SharedCode/code/state/` (runtime):
- `sessions/` — conversation + graph + per-node results.
- `memory/` — drawers, vector index.
- `runs/<bucket>/<run>.jsonl` + `index.json` — Studio run transcripts.
- `code_workspace/` — default empty scratch.
- `agent.token` — the per-launch console token.

Gateway `llm_gatewayV9/` (runtime):
- `gateway_v8.db` — SQLite: `calls` (tokens/cost/status/error), memory,
  documents, cost ledger.
- `documents/`, `memory/`, `parts/`, `state/`.

---

## 16. Reliability and failure model

### 16.1 Empty completions (HTTP 200, 0 text, 0 tool calls)
The gateway treats an empty completion as a **failed** call, not a success:
`main.py` retries the **same provider twice** (`empty_retries >= 2`) then fails
over to the next provider. Only when the whole free pool is momentarily empty
does an empty reach the agent. (Free Gemini ~15 rpm makes this common under
bursts.)

### 16.2 Unusable / mismatched JSON
Every `_JSON_CONTRACT_SKILLS` node that returns empty or malformed JSON gets
**one corrective call** (`_repair_json_reply`) with its own schema quoted back —
the `not reply.get("error")` gate was removed so an empty/error reply is
repaired, not skipped. A node that still cannot produce usable output is marked
failed.

### 16.3 Re-plan on failure
A failed node triggers `plan_recovery` + `_splice_recovery` (§5.4): the recovery
subgraph replaces the failed node, waiting consumers rewire, and the run
continues. Deterministic failures (e.g. a sandbox timeout) are classified
environmental and skipped, not retried forever.

### 16.4 Critic loop
A `critic: true` skill (distiller) auto-inserts a Critic on every outgoing edge;
a `fail` verdict with a specific rationale drives a targeted recovery.

### 16.5 Budgets, Stop, idempotency
Soft per-skill time budgets (§5.2), cooperative Stop inside nodes (§5.3), and a
per-session idempotency claim released on Stop so a resubmit is not rejected.

---

## 17. Security and privacy

- **Loopback + per-launch token**; cross-origin writes rejected.
- **Credentials live in the gateway**, never the agent; the agent's logs contain
  no token.
- **File tools confined** to a session's declared roots; escaping paths refused;
  an unreadable workspace is empty, not inherited.
- **Commands/writes approval-gated**, every attempt audit-logged.
- **Code execution resource-bounded** (§9) with a credential-scrubbed env.
- **Secrets never committed**; only `.env.example` templates are tracked
  (`.env` reads are additionally blocked by tooling).
- **Fail-closed on trust**: an unpaired sender gets no agent turn.

---

## 18. Cost model

- **Free-first**: prefer free/cheap backends; automatic failover; a cross-vendor
  backstop.
- **Caps** per call, per agent, per session (`budget.py`), recorded in the ledger
  (`/v1/spend`, `/v1/cost/by_agent`).
- **Progressive disclosure** keeps the action node's tool schema small (a
  two-service request pays for two channels, not twenty).
- **Prompt-cache reuse** via per-node provider pinning.

---

## 19. Verification

Run after any change (the exact commands matter — the shims resolve the wrong
interpreter otherwise):

```
# Agent tests (from S9SharedCode/code — NOT bare `uv run pytest`)
cd S9SharedCode/code
uv run python -m pytest tests/ -q          # full suite ~5–6 min

# Gateway tests (from llm_gatewayV9 — NOT `tests/`; most suites live outside it)
cd llm_gatewayV9
uv run python -m pytest -q                  # ~1.5 min

# Frontend (from console-frontend)
cd S9SharedCode/code/console-frontend
npx tsc --noEmit
npm run build

# Live health
curl -s localhost:8500/api/health           # gateway_up must be true
curl -s localhost:8109/health
```

`S9SharedCode/code/TEST.md` is the Studio verification checklist. Restart both
services to pick up backend changes; verify with `/api/health` (agent) and
`/health` (gateway) — never assume a restart happened.

---

## 20. Known issues and hard-won fixes

These are real defects found by running a 12-task DAG battery across every skill
and channel (all except computer use), with the fix direction for each:

1. **Coder → sandbox key mismatch.** `sandbox_executor` reads only
   `output["code"]`; the coder sometimes returns its script under
   `script_content` (valid JSON, node "complete") → "no code in upstream coder
   output", code silently dropped. **Fix:** normalize the coder's parsed output
   (`script_content`/`script`/`content` → `code`) and let the sandbox accept the
   synonym; never treat a recoverable shape as a hard failure.
2. **Vision on an artifact handle.** `vision_file` received a node reference
   (`"n:2"`) instead of a file path → `unsupported image type ''`. **Fix:**
   resolve `n:`/`art:` handles to the underlying file before the type check.
3. **Documented vs reachable.** The action prompt named `list_channels` /
   `schedule_wake` that were not in the tool grant — prose described hands the
   node lacked. **Fix:** `tests/test_prompt_truth.py` enforces grant direction.
4. **Orphan sandbox executor.** `planner → sandbox_executor → formatter` with no
   coder (the "25 × 4" case) re-planned twice into the same impossible shape
   (3 planner nodes, 59.4s). **Fix:** `ensure_sandbox_has_code` drops an executor
   no coder feeds.

Earlier fixes now in the tree: empty-completion retry 1→2 (gateway);
repair-on-empty for all JSON skills (`skills.py`); computer-use `open_app` +
change-detection settle + no-relaunch + action history + malformed-click re-ask;
the `if/then` schema reverted (Gemini strict-mode 400).

---

## 21. Glossary

| Term | Meaning |
|---|---|
| **DAG** | Directed acyclic graph of skill-nodes the planner emits for a request. |
| **Node / skill** | One unit of work (researcher, author, coder, action, …). |
| **Planner** | The skill that decomposes a request into the graph + recovery subgraphs. |
| **Formatter** | Terminal node that writes the final answer. |
| **Wave** | A bounded-parallel batch of ready nodes (`MAX_CONCURRENCY = 8`). |
| **Recovery / splice** | Re-plan that replaces a failed node and rewires consumers. |
| **Critic** | Auto-inserted pass/fail reviewer on a `critic: true` skill's edges. |
| **Facet** | One research angle; the facet count is the research budget. |
| **Drawer** | A scope of memory (working / episodes / facts / documents). |
| **RRF** | Reciprocal Rank Fusion — fuses dense + sparse rankings. |
| **Artifact** | A content-addressed rendered file (`art:` / `doc-` id). |
| **Progressive disclosure** | Action node loads a channel's tools only when needed. |
| **Grant** | The exact tool list a skill may call this run. |
| **Sandbox** | The resource/process boundary for arbitrary code (§9). |
| **L0–L3** | Computer-use cascade: gated-shell → extract → a11y → vision. |
| **Studio** | The full coding workspace (distinct from the DAG sandbox coder). |

---

*Aria runs on your machine. This document is the technical reference; the
module-by-module design rationale lives alongside the code and in
`docs/ARCHITECTURE.md`.*