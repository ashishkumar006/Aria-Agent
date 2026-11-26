# Aria — General-Purpose AI Agent

A self-hosted AI agent: an LLM gateway, a FastAPI agent server with an async DAG
executor, and a React console. Bring your own keys.

```
┌────────────┐    HTTP     ┌──────────────┐    providers    ┌──────────┐
│  Console   │ ─────────► │    Agent     │ ──────────────► │ Gateway  │
│  (React)   │ ◄───────── │  :8500       │ ◄────────────── │  :8109   │
└────────────┘   SSE      └──────────────┘   /v1/chat      └──────────┘
       │                              │                              │
       │ (Electron shell wraps        │ MCP tools                    │ memory plane
       │  the same console)           ▼                              ▼
┌────────────┐            skills, browser, computer            drawers, ledger,
│ agent-     │            use, documents, parts,               retrieval, policy,
│ desktop/   │            notifier, scheduler                  channels, cost
└────────────┘
```

Three processes, one direction of trust. The agent owns the console and the
orchestration; the gateway owns providers, channels and the memory plane. The
Electron shell is a thin wrapper with no copy of the UI at all — see
[Desktop shell](#desktop-shell).

---

## Contents

1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Architecture](#architecture)
4. [The main agent (orchestrator)](#the-main-agent-orchestrator)
5. [The console, section by section](#the-console-section-by-section)
6. [Studio — the coding workspace](#studio--the-coding-workspace)
7. [Scheduled tasks and notifications](#scheduled-tasks-and-notifications)
8. [Self-scheduling and continuations](#self-scheduling-and-continuations)
9. [Channels and integrations](#channels-and-integrations)
10. [Document generation](#document-generation)
11. [Model Lab](#model-lab)
12. [The 3D parts module](#the-3d-parts-module)
13. [Desktop shell](#desktop-shell)
14. [Security model](#security-model)
15. [Honesty rules the code enforces](#honesty-rules-the-code-enforces)
16. [Testing](#testing)
17. [Known gaps](#known-gaps)
18. [Repository hygiene](#repository-hygiene)

---

## What it does

- **Documents** — upload PDF/DOCX/MD/TXT/HTML/CSV/XLSX, chunk, embed and retrieve
  grounded answers with citations. Per-conversation opt-in.
- **Chat** — streaming and buffered, over a 42-tool catalog (web search and fetch,
  Gmail/GitHub/Notion/Slack/Calendar lookups, file create/edit/search, memory
  read/write, artifact read, document rendering, task scheduling, channel
  discovery), with document grounding and standing memory. The `chat.tools` flag
  is a kill-switch: off means plain text, no tools.
- **Research** — an async DAG executor (`flow.py`) that fans out over skills and
  renders a real report.
- **Authoring** — real file output. Asks the model for a document spec, then
  renders actual PDF/PPTX/DOCX/XLSX through the gateway (`docgen.py`), stores the
  bytes content-addressed in `artifacts.py`, and serves preview and download.
- **3D parts** *(gateway-side, not yet wired to the agent — see below)* — load,
  measure, validate and repair STL/OBJ/3MF/PLY meshes, and report what is wrong
  with them.
- **Code / Studio** — a full coding workspace (`/studio`): a multi-folder explorer,
  an editable buffer, a **coding agent** that reads, edits, runs checks and renders,
  with per-turn **checkpoints** and Keep/Revert review. The agent works inside the
  session's own roots, never the process default.
- **Computer use** — browser and desktop control, gated by an approval allowlist.
- **Protocols** — AG-UI events and A2UI component surfaces, both validated.
- **Memory** — append-only drawers (working, episode, fact, playbook, document)
  with hybrid keyword + vector recall, a cost ledger and a policy gate.
- **Scheduler & self-scheduling** — one-shot and recurring agent turns, pre-warnings,
  a choice of delivery channel, and an acknowledgement conversation. The main agent
  can also **hand a goal to its future self** and wake on a timer or an event.
- **Channels** — the agent can read and write Gmail, Calendar, GitHub, Notion,
  Telegram, Discord and Slack through the gateway (one place for keys), and can be
  **talked to** over Telegram, Gmail or Slack, not just notified.
- **Model Lab** (`/model-lab`) — a showcase for the from-scratch 124M model, with a
  live playground.
- **3D parts** *(gateway-side, not yet wired to the agent — see below)* — load,

---

## Quick start

**Requirements** — Python 3.11+, Node 18+, [Ollama](https://ollama.com) for
local embeddings (`nomic-embed-text`, 768-dim).

```bash
# gateway
cd llm_gatewayV9 && uv sync && uv run main.py        # :8109

# agent + console
cd S9SharedCode/code && uv sync && uv run agent_server.py   # :8500
```

Copy `.env.example` to `.env` and fill in whichever provider keys you have — the
gateway fails over across whatever is configured, and runs fully on Ollama alone.

Open <http://127.0.0.1:8500>. The console route is `/console`; `/chat` redirects
to `/research`, because research is the general-purpose surface. Both services
bind loopback, and the agent issues a per-launch token that the console sends as
`X-Aria-Token`.

Never assume a restart took effect:

```bash
curl -s localhost:8500/api/health     # gateway_up must be true
curl -s localhost:8109/health         # gateway liveness, no token needed
```

> **Windows note.** A shell launched with `Start-Process` is a child of the
> launching process and dies with it. To keep the backends running across
> commands, double-detach:
> ```powershell
> cmd /c 'start "" /min cmd /c "cd /d <dir> && uv run <entrypoint>.py"'
> ```

---

## Architecture

| Path | Role |
|---|---|
| **Gateway** (`llm_gatewayV9/`) | |
| `llm_gatewayV9/main.py` | FastAPI app; registers every router |
| `llm_gatewayV9/providers.py` | Provider adapters (OpenAI-compat, Gemini, Ollama, Groq, Cerebras, NVIDIA, OpenRouter, GitHub, Kilo) |
| `llm_gatewayV9/docgen.py` | Real PDF/PPTX/DOCX/XLSX generation, including vector charts |
| `llm_gatewayV9/doclayout.py` | Page, style, citation and slide geometry |
| `llm_gatewayV9/docgen_api.py` | Gateway generation and preview endpoints |
| `llm_gatewayV9/db.py` | Call ledger, cost rollups, tool-use statistics |
| `llm_gatewayV9/channels_api.py` | 16 channel adaptors, inbound webhooks, pairing |
| `llm_gatewayV9/adaptors/` | Per-channel adaptors + `trust.py` (pairing store) |
| `llm_gatewayV9/pricing.py` | Per-provider cost estimation |
| `llm_gatewayV9/parts/` | 3D mesh validation, repair and analysis |
| **Agent** (`S9SharedCode/code/`) | |
| `agent_server.py` | Agent API, SSE chat, documents, runs, ledger, scheduler, auth, AG-UI, A2UI |
| `flow.py` | DAG executor; run-status and produced-file persistence |
| `skills.py` | Skill execution, length contract, prompt assembly |
| `mcp_server.py` | MCP tool registrations, including `render_document` |
| `mcp_runner.py` | Tool loop and `prepare_fn` pre-dispatch |
| `artifacts.py` | Content-addressed artifact store |
| `memory.py` | Memory reads, drawers, policies |
| `scheduler.py` | Schedule persistence, notification policy, worker |
| `notify.py` | Multi-channel delivery, target resolution, token verification |
| `ack.py` | Acknowledgement conversation state |
| `ack_bridge.py` | Routes an inbound reply to the agent LLM |
| `channel_chat.py` | Inbound channel message → chat turn → reply on the same channel |
| `telegram_poller.py` | Gateway-side `getUpdates` loop; the local-install inbound path |
| `gmail_poller.py` | Inbound Gmail: polls unread mail from an allowlisted sender |
| `slack_poller.py` | Inbound Slack: reads the bot's DMs + channels |
| `sleeps.py` | Self-scheduled continuations: goal + context + time/email trigger |
| `studio_workspace.py` | Studio sessions and their roots |
| `studio_review.py` | Studio baselines, content-fingerprinted hunks, checkpoints |
| `studio_runs.py` | Studio run registry + JSONL transcripts + replay |
| `studio_composer.py` | Studio `@` mentions, `/` commands, commit messages |
| `studio_files.py` | Studio file ops, search/replace plans, git argv (planners only) |
| `flags.py` | Feature flags, read by `POST /api/apps/flags` |
| `agui.py` | AG-UI capability document |
| `prompts/` | Per-skill prompts |
| **Console** | |
| `console-frontend/src/views/` | One file per console section |
| `console-frontend/src/components/markdown.tsx` | Shared status vocabulary + markdown renderer |
| `console-frontend/src/components/AgentNode.tsx` | DAG node in the graph views |
| **Shell** | |
| `agent-desktop/main.cjs` | Electron main process: window, menu, autostart, deep links |
| `agent-desktop/preload.cjs` | The only bridge between the shell and the page |

### Request lifecycle

```
browser ──▶ agent :8500
              ├─ auth (per-launch token, loopback)
              ├─ document retrieval (optional, per conversation)
              ├─ memory recall (cross-session facts, not conversation history)
              ├─ gateway.ensure_gateway()
              └─ POST /v1/chat ──▶ gateway
                                       ├─ normalise, estimate tokens
                                       ├─ provider resolution (pin → route → auto_route)
                                       ├─ capability filter, cooldown/backoff
                                       ├─ execute (retry, then failover)
                                       ├─ ledger write
                                       └─ stream or JSON
```

---

## The main agent (orchestrator)

The **main agent** is the DAG orchestrator behind the chat section. A chat turn is a
`flow.Executor.run()` over a growing graph of **skill-nodes**; the planner turns the
request into a plan, and each node runs one skill:

- `planner` — decomposes the request into nodes.
- `researcher` / `retriever` — gather evidence (web, documents).
- `author` / `deck` — write and render real documents.
- `action` — side effects in the world: send email, post to a channel, create a
  calendar event, schedule a task, and **continue a goal in a future turn**.
- `coder` — write/change code in a sandbox; `sandbox_executor` runs it.
- `formatter` — the final answer (sectioned into a multi-section report when there
  are several upstream results).

The planner emits nodes by **skill and goal**, never by tool — each skill's own
prompt owns its tools. A node can add dynamic successors, and YAML
`internal_successors` add static edges; a critic can be auto-inserted; a failed node
triggers a recovery replan. The graph is persisted to disk (`graph.json` + per-node
files), which is what lets a run be resumed or woken later.

This is the agent that delegates: it fans out to researcher/author/action nodes the
same way it always has, and (with the Studio integration) can delegate real coding
work to the Studio coder.

---

## The console, section by section

Every section is one file under `console-frontend/src/views/`. Routes, the API
each one talks to, and what is currently known about it.

### `/console` — Chat
`Chat.tsx` · `agent_server.py` `chat_simple` / `chat_simple_stream`

Two endpoints, identical message lists, same 21-tool payload, same memory and
document injection. One buffers, one is SSE. They are separate code and have
drifted before; `tests/test_chat_simple_regression.py` pins the invariant.

Tool-call progress is a single `status` line per call (`using <name>…`). A
successful tool result emits nothing. There is no per-call arguments/result panel
and no graph — the empty state says as much: *"Quick answers, no pipeline,
nothing recorded in Runs."*

**Known gap:** the transcript can only render prose, a table, a link, or a file
row. There is no image node in `renderMarkdown`, and `_CHAT_SYSTEM` never tells
the model it could draw a chart — even though `render_document` accepts `chart`
and `image` blocks and can produce real vector charts inside a PDF or deck. The
chart is reachable, but only via the document viewer, after two clicks.

### `/research` — Research
`Research.tsx` · `DagCanvas.tsx`, `Inspector.tsx` · `/api/chat`, `/api/sessions`, `/api/sessions/{id}/*`

Submit a brief at one of three depths; the planner builds a graph and it fans
out over researcher/author/deck/formatter nodes. Live DAG with ports and
orthogonal edges, a node inspector (Overview / Output / Stats), a per-node
timeline, and a report tab.

Run status comes from `runStatusFromNodes` in `markdown.tsx` — the same helper
`Runs.tsx` uses, so the two cannot disagree. A cancelled run reads `stopped`, an
interrupted one reads `interrupted`, and `done` requires every node to have
**succeeded** rather than merely finished.

**Known gaps:** the Files row and reattach work, but the sidebar only reaches a
window of recent sessions, and researcher output averages ~300 words where a
substantial contribution should be 1,500–2,000.

### `/authoring` — Authoring
`Authoring.tsx` · `DocSetup.tsx` (`DocumentViewer`) · `render_document`

Ask for a document in prose; the model returns a spec; the gateway renders real
bytes; the bytes land in the artifact store; the receipt and file list are read
back from the renderer, never from the model. Server-backed history, real preview
thumbnails, Fit/Actual size.

**Known gaps:** deck slide counts have been wrong (6 requested, 3 rendered, 6
claimed); `_doc_stats` measures the spec rather than the rendered output.

### `/documents` — Documents
`Documents.tsx` · `/api/documents`

Upload, per-document enable/disable, re-index, preview, download, delete, and a
retrieval test-search box. Grounding is per-conversation: disabling a document
stops it being cited in a new turn.

The preview is a sandboxed iframe. This is load-bearing: blob URLs share the
console's origin and `.html` is an accepted upload type, so an unsandboxed
preview let an uploaded page read the auth token out of the parent document.
`sandbox="allow-same-origin"` **without** `allow-scripts` is the only value that
both blocks execution and leaves Chrome's PDF viewer working — `sandbox=""`
blocks plugins, so it silently killed every PDF preview.

### `/runs` — Runs
`Runs.tsx` · `/api/runs/summary`

Every stored run with status pills, node counts, cost and age. A run with no
graph reads `no graph` with `age unknown` where the truth is not knowable —
previously those rows read `queued` / `0s ago` and sorted to the top of the list
while being up to 119 hours old.

### `/memory` — Memory
`Memory.tsx` · `/api/memory`

Drawer and kind filters, search, and write. Chat honours these preferences —
verify by writing one and asking what it knows about you.

**Known gaps:** the search half of the load-error path still returns an empty
list instead of admitting the gateway is down; 60 of several thousand records are
shown with no pagination indicator.

### `/ledger` — Ledger
`Ledger.tsx` · `/api/cost/by_skill`, `/api/nodes/health`

SPEND BY SKILL and TURN HISTORY, plus a NODE RELIABILITY table with per-skill
failure rates and grouped reasons. The reliability denominator is nodes
**attempted** — a node that never ran cannot have failed.

### `/scheduler` — Scheduler
`Scheduler.tsx` · `/api/schedule`

Job list and CRUD. Note that this page now surfaces a richer job than it can
itself create: pre-warnings and acknowledgement threads are set via the
`schedule_task` tool, not the form. `GET /api/schedule` is a pure read; the
purge that used to happen as a side effect of listing now has its own endpoint.

### `/skills`, `/apps`, `/settings`, `/mission`
`Skills.tsx` shows capability packs over the MCP catalog and per-tool withholding.
`Apps.tsx` renders prefabs and generated surfaces. `Settings.tsx` is environment
status and read-only config. `Mission.tsx` is the console landing surface.

### `/studio` — Studio
`views/studio/*` — the coding workspace: explorer, editor, review, terminal, and the
Studio coder agent. See [Studio](#studio--the-coding-workspace).

### `/model-lab` — Model Lab
`ModelLab.tsx` — the 124M model showcase and playground. See [Model Lab](#model-lab).

---

## Studio — the coding workspace

`/studio` is the coding IDE: a session-scoped, multi-folder workspace with a file
explorer, an editable buffer, a diff review pane, a terminal dock, and the **Studio
coder** — a real coding agent.

- **Sessions own their roots.** Each studio session declares the folders it may
  touch; the agent is confined to those (`studio_workspace.py`). The process-global
  default is only a fallback for requests with no session.
- **The coder agent** (`prompts/studio_coder.md`) reads before it writes, edits with
  verified `edit_file`, runs the project's own checks, and can drive a browser to
  verify a front-end change. It acts only through tools.
- **Checkpoints & review.** Every approved write is snapshotted; changes appear as
hunks the user Keeps or Reverts, and every change sits in a checkpoint they can
  restore (`studio_review.py`).
- **Runs are objects.** A coder run has an id, an owner, a status, a cost and a
JSONL transcript (`studio_runs.py`), so it can be stopped, listed and replayed — not
just an SSE stream in one tab.

Backend modules, one concern each: `studio_workspace.py` (sessions/roots),
`studio_review.py` (baselines, checkpoints), `studio_runs.py` (run registry +
transcripts), `studio_composer.py` (`@` mentions, `/` commands), `studio_files.py`
(file ops, search/replace plans — planners only, it never writes). `agent_server.py`
owns the routes.

> The Studio coder is a **separate agent** from the DAG's sandboxed `coder` skill.
Today the main agent delegates to the sandbox coder; delegating to the Studio coder
is the next integration.

---

## Scheduled tasks and notifications

The scheduler runs agent turns at a future time. What it could *not* do until
recently was tell anyone: the only delivery path was a hardcoded Telegram summary,
and a reminder that never arrived was indistinguishable from one that was never
set.

### Three new modules

`notify.py`
: One entry point for every channel. Resolves the destination, sends through the
  gateway's own adaptor, and records the attempt either way. Also verifies a
  credential — `check_channel("telegram")` calls `getMe` and distinguishes "the
  gateway holds a token" from "that token still works", which the inventory
  cannot do. Refreshing means re-authenticating where the platform allows it
  without a human, and otherwise naming the env var that needs a new value. It
  never writes a credential to disk.

`ack.py`
: The acknowledgement conversation. Opened per pre-notification, persists to
  `state/ack_threads.json`, bounded by `max_follow_ups` (0–10), `snooze_s` and an
  absolute expiry. Matching is on channel *and* sender.

`ack_bridge.py`
: Routes an inbound reply into the agent. `handle_inbound` looks up the thread,
  appends the reply, asks the model whether to answer, and applies its verdict.

### Who decides, and what is enforced

The model picks one of `acknowledge` | `reply` | `silent`. The bookkeeping does
the enforcing, so a model that ignores the instruction cannot produce an
unbounded loop:

| The model decides | The code enforces |
|---|---|
| what to say | how many messages it may send (`max_follow_ups`) |
| whether a reply is worth sending | that a reply needs text |
| — | the snooze interval between nudges |
| — | the absolute thread expiry |
| — | that only the paired (channel, sender) can drive the thread |

An unrecognised verb becomes `silent`, so a malformed or hostile completion
cannot manufacture an action.

### The tool surface

`schedule_task` gained these arguments (all optional; without them it behaves
exactly as before):

| Argument | Meaning |
|---|---|
| `channel` | `telegram`, `slack`, `discord`, `gmail`, `webhook`, … — validated at schedule time against the live gateway inventory |
| `lead_s` | how long **before** the task to warn, e.g. `"15m"`, `"2h"`, or seconds |
| `lead_message` | the text sent at pre-warning time |
| `message` | the text sent when the task actually fires |
| `wait_for_ack` | make it a conversation instead of an announcement |
| `snooze_s` | delay before nudging again if unanswered |
| `ack_timeout_s` | give up on the conversation after this long |
| `max_follow_ups` | hard cap on re-messages, 0–10 |

Two supporting tools: `list_channels` (what the gateway can reach, and whether
anyone is paired to receive on it) and `check_channel` (whether the credential
still works).

Accepted durations are numbers of seconds or `15m`/`2h`/`1d`/`90s`.
`lead_s: "fifteen minutes"` is **rejected**, not silently zeroed — zero would
mean "no pre-warning", the opposite of what was asked. A lead time longer than
the time until the task is rejected for the same reason.

The pre-warning is armed as its own derived job (`<id>-lead`) that carries **no
query**, so firing it costs no LLM spend. It hangs off the parent, so cancelling
the parent's companion cannot leave an orphan warning armed.

New endpoints: `GET /api/notify/channels`, `POST /api/notify/refresh`,
`GET /api/ack/threads`, `POST /api/ack/inbound`, `POST /api/channel/inbound`,
`GET /api/channel/threads`, and on the gateway `GET /v1/control/telegram`.

### Getting a message *into* the agent

An outbound notification only needs a bot token. Talking **to** the agent needs
an inbound path too, and there are two:

- **Telegram long-poll** (`telegram_poller.py`, started by the gateway lifespan).
  This is what makes the feature work on a local install. Telegram's webhook
  delivery requires a public HTTPS URL — it can never reach `127.0.0.1` — and
  nothing here ever called `setWebhook`, so the webhook route was unreachable in
  practice: messages could not arrive, so no sender could ever be paired, so
  `resolve_target` returned empty and every reminder failed with *"no destination
  paired"*. The poller calls `getUpdates` from inside the gateway (the only
  process holding the bot token) and feeds updates through the same
  `_record_inbound` path the webhook uses. The offset is persisted, so a restart
  neither replays nor drops messages, and it stands down automatically if a
  webhook is ever registered (Telegram answers `getUpdates` with 409, and two
  consumers of one queue is a bug). Disable with `TELEGRAM_POLLING=off`.
- **`POST /v1/hooks/{name}`**, for anything that can reach the gateway over HTTP.

Pairing is trust-on-first-contact: the first sender to message the bot on an empty
store becomes `owner`, and later senders only ever become `paired`, with
`resolve_notify_target` preferring `owner`. So a stranger who finds the bot cannot
redirect your reminders. Pair manually with `POST /v1/control/pair` (loopback).

**The forward runs off the event loop.** `_record_inbound` makes a blocking call
into the agent, and the agent calls straight back into the same gateway to ask
whether the sender is paired. Inline, that deadlocks against itself: the loop waits
on the agent, the agent waits on the loop, the trust lookup times out into
`unknown`, and every message is refused as untrusted behind a cheerful `200`.

That endpoint routes in a fixed order. A reply belonging to a live
acknowledgement thread is consumed by `ack_bridge` whatever its verdict,
including `silent` — "k" is an answer to a reminder, not a new question, and
answering it twice is worse than not at all. Anything else becomes an ordinary
chat turn: trust-gated against the gateway's pairing store (an unpaired sender
is refused, because a channel the agent can be talked to is also one a stranger
can type into), answered back over the same channel, with its own stable
conversation id per `(channel, sender)` so history survives a restart. The turn
runs on `/api/chat/simple/stream` in a daemon thread — the webhook must answer
its platform fast, and that route reuses the full chat pipeline (thread history,
memory recall, document grounding, the tool loop) rather than a second
implementation of it. The lightweight stream is the target, not `POST
/api/chat`: that route runs the research DAG, and "what time is it" must not
cost a multi-node run.

---

## Self-scheduling and continuations

The main agent can act **later**, not just in the turn the user is in.

- **`schedule_task(query, when)`** — run a query at a future time (one-shot or
  recurring), in the same conversation, with an optional pre-warning and an
  acknowledgement exchange. This is a reminder or a deferred job.
- **`schedule_self(goal, when | email_from, context?)`** — hand a **goal** to a
  future turn of yourself and end this one. The future you is a fresh planner that
  is told **both the goal and the `context`** (what you already did), so it
  continues instead of restarting. Wake triggers:
  - a **time** — "in 1h", "tomorrow 9am", an ISO datetime;
  - an **event** — `email_from` an address; the agent polls Gmail read-only (no cost
    while nothing matches) and wakes when a message from that person arrives.

When the trigger fires, the scheduler runs a **fresh DAG turn** whose query is the
goal plus the context. If the session produced research, the most recent findings
are **prepended inline** (`scheduler._session_research_context`) — so the future
planner has the full material without re-searching. The result is delivered to the
conversation and the in-app feed.

A **receipt guard** keeps this honest: an action node that reports `tool_used` but
never actually dispatched a tool is retried once, then failed loudly, rather than
claiming a schedule/send/create that never happened.

## Channels and integrations

The **gateway is the single place** credentials live and channels are configured.
The agent calls them through `/v1/integrations/{service}/{op}` (policy-gated, never
holding the token itself).

- **Read/write:** Gmail (send, search, read), Calendar (events), GitHub (repos,
  issues, PRs, code search), Notion (pages, databases, blocks), Telegram, Discord,
  Slack (send + history).
- **Progressive disclosure:** the action skill starts with core tools and calls
  `load_tool_bundle("<channel>")` to append that channel's schemas mid-run, so a
  task only pays for the channels it names.
- **Inbound chat (talk TO the agent):** Telegram (long-poll), **Gmail** (polls unread
  mail from an allowlisted sender), and **Slack** (reads DMs + channels) all feed
  `channel_chat.route_inbound`, which runs a trust-gated turn and replies on the same
  channel. So you can chat with the agent by email or Slack, not just Telegram.
- **Operations UI:** the gateway console exposes key fields, OAuth connect/refresh
  (Gmail/Calendar), channel health (`/v1/channels/health`), and a key link per
  provider — so keys and channels are easy to manage in one place.

## Document generation

Authoring does not ask the model to "make a PDF". The flow is:

1. the model returns a **spec** (sections, slides, sheets) as JSON;
2. the gateway renders real bytes from that spec (`docgen.py`);
3. the bytes go into the content-addressed artifact store (`artifacts.py`);
4. the receipt and the file list both come from the renderer's return value.

Only an artifact id the store can resolve is ever shown or linked. A model's own
`artifact` field is treated as untrusted: a live run once produced `art:dummy`,
`art:generated_document` and `art:mock-placeholder`, every one invented by a
model that never called the renderer, each one a dead download button. The guard
exists on both sides of persistence — the writer refuses to record an
unresolvable id, and the reader refuses to return one.

Explicit length requests are binding ("one page" means one page). There is no
invented default cap; otherwise the author decides length from the material.

Supported block types include `heading`, `paragraph`, `bullets`, `numbers`,
`quote`, `table`, `pagebreak`, `cover`, **`chart`** (bar/line/pie, native editable
in PPTX, vectors in PDF, degraded to a data table in DOCX) and **`image`** (a
figure lifted from an uploaded document by page).

---

## Model Lab

`/model-lab` showcases the from-scratch **124M instruction-tuned model** (separate
from the agent's own LLM): a portfolio-style overview (metrics, training pipeline,
architecture, real train/val loss chart, strengths/limits/next) plus a **live
playground** that embeds the local Model Lab app on port 7861.

- The playground asks `GET /api/model-lab/status` first; when nothing is listening on
  `:7861` it shows a clear "not running" placeholder (with `python run_model_lab.py`
  instructions) instead of a blank iframe.
- Screenshots of the lab live in `console-frontend/public/assets/model-lab/`.
- The model card and HF Space link out to Hugging Face.

## The 3D parts module

`llm_gatewayV9/parts/` — 12 modules, 201 tests, and a documented public API:

```
analyse_part(source)      load, measure, repair, re-measure, report
validate_part(source)     the same without the repair passes
load(source)              bytes/path -> trusted Mesh
validate_mesh(mesh)       Mesh -> Validation
repair_mesh(mesh, opts)   Mesh -> RepairResult
to_binary_stl(mesh)       the only native writer
capabilities()            which optional backends are installed
```

V1 deliberately excludes topology optimisation, mesh decimation, thin-wall and
overhang analysis, multi-body handling and hole filling. Hole filling is a design
decision rather than an omission: filling a boundary would hide a defect that
should be reported.

**Current status, measured rather than assumed:**

| | |
|---|---|
| Tests | `pytest parts/tests -q` → **201 passed** |
| Gateway routes | live and open to loopback: `POST /v1/parts/analyse`, `POST /v1/parts/validate`, `GET /v1/parts/capabilities` |
| Not implemented | `/v1/parts/formats`, `/v1/parts/slicer` → 404 |
| Optional backends | `trimesh` ✗, `manifold3d` ✗ — absent, reported rather than raised |
| Agent side | **nothing.** No MCP tool, no skill, no console view, no console API call references `/v1/parts` |
| Native readers | whatever `readers.py` lists as `SUPPORTED`, without `trimesh` |

So the package is complete and tested on its own terms but **not integrated into
the agent**. The gateway can analyse a mesh; the agent cannot ask it to, the
console cannot display a report, and neither `trimesh` nor `manifold3d` is
installed, which limits format coverage and leaves mesh repair without a topology
oracle.

**To integrate it, the work is on the agent side:**

1. an MCP tool (`analyse_part` / `validate_part`) in `S9SharedCode/code/mcp_server.py`
   that proxies to the gateway, mirroring how `render_document` proxies to docgen;
2. a `parts` skill + prompt so the DAG can route mesh questions;
3. an upload path — documents and artifacts have one, meshes have none;
4. a console view, or a report surface inside an existing one, since
   `PartReport` has a schema version and no renderer.

---

## Desktop shell

`agent-desktop/` — an Electron window over `http://localhost:8500/console`.
**There is no copy of the UI in it.** `main.cjs` loads the console from the
server, so a console fix appears in the app with no Electron change at all.

Each fix still needs its normal step: Python changes need the agent restarted
(which means quitting the shell, because `will-quit` kills the backends it
spawned), and frontend changes need `npm run build` because the shell serves the
built `dist/`, not source.

The shell health-gates on startup, auto-starts missing backends, enforces a
single instance, handles `aria://` deep links, and kills its child process tree
on quit. The OS title bar is hidden (`titleBarStyle: 'hidden'`) and replaced by a
34px in-app strip with minimise/maximise/close; `-webkit-app-region: drag` makes
the strip move the window while the buttons are `no-drag`, because an element
marked `drag` swallows clicks. The application menu is hidden until Alt.

Two shell-only behaviours are worth knowing, since they exist nowhere else: the
boot splash polls from the **main** process, because a `data:` URL has an opaque
origin and CORS refuses it (that is why the auto-retry used to never fire); and
the gateway readiness probe uses the public `/health`, because `/v1/control/
presence` needs a token and answered 401 to a healthy gateway — which made the
shell spawn a second gateway on every launch and log a socket bind error.

Keep the browser console as your working surface even though the app is the
shipped thing: it is the faster loop, and it is the escape hatch for a shell bug.

---

## Security model

- **Loopback + per-launch token.** Binding to a non-loopback address without a
  token refuses to start. The token is regenerated each launch, injected into the
  SPA as a meta tag, and sent as `X-Aria-Token`; cross-origin writes are
  rejected by origin comparison as a second layer.
- **What the token is not.** Not authentication of a user — there is no user. It
  is origin enforcement: a remote web page gets neither the token nor the
  ability to send the header. It does not stop another process running as the
  same OS user, anything already on loopback, or a browser extension with host
  permissions. Stated plainly because the module docstring claims the same
  bounds.
- **Iframes are sandboxed, and never with both `allow-scripts` and
  `allow-same-origin`.** That combination is the one that re-opens token theft.
- **Policy gate** on the gateway, dry-run by default. Agent-dispatched MCP calls
  still bypass it — see Known gaps.
- **Computer use** is allowlist-driven and defaults to disabled.
- **Secrets** are never committed; only `.env.example` is tracked.
- **Credentials live in the gateway, not the agent.** A channel being
  "configured" is the gateway's to report; the agent process holds no channel
  token and claims nothing it does not have.

---

## Honesty rules the code enforces

These are not style preferences; each has a bug behind it and a test.

- **A model-claimed identifier is never shown unless the store resolves it.**
- **"No data" and "failed to load" are different states.** `/api/nodes/health`
  reports how many graphs it could not parse; the Ledger says so instead of
  rendering an empty table as "no completed nodes".
- **A failure rate's denominator is nodes attempted.** A node that never ran
  cannot have failed, so `pending` and `interrupted` nodes are counted
  separately rather than diluting the percentage.
- **A missing price is not a zero price.** The Ledger prints `—`, never
  `$0.0000`, in the same breath as "cost data unavailable".
- **A read does not mutate.** `GET /api/schedule` used to purge aged schedules
  as a side effect, and the console polls it every 15s — so opening the page
  deleted execution history.
- **A degraded run stays degraded.** "This answer is incomplete" is persisted
  next to the run; it used to exist only in the SSE frame that carried it once,
  so reloading the run showed a complete-looking report.
- **Empty and prose failures are different bugs** and are counted separately. A
  renderer `NameError` is reported as an agent bug, not as model misbehaviour.
- **A capability document describes what the agent can actually do.**
  `/api/capabilities` honours the `chat.tools` kill-switch instead of advertising
  21 tools the model was not given.
- **Bad input is rejected, not silently defaulted.** An unparseable buffer
  duration, a lead time longer than the delay, or an unknown channel all fail
  with the list of valid values, because defaulting any of them to zero disables
  the thing the caller asked for.

---

## Testing

```bash
cd llm_gatewayV9     && uv run python -m pytest tests/ -q
cd llm_gatewayV9     && uv run python -m pytest parts/tests -q
cd S9SharedCode/code && uv run python -m pytest tests/ -q
cd S9SharedCode/code/console-frontend && npx tsc --noEmit && npm run build
```

Use `uv run python -m pytest`, **not** bare `uv run pytest` — the shim resolves
the wrong interpreter and fails on `numpy`. The full agent suite takes about
five minutes; target files when iterating.

Collection is pinned to `test_*.py` in `pyproject.toml`. Pytest's default also
matches `*_test.py`, which silently swept in two scratch scripts with absolute
paths from another machine.

The live-provider test (`test_worker_chat_each_live_provider`) is deselected in
the default run because repeated live calls hit rate limits.

Playwright (`npx playwright test`) drives a live server and writes screenshots to
a machine-local temp path. Several of its document assertions pass on the initial
render, before indexing completes, so they verify that a row appears rather than
that a pipeline worked.

### Tests that must not be written

Avoid these, because each one has shipped a real bug:

- **Asserting on `inspect.getsource(...)` string literals.** It passes when the
  code is unreachable or inside a comment, and it pins the exact line that is
  wrong — the node-health denominator test did exactly this and blocked the fix.
- **Re-implementing the code under test inside the test.** The turn-cost
  fallback test asserted `calls == 0`, which is exactly the bug it should have
  caught.
- **Mocking the one function whose behaviour is the point.** The notification
  test mocked the recorder, so a write-only, UI-invisible record passed.
- **Asserting `x == 0` or `... or True`.** Unconditional passes.
- **Capturing a value and never asserting it.** The `chat.tools`-off test built
  a `seen` dict from a mock that was never called and asserted only the status
  code, so it passed identically with tools on.
- **Patching a symbol that does not exist.** `raising=False` creates it, the
  patch is never read, and the test quietly makes a real, billed LLM call.

---

## Known gaps

Honest list of what is not finished:

- **The main agent cannot yet delegate to the Studio coder.** The DAG delegates to
  the sandboxed `coder` skill; wiring it to the Studio coding agent (real workspace,
  checkpoints) is the next integration.
- **Self-wake is built but the self-poll watch is behind `agent.self_wake`** (off by
  default). `schedule_self` (time/email continuations) is always available.
- **The 3D parts module is not integrated.** Gateway-side and tested, agent-side
  missing entirely — see [above](#the-3d-parts-module).
- **Chat cannot show a picture or a chart.** No image node in the markdown
  renderer, and the system prompt never mentions charts, though `render_document`
  supports them.
- **No media regression test anywhere.** `renderMarkdown`, the artifact scanner
  and the preview path have no coverage; deleting every `<img>` would keep the
  whole suite green.
- **Gateway `/v1/*` requires `X-Gateway-Token`** (shared token from
  `GATEWAY_V9_TOKEN`, else `state/gateway.token` published at startup).
  Unauthenticated calls get 401; non-loopback bind without a configured token
  refuses to start. Docs endpoints (`/openapi.json`, `/docs`) stay open.
- **Policy is dry-run**, and MCP mutations dispatched by the agent skip the gate.
- **Image URLs in `/v1/chat` are fetched without SSRF checks.**
- **Streaming bypasses rate accounting, retry and failover.**
- **Run idempotency is best-effort.** A duplicate identical submit is refused
  rather than double-billed, but the claim is per-process and the release paths
  are the fragile part — see `tests/test_idempotency_release.py`.
- **`/api/chat/simple` blocks the event loop** for the whole model call: it calls
  a synchronous client inside an `async def` handler, so every other endpoint
  stalls behind a one-shot chat. Measured at a 34s turn slowing `/api/health` to
  a 3s timeout.
- **Chat renders are only reachable by luck.** `render_document` works, but
  nothing tells the model to print the artifact handle, so a real 2,250-byte PDF
  sat in the store unreachable from the Chat transcript.
- **No global artifact index.** Rendered files are reachable through the run
  that produced them; `/api/artifact/{id}` works if you already know the handle.
  There is no list-the-store endpoint, so historical artifacts have no single UI
  route.
- **Fired-schedule and acknowledgement results are not yet surfaced in the
  console.** `GET /api/notifications` and `GET /api/ack/threads` serve them; no
  view renders them.
- **Channel destinations.** Gmail + Calendar are connected (OAuth); Telegram/Discord/
  Slack/Notion/GitHub are configured but need a destination (e.g. send `/start` to
  the Telegram bot, invite the Discord bot, paste a `xoxb-` Slack token) before a
  notification has somewhere to go. `list_channels` / `/v1/channels/health` show
  which are live.
- **Pricing gaps.** `pricing.py` has no entry for some provider keys in active
  use, so those calls are priced at zero — indistinguishable from free tier.
- **Research output depth.** The researcher returns roughly 300 words on
  average; a substantial contribution is expected to be 1,500–2,000. The planned
  fix is an artifact-backed research working set plus a distiller node, not a
  longer prompt.
- **Computer-use approval** is per-session, not per-action.
- **Some tests are order-dependent.** A handful pass alone and fail in the full
  suite because they write shared state.

---

## Repository hygiene

- Runtime `state/`, logs, model weights, `node_modules/` and `.venv/` are ignored.
- No credentials in the tree. `.env.example` files are templates with empty
  values.
- `artifacts.py` creates its store directory at import, so importing the module
  has a filesystem side effect.
- Test fixtures and scratch scripts do not belong in the repo root or in
  `tests/`; keep them in a temp directory and delete them when the run that
  needed them is finished. Several committed `_*.js` scripts and screenshots still
  sit in `tests/` from earlier sessions and target selectors that no longer exist.
