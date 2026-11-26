# Aria vs. Hermes Agent

A comparison written from the two systems' own documentation, not from
marketing copy. Hermes facts come from Nous Research's repo and docs
(November 2026); Aria facts come from this working tree — the numbers below
were counted, not recalled.

---

## 1. What Hermes Agent is

**Hermes Agent** is Nous Research's open-source personal agent, MIT licensed,
first released February 2026. Its positioning is "the agent that grows with
you" — a CLI-first assistant that persists memory and writes its own skills.

What it actually consists of:

| Layer | What Hermes has |
|---|---|
| **Interface** | A full terminal UI (`hermes`) with multiline editing, slash commands, history, interrupt-and-redirect, streaming tool output. The same agent also answers through a messaging gateway. |
| **Architecture** | **One** main agent with tools, memory and skills. It delegates by spawning isolated subagents for parallel workstreams — a delegation feature, not a planner/graph engine. |
| **Learning loop** | Autonomous skill creation after complex tasks, skills self-improve during use, agent-curated memory with periodic nudges. Follows the `agentskills.io` standard; has a Skills Hub. |
| **Memory** | Persistent, session-aware: FTS5 full-text search over past sessions with LLM summarisation for cross-session recall, plus "Honcho dialectic user modelling" — it builds a model of who you are. |
| **Models** | Model-agnostic. Nous Portal (300+ models), OpenRouter, OpenAI, or any endpoint, switched with `hermes model` at runtime. No lock-in by design. |
| **Tools** | 40+ built-in tools plus toolsets; MCP servers supported; a plugin catalogue and importers for OpenClaw, Claude Code, Codex CLI and Cursor. |
| **Channels** | Telegram, Discord, Slack, WhatsApp, Signal, Email, Home Assistant, plus voice-memo transcription. |
| **Deployment** | Six terminal backends: local, Docker, SSH, Modal, Daytona, Vercel Sandbox, plus plugin backends (Singularity/Apptainer). Runs on a $5 VPS, a GPU cluster, or serverless. |

**The honest summary:** Hermes is a *general assistant harness*. It is built to
be installed anywhere, pointed at any model, and used every day for a wide
spread of small-to-medium tasks. Its distinguishing asset is the learning loop
and the depth of its memory/user-modelling, and its distribution (MIT, one
binary, every backend).

---

## 2. What Aria is

Aria is a **local, multi-surface agent platform** with an explicit
orchestration engine in front of it:

| Layer | What Aria has |
|---|---|
| **Interfaces** | React console (Chat, Research, Authoring, Documents, Apps, Parts, Scheduler, Studio/IDE, Computer), an Electron desktop shell, and a live line of communication to Telegram/Slack/Discord/Gmail/Calendar through a separate gateway process. |
| **Architecture** | A **DAG planner + executor**. A planner node emits a typed graph of skill nodes; the executor runs them with per-node budgets, retries, recovery re-planning, critic verdicts and shortfall reporting. A lightweight single-call mode exists alongside it. |
| **Skills** | **20 registered cognitive roles** (`planner`, `retriever`, `researcher`, `distiller`, `summariser`, `critic`, `author`, `deck`, `formatter`, `sandbox_executor`, `coder`, `browser`, `action`, `vision_file`, `computer`, `part_analyst`, `part_engineer`, `app_worker`, `app_builder`, `part_blender`) wired to **86 MCP tools**. |
| **Models** | A gateway in front of multiple providers with per-agent routing, per-node provider affinity, prompt-cache awareness, spend accounting and policy. |
| **Outputs** | Real files: PDF (reportlab **and** a LaTeX typesetter), PPTX, DOCX, XLSX, plus A2UI interactive surfaces rendered in the console, HTML/markdown reports, and 3D part files through Blender. |
| **State** | Sessions, graphs, per-node transcripts (JSONL), memory drawers (FAISS + keyword), documents with chunk retrieval, apps, schedules, artifacts, audit ledger — all local and inspectable. |
| **Switches** | Every major capability is behind a named flag (`ui.code_ide`, `browser.tools`, `documents.image_search`, `agent.self_wake`, …), so a capability can be turned off without leaving residue. |

---

## 3. Where Aria is stronger

These are structural differences, not taste.

### 3.1 Structured multi-node work
Hermes delegates by spawning subagents; Aria **plans a graph first**. That
buys things a delegation model cannot express:

- **Fan-out with per-node scoping.** "Compare the three cities" becomes three
  researcher nodes with separately scoped briefs, in parallel, each seeing only
  its slice — the leak where every sibling searches for all three is a solved
  problem here, not an open one.
- **A terminal formatter that is not the researcher.** The node that gathers
  and the node that writes are different, so length, structure and citation are
  explicit responsibilities instead of emergent behaviour.
- **Recovery as a first-class path.** A failed node's brief and its waiting
  consumers are handed to a recovery planner that re-routes rather than
  retrying the same thing. A node that *succeeds but reports a shortfall* also
  reaches the planner, tagged as a gap to repair.
- **Critic verdicts** in the graph, not in the model's self-assessment.

### 3.2 Documents are a first-class output, not an export
This is the largest single difference, and it is measurable:

- A **document renderer with a real type system**: 13 named styles, page
  geometry, margin presets, column control, running headers, TOC with page
  numbers and PDF bookmarks, 10 citation styles, figure extraction from the
  user's own uploads, and chart/image/table/callout/stat/column blocks.
- A **LaTeX path** (`typeset="latex"`) that typesets real mathematics —
  numbered equations, radicals, subscripts, `\mathbb`, proper table rules —
  instead of flattening it to text. It falls back honestly when no engine is
  present.
- A **deck builder** with real slide geometry (13 canvas presets), native
  editable charts, stat cards, column cards, callouts, speaker notes, and a
  preview that reads the file's own shapes.
- **The harness guarantees the file.** If the model returns a document spec
  without rendering it, the code renders it. A node cannot "claim a document"
  that does not exist — that path fails the node loudly.

A generic agent harness produces a *document by asking a model to write one*.
Aria produces it by rendering a validated spec through a renderer that has been
tuned against real defects.

### 3.3 Verified side effects
Aria's action layer is built around *proof of the side effect*: a send is
confirmed by a returned message id, a schedule by a `schedule_id`, and a run
that reports success without one is a failure. Prompts carry the live tool
signature, and a test (`test_prompt_truth.py`) fails the build if a prompt
names a tool or parameter that does not exist. Hermes documents no equivalent
invariant.

### 3.4 Local data, local models, inspectable state
Every session is a directory of JSON: graph, per-node inputs/outputs, prompt
sent, tokens, cost. Every run reproduces. Memory lives in local drawers. The
model layer is a gateway with accounting rather than a vendor SDK. There is no
opaque service in the path.

### 3.5 Purpose-built verticals
Blender parts (`part_analyst`/`part_engineer`/`part_blender`), a coding IDE
with hunk-level review and checkpoints, A2UI app generation, and a documents
pipeline with folder scoping and per-conversation binding. Hermes is
deliberately general and has none of these.

---

## 4. Where Aria is weaker

Written plainly, because the gaps are real.

### 4.1 Distribution and installation
Hermes installs anywhere — one command, MIT, six backends, Windows/WSL2
supported. Aria is a local Python + Node + React stack with an Electron shell
built by a PowerShell script, a separate gateway venv, and now a bundled TeX
engine. **Getting Aria running on a stranger's machine is a project; getting
Hermes running is an afternoon.** This is the single biggest gap.

### 4.2 Memory and user modelling
Hermes has FTS5 session search with LLM summarisation and an explicit user
model that deepens across sessions. Aria has FAISS-ranked memory hits, policy
notes and transcript injection — good for grounding an answer, but **no
equivalent of "who is this person" beyond what an explicit memory note says.**
Its persona is evolving, but that is a different thing from an automatic model.

### 4.3 Self-improvement
Hermes writes and refines its own skills from experience. Aria's skills are
hand-authored prompt files plus a catalogue; nothing rewrites a skill because a
task went badly. Aria *does* convert failures into planner-visible shortfalls,
which is a narrower loop over the same idea.

### 4.4 Breadth of channels and voice
Hermes ships Telegram, Discord, Slack, WhatsApp, Signal, Email and Home
Assistant, plus voice-memo transcription. Aria has Telegram, Slack, Discord,
Gmail, Calendar — **no WhatsApp, no Signal, no Home Assistant**, and voice
support that is narrower.

### 4.5 Model breadth at the point of use
The gateway supports several providers and per-agent routing, but the
practical ceiling is what a key is configured for. Hermes's Nous Portal puts
300+ models behind one credential and switches at runtime with no code path
change.

### 4.6 Concurrency and hosting
Hermes runs on Modal/Daytona/Vercel serverless and scales horizontally by
being stateless per run. Aria is a single-process local server with a local
SQLite ledger. Excellent for one user on one machine; **not a hosted
multi-tenant service.** (Per-user gateway keys exist but are paused.)

### 4.7 Ecosystem import
Hermes imports plugins from OpenClaw, Claude Code, Codex CLI and Cursor.
Aria has an MCP surface, so it can *consume* MCP servers, but it cannot ingest
another agent's plugin format.

---

## 5. Side-by-side

| Dimension | Hermes Agent | Aria |
|---|---|---|
| Core model | Single agent + subagent delegation | DAG planner/executor + 20 skills |
| Install | One command, MIT, 6 backends | Python + Node + Electron + gateway + TeX |
| Memory | FTS5 recall + dialectic user model | FAISS hits + drawers + transcripts |
| Learns skills | Yes, automatic | No (hand-authored, with a shortfall loop) |
| Channels | 7 + voice memos | 5 |
| Models | 300+ via Portal, any endpoint | Multi-provider gateway, keyed |
| PDF quality | Via model/templates | Purpose-built renderer **+ LaTeX** |
| Decks | Via model/templates | Real geometry, native charts, shape preview |
| Math | Model-dependent | Real TeX typesetting |
| Documents | Text output | PDF/PPTX/DOCX/XLSX + A2UI surfaces |
| Side-effect proof | Not documented | Enforced (ids or failure) |
| Vertical tools | None | Blender parts, IDE, apps, documents |
| State inspection | Sessions + memory | Full per-node graphs, prompts, cost |
| Hosting | Serverless-capable | Local single-process |
| Multi-user | Your install | Single-user (gateway multi-user paused) |

---

## 6. The short version

**Aria is better at producing artefacts and at executing multi-step work whose
output is verifiable** — research reports, typeset PDFs, decks, spreadsheets,
apps, parts, code — because those outputs are rendered by code rather than
narrated by a model, and because the orchestration is a real graph with
recovery and proof.

**Hermes is better at being installable, adaptable and self-improving** — one
command anywhere, any model, any channel, and it gets better at your tasks over
time without anyone editing a prompt file.

They are not really competing for the same slot: Hermes is a personal
assistant harness, Aria is a local production line for structured deliverables.
The most useful thing Aria could borrow is Hermes's install story and its
automatic skill-improvement loop; the most useful thing Hermes could borrow is
a rendered-document pipeline.

---

### Sources

- [NousResearch/hermes-agent (GitHub)](https://github.com/nousresearch/hermes-agent)
- [Hermes Agent — official site](https://hermes-agent.nousresearch.com/)
- [Hermes Agent Docs](https://hermes-agent.nousresearch.com/docs/)
- [What Is Hermes Agent? (Kie.ai)](https://kie.ai/blog/what-is-hermes-agent)
- [What Is Hermes AI Agent? (Width.ai)](https://www.width.ai/post/what-is-hermes-ai-agent)
- [Alternative to Beamer LaTeX: Practical Options for 2026](https://www.anygen.io/showcase/alternative-to-beamer-latex/index.html)
- [touying — Typst Universe](https://typst.app/universe/package/touying/)
- [Slidev vs Marp vs Reveal.js 2026](https://www.pkgpulse.com/guides/slidev-vs-marp-vs-revealjs-code-first-presentations-2026)
- [The Best Slides-as-Code Presentation Tools](https://xergioalex.com/blog/best-slides-as-code-presentation-tools/)
