# Aria

Aria is a self-hosted AI agent — an AI gateway, an agent server with a DAG orchestrator, and a web console. It runs on your machine, uses your own model keys (or runs fully local), and is built to carry out real work across the tools you already use, not just hold a conversation.

This document explains, in full, what Aria is, what it can do, how the pieces fit together, how to run it, how to verify it, and where its limits are. The module-by-module engineering detail lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Table of contents

1. [Overview](#overview)
2. [What Aria can do](#what-aria-can-do)
   - [Conversation and memory](#conversation-and-memory)
   - [Documents and retrieval](#documents-and-retrieval)
   - [Research](#research)
   - [Authoring (documents)](#authoring-documents)
   - [Studio (the coding workspace)](#studio-the-coding-workspace)
   - [Channels and integrations](#channels-and-integrations)
   - [Two-way channel chat](#two-way-channel-chat)
   - [Scheduling and self-continuation](#scheduling-and-self-continuation)
   - [Computer use](#computer-use)
   - [Model Lab](#model-lab)
   - [3D parts](#3d-parts)
3. [How it works](#how-it-works)
   - [The agent loop](#the-agent-loop)
   - [Tools and the gateway](#tools-and-the-gateway)
   - [Models and routing](#models-and-routing)
   - [Reliability: empty completions and recovery](#reliability-empty-completions-and-recovery)
4. [The agents](#the-agents)
5. [The console](#the-console)
6. [Running Aria](#running-aria)
7. [Verifying Aria](#verifying-aria)
8. [Security and privacy](#security-and-privacy)
9. [Design principles](#design-principles)
10. [Current limitations](#current-limitations)
11. [Project layout](#project-layout)

---

## Overview

Aria is three cooperating processes:

- **The agent server** — the orchestrator. It runs conversations, plans and executes multi-step tasks, and serves the web console and its API.
- **The gateway** — owns the model providers, the channel credentials, the document store, the memory plane, and document generation. The agent never holds a credential directly; it calls the gateway.
- **The console** — a React web app (and an optional Electron desktop shell) that drives the agent server on the same port.

The division of trust is deliberate: the agent owns orchestration and the UI; the gateway owns secrets and external services. A channel being "configured" is the gateway's to report, and the agent claims nothing it does not actually have. Credentials never leave the gateway process, and the agent's own logs never contain a token.

A request is rarely answered by a single model call. The orchestrator decomposes it into a **graph of skill-nodes**, which run (often in parallel) and pass results to one another, until a **formatter** produces the final answer. This is what lets Aria take on multi-step work — research, then write, then send — rather than answering one question at a time. The graph is persisted to disk, which is what lets a run be resumed or woken later.

---

## What Aria can do

### Conversation and memory

Aria holds a normal conversation, with two properties that matter for real use:

- **It remembers you across sessions.** A memory store records durable facts, preferences, and decisions, and recalls the relevant ones into every new conversation. You do not re-explain your setup each time. Memory is read once at session start and shared into every skill's prompt for that run.
- **Memory and conversation history are kept distinct.** Memory is background facts about you; the conversation history is what was actually said in this thread. A recalled fact is never presented as something you just said.

Memory is organized into **drawers** — different kinds of recollection, such as working notes (scoped to one run), episodes, durable facts, and documents — so a recall can be scoped to the right kind of thing rather than dumping everything. Each chat turn is a fresh graph (so resuming an old one cannot replay a stale answer), but the session keeps a stable identity, so memory, cost, and history accumulate correctly across turns.

### Documents and retrieval

Aria can ingest documents (PDF, Word, Excel, Markdown, HTML, CSV, and more), split them into chunks, index them, and answer questions grounded in them with citations.

Retrieval is **hybrid**: it combines

- **dense vector search** — semantic similarity over embeddings, so a question matches by meaning, and
- **sparse keyword search** — BM25, so it also matches exact terms and rare words,

and fuses the two rankings with **Reciprocal Rank Fusion (RRF)**. This means a query like "that contract about the payment terms" finds the right page even without the exact wording, while "Article 14(b)" still matches the precise clause. It is the same retrieval used for both the agent's memory and the Documents section.

Grounding is per-conversation and controllable: a document can be enabled or disabled for a given chat, and a run only draws on documents the conversation is allowed to see. A document that is disabled or not shared is never cited.

### Research

Research is a pipeline, not a single "search and summarize" call:

1. The **planner** turns the question into a plan with several angles (facets).
2. The work is **fanned out across multiple researcher agents in parallel**, each covering a different angle and reading multiple live sources (web search, fetch, arXiv, Wikipedia, OpenAlex, news, PDF, tables, Wayback).
3. A **distiller** consolidates the findings into one structured report, recording conflicts and caveats rather than smoothing them over.

Because the researchers run in parallel and the report is assembled from their actual findings, a broad question gets broad coverage instead of a thin paragraph. Each researcher files its findings as a document in the Research section, so the evidence is durable and searchable afterward. A **critic** node can be inserted to review a distiller's output and send it back for another pass.

### Authoring (documents)

Aria produces real files, not descriptions of files. Ask for a report, deck, or spreadsheet, and the authoring pipeline:

1. Has the model produce a structured **spec** (sections, slides, sheets) as data.
2. Renders **actual bytes** from that spec — PDF, PPTX, DOCX, XLSX — including tables, images, and native charts (editable in PowerPoint, vector in PDF).
3. Stores the bytes in a content-addressed artifact store.
4. Returns a receipt and file list taken from the renderer, never from the model's own claim.

The result is a real download with a preview, not text in the chat that you have to reformat. A model that claims an artifact it did not produce is treated as untrusted: only an id the store can resolve is ever shown or linked, so a download button never appears for a file that does not exist. The authoring node always has research above it — a document is written from findings, not from nothing.

### Studio (the coding workspace)

Studio is a full coding agent and workspace, separate from the sandboxed coder used inside research runs.

- **It works inside your projects.** A Studio session declares the folders it may touch, and the agent is confined to those roots. It never silently falls back to some other directory; if a session's folders vanish, the allow-list is empty rather than inherited.
- **It reads before it writes.** The coder uses real tools to inspect the project (list, read, search, symbol lookup), makes targeted edits, runs the project's own checks, and iterates until they pass. It does not ship code it has not verified.
- **Every change is reviewable.** Writes are snapshotted; changes appear as hunks you Keep or Revert, and every change sits in a checkpoint you can restore. A run is an object with an id, a status, a cost, and a JSONL transcript — so it can be stopped, listed, and replayed, not just watched live in one tab.
- **It can verify visually.** With the browser tools enabled, the coder can open the app it just changed and check the result rather than describe what it expects.

The Studio coder is a distinct agent from the DAG's sandbox coder. The DAG's `coder` skill writes into a throwaway sandbox; the `studio_coder` skill delegates a task to the full Studio coding agent against the session's real project roots. The system prompt for the Studio coder is `prompts/studio_coder.md`, re-read when its mtime changes (edits apply on the next message, no restart).

### Channels and integrations

Aria connects to external services through the gateway, which holds the credentials and enforces policy. The full channel set:

- **Email** — Gmail (send with attachments, search, read) and **Zoho Mail** (send, query, reply, mark, move, delete, folders) on a custom domain.
- **Google Calendar** and **Zoho Calendar** — read the schedule, create/list events.
- **GitHub** — list repositories, read code, manage issues and pull requests.
- **Notion** — read and write pages and databases.
- **Chat** — Telegram, Discord, Slack, **Zoho Cliq**, **Google Chat**; plus **Bluesky**, **Mastodon**, and **ntfy** (phone push) for broadcast.
- **Web search** — search and fetch pages (Tavily primary, DuckDuckGo fallback).

The action agent loads only the channels a task needs (**progressive disclosure**: it starts with core tools and calls `load_tool_bundle("<channel>")` to append that channel's schemas mid-run), so a request that touches two services only pays for those two. A single instruction can span several — "summarize my unread email, check my calendar, and post the action items to Slack" — and the planner orchestrates the chain. The action prompt tells the model to call `list_channels()` / `check_channel()` before choosing a delivery target, so it only offers what is actually configured.

The channel layer is built to extend. Adding a channel touches one place per concern — an **adaptor** (`llm_gatewayV9/adaptors/<name>/`), an **integration** (`llm_gatewayV9/integrations/<name>.py`), a **dispatch** route, an **MCP tool**, a **bundle** in `skills.py`, a **grant** in `agent_config.yaml`, and **key docs** in `key_info.py` — so a credential lives in exactly one process. The gateway already models **WhatsApp (Meta/Twilio), Signal, Matrix, Microsoft Teams, LINE, generic IMAP/SMTP, webhooks, and a local mic** alongside the wired set, health-probed and lighting up as they are configured.

### Two-way channel chat

A channel is not only outbound. Aria can be **talked to** over Telegram, Gmail, or Slack:

- A poller (Telegram long-poll; Gmail unread-mail poll; Slack DM/channel read) receives the message.
- It is verified, trust-checked (only an owner/paired sender gets a turn), and turned into a normal chat turn — the full DAG runs (`run_kind="chat"`), so the channel gets the same behaviour as the console.
- The reply goes back on the same channel, as a real conversation with its own stable thread.

So you can start a conversation from your phone or your inbox, not only from the console — and the reply arrives where you asked.

### Scheduling and self-continuation

Aria can act later, not only in the turn you are in:

- **Scheduled tasks** — run a query at a future time (one-shot or recurring), with an optional pre-warning and an acknowledgement exchange.
- **Self-continuation** — `schedule_self` hands a goal to a future turn and ends the current one. The future turn is a fresh planner that is told **both the goal and a summary of what was already done**, so it continues instead of restarting. It can wake on a timer ("in 1h", "tomorrow 9am") or on an event ("when an email from this person arrives" — polled read-only, no cost while nothing matches).
- **Watching for change** — Aria can watch for something (a new document, a new email) and report when it actually changes, rather than re-reading the world on a timer.

When a wake fires, if the session produced research, the most recent findings are prepended inline, so the future planner has the full material without re-searching. This is what lets Aria take on work that finishes later — waiting on research, waiting on an email, pausing a long task and resuming it — and be woken with the full context of what it was doing.

### Computer use

With the desktop agent enabled, Aria can control the computer: open applications, click, type, and read the screen. This is for automating GUI tasks, verifying a change visually, or driving a tool that has no API. It is a perception-driven cascade (gated shell → accessibility tree → vision), gated by an approval allowlist and defaulting to a dry-run mode, so nothing sensitive happens without a human. Some actions (like screenshots) require a reviewed risk classification before they run. The judge/vision model defaults to a fast model and can be overridden by environment variable.

### Model Lab

Model Lab is a showcase for a 124M-parameter language model trained from scratch — pre-training, instruction tuning, and evaluation — with a live playground and the full training story (metrics, loss curves, architecture, screenshots). It is a look at building an LLM end to end, separate from the agent's own model.

### 3D parts

Aria can load, measure, validate, and repair 3D meshes (STL, OBJ, 3MF, PLY) and report what is wrong with them — non-manifold edges, holes, inconsistent normals — for 3D-printing and CAD workflows, with a headless worker and a live Blender bridge sharing one catalog.

---

## How it works

### The agent loop

A chat turn becomes a graph. The **planner** turns the request into skill-nodes; each node runs one skill; the executor runs ready nodes in bounded parallel batches, splices in new nodes as skills produce them, and recovers from failures with a re-plan. When no nodes remain, the **formatter** produces the final answer.

The graph is persisted to disk (the graph plus per-node results), which is what lets a run be resumed or woken later. The executor also enforces contracts — for example, a research run is text-only and can never be turned into a document run by a model that liked the word "report," and an authoring node always has research above it.

### Tools and the gateway

Tool-using skills run a tool loop: the model calls a tool, the harness dispatches it, the result is returned, and the model continues until it produces an answer. Channel tools are thin forwarders to the gateway (`/v1/integrations/{service}/{op}`), where credentials live and policy is applied. This is why the agent never holds a secret and why a channel's configuration is the gateway's to report.

The tool payload a skill sees is built from `agent_config.yaml`'s `tools_allowed` filtered against the catalog in `skills.py` — a tool that is not in the catalog is dropped loudly rather than silently advertised. A test (`tests/test_prompt_truth.py`) fails if a prompt names a tool the payload does not carry, because "documented" and "reachable" are two different facts and only the second is load-bearing.

### Models and routing

The gateway routes each call to a model. It evaluates the requested model's capabilities, fails over across providers when one is unavailable or rate-limited, and pins a node's hops to one provider for prompt-cache reuse. This keeps a long tool-using run both reliable and reasonably priced, and it lets the operator favor free or cheap backends while keeping a cross-vendor backstop. Spend is capped per call, per agent, and per session, and recorded in a ledger.

### Reliability: empty completions and recovery

Two failure modes are handled explicitly, because they are common on free model tiers:

- **Empty completions (HTTP 200, zero text, zero tool calls).** The gateway treats an empty completion as a *failed* call, not a success: it retries the same provider (up to twice) and then fails over to the next provider. Only when the whole pool is momentarily empty does an empty reach the agent.
- **Unusable JSON from a skill.** Every JSON-contract skill (coder, researcher, planner, author, formatter, action, …) that returns empty or malformed JSON gets **one corrective call back to the gateway** — a regenerate with its own schema quoted back — instead of being skipped. A node that still cannot produce usable output is marked failed, and the executor **re-plans around it** (splicing in a recovery subgraph and rewiring waiting nodes) rather than sinking the run.

The net effect: a single bad step — a rate-limited model, a malformed reply — degrades one node, not the whole task, and the run reports honestly what is missing rather than dressing it up as success.

---

## The agents

A skill is one role the orchestrator can dispatch. Each has its own prompt and tool grant; the planner composes them into a graph for a request. Grouped by what they do:

**Cognitive / control**
- **planner** — turns a request into a plan of nodes; the "main agent" that decides what work to do and in what order.
- **critic** — reviews another skill's output and can send it back for another pass.
- **distiller** — consolidates multiple findings into one structured result.
- **summariser** — condenses a set of results into a concise summary.
- **formatter** — produces the final answer; writes a multi-section report when there are several upstream results.

**Research**
- **retriever** — pulls relevant indexed knowledge and documents.
- **researcher** — gathers live evidence from the web and files its findings as documents.

**Authoring**
- **author** — writes and renders real documents (PDF, DOCX, XLSX, etc.).
- **deck** — renders slide decks.

**Coding and interaction**
- **coder** — writes and changes code in a sandbox; the Studio coder is the full-workspace counterpart.
- **studio_coder** — delegates a coding task to the Studio coding agent against the session's real project.
- **sandbox_executor** — runs code the coder produced.
- **browser** — drives a real browser to inspect a page (four-layer cascade: extract → deterministic → a11y → vision).
- **computer** — controls the desktop (gated).
- **vision_file** — understands a local image or screenshot.

**Action**
- **action** — side effects in the world: send email (with attachments), post to channels, create calendar events, schedule tasks, and continue a goal in a future turn.

**Specialist builders**
- **part_analyst / part_engineer / part_blender** — analyse, engineer, and render 3D parts.
- **app_worker / app_builder** — build small apps and surfaces.

A skill can add dynamic successors (the planner can invent new nodes mid-run), and the executor recovers from failures by re-planning. This is what makes the agent a *team*, not a single model call.

---

## The console

The console is the web app (and optional desktop shell) that drives the agent. Each section is one page:

- **Chat** — quick answers and conversation, with memory and document grounding, but no multi-node pipeline (fast, for short questions).
- **Research** — submit a brief and watch the DAG run live: a node graph with an inspector, per-node timeline, and the assembled report.
- **Runs** — every stored run with status, node counts, cost, and age.
- **Memory** — browse, search, and write the agent's memory (facts, preferences, notes, drawers).
- **Documents** — upload, enable/disable, re-index, preview, and search your documents; the grounding source for chat and research.
- **Authoring** — ask for a document in prose, get a rendered file with preview and download.
- **Studio** — the coding workspace: explorer, editor, diff review (Keep/Revert), terminal, checkpoints, and the coding agent.
- **Code** — a read-only workspace browser (folders, search, outline, diagnostics) for quick inspection.
- **Model Lab** — the 124M model showcase and playground.
- **Scheduler** — scheduled tasks, self-wakes, and their status.
- **Skills** — capability packs and per-tool enable/disable.
- **Apps** — generated app surfaces and prefabs.
- **Ledger** — spend by skill, turn history, and per-skill reliability.
- **Architecture** — a view of the system's own architecture.
- **Parts** — 3D mesh analysis and repair reports.
- **Computer** — desktop-control actions and the approval queue.
- **Settings** — environment and read-only configuration status.
- **Console (Mission)** — the landing surface.

---

## Running Aria

Requirements: Python 3.11+, Node 18+, and optionally Ollama for local embeddings.

```
# gateway
cd llm_gatewayV9 && uv sync && uv run main.py          # :8109

# agent + console
cd S9SharedCode/code && uv sync && uv run agent_server.py   # :8500
```

Copy `.env.example` to `.env` and fill in whichever provider keys you have — the gateway fails over across what is configured and runs on local models alone. Then open the console at `http://127.0.0.1:8500`.

### Helper scripts (Windows / PowerShell)

- `start_aria.ps1` starts both services (re-running reuses whatever is already listening, waits for health, opens the console). `-NoBrowser` skips the browser; `-ComputerUseMode live` enables live computer use.
- `stop_aria.ps1` stops the listeners on both ports.

```
powershell -ExecutionPolicy Bypass -File start_aria.ps1 -NoBrowser
powershell -ExecutionPolicy Bypass -File stop_aria.ps1
```

### Verify a restart actually took effect

```
curl -s localhost:8500/api/health     # gateway_up must be true
curl -s localhost:8109/health         # gateway liveness
```

The agent and gateway must both be restarted to pick up backend changes; a running process keeps its loaded credentials and code until restarted. To restart: kill the listener on the port, then start fresh — never assume a restart happened; verify with `/api/health` (agent) and the gateway `/health`.

---

## Verifying Aria

Run the checks after any backend or frontend change. The exact commands matter — the shims resolve the wrong interpreter otherwise.

```
# Agent tests (from S9SharedCode/code — NOT bare `uv run pytest`)
cd S9SharedCode/code
uv run python -m pytest tests/ -q

# Gateway tests (from llm_gatewayV9 — NOT `tests/`; most suites live outside it)
cd llm_gatewayV9
uv run python -m pytest -q

# Frontend (from console-frontend)
cd S9SharedCode/code/console-frontend
npx tsc --noEmit
npm run build

# Live health
curl -s localhost:8500/api/health      # gateway_up: true
```

`S9SharedCode/code/TEST.md` is the Studio verification checklist — read it before changing a Studio surface and add to it when you add one. Scratch E2E/visual scripts live outside the repo and are deleted after the run they verify.

---

## Security and privacy

- **Loopback + a per-launch token.** The services bind loopback; the console sends a per-launch token regenerated each start. Cross-origin writes are rejected.
- **Credentials live in the gateway, not the agent.** The agent process holds no channel token and claims nothing it does not have.
- **File tools are confined** to the session's declared folders; paths that escape are refused, and an unreadable workspace is empty rather than inherited.
- **Commands and writes are approval-gated**, and every command attempt is written to an audit log.
- **Code execution is resource-bounded** — wall-clock timeout that kills the whole process tree, memory and process limits, and a credential-scrubbed environment. It is a resource boundary, not a hardened container; network access is possible, which is why commands are approval-gated.
- **Secrets are never committed**; only `.env.example` templates are tracked. (`.env` files are additionally protected from being read by tooling.)

## Design principles

- **Honesty.** Aria never claims a side effect that did not happen. A tool that fails, a credential that is missing, a step that could not complete — these are reported plainly, and the run is marked incomplete rather than dressed up as success.
- **Resilience.** A failed node triggers a re-plan rather than sinking the run; a single bad step does not end the task.
- **Fail-closed on trust.** An unpaired sender gets no agent turn; an unreadable workspace is empty, not inherited.
- **Cost-awareness.** Free/cheap backends are preferred, failover is automatic, and spend is capped per call, per agent, and per session.
- **Lean prompts, model decides.** Prompts carry methodology and capability tables, not per-case instructions; the model chooses the route, and generic causes are fixed rather than task-special-cased.

## Current limitations

- The main agent delegates to the sandbox coder by default; the `studio_coder` node exists for real-project edits but is not yet the default path for every coding goal.
- Inbound chat is wired for Telegram, Gmail, and Slack; other channels are outbound-only.
- Computer use is gated (some actions need a risk classification) and defaults to dry-run; GUI reliability on some apps is still being hardened.
- The self-poll watch (`schedule_wake`) is behind a flag (off by default); time/email continuations (`schedule_self`) are always available.
- The 3D parts module is partly gateway-side; not every capability is wired into the agent yet.
- Free-tier models can intermittently return empty completions; the gateway retries and fails over, and the agent repairs unusable JSON, but a stronger model is more reliable under heavy bursts.

## Project layout

- `S9SharedCode/code/` — the agent server (`agent_server.py`), skills and executor (`skills.py`, `flow.py`), MCP tools, the pollers, computer-use, and the React console (`console-frontend/`).
- `llm_gatewayV9/` — the gateway: providers, channels/adaptors, integrations, document generation, memory plane, policy.
- `agent-desktop/` — an optional Electron shell over the same console (`build_desktop.ps1` at the repo root builds the portable app).
- `docs/` — deep-dive docs: `ARCHITECTURE.md` (module-by-module), `NEW_CHANNELS.md`, `DOCUMENT_FORMATS.md`, `STUDIO_OPENCODE_PARITY.md`, and more.
- `start_aria.ps1` / `stop_aria.ps1` — start/stop both services.
- `AGENTS.md` — working rules for agents in this repo (layout, services, verify loop).

---

*Aria runs on your machine. Your keys, your data, your agent.*