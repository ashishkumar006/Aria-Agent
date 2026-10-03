# Agent-driven UI: A2UI and AG-UI

Surveyed and then **implemented**, 2026-10-03. This file records what shipped,
what is deliberately not built, and the reasoning.

## TL;DR

- **AG-UI** — shipped. `POST /api/agui` plus `GET /api/capabilities`, behind
  the `chat.agui` flag. Additive; the console does not use it.
- **A2UI** — the catalog and the generation pipeline shipped. `GET
  /api/a2ui/catalog`, `POST /api/a2ui/validate`, `POST /api/a2ui/generate`
  behind `apps.a2ui`. No console renderer yet; see "What is not built".
- **Auth** — shipped, and it was the gate. A per-launch token on every
  `/api/*` route, plus a loopback binding guard. This is what made Stage 3
  defensible rather than reckless.

## What shipped

### Stage 1 — AG-UI vocabulary (`agui.py`, `/api/agui`)

Emits `RUN_STARTED`, `TEXT_MESSAGE_START/CONTENT/END`, `TOOL_CALL_*`,
`ACTIVITY_SNAPSHOT`, `STEP_STARTED`, and exactly one `RUN_FINISHED` or
`RUN_ERROR`. Verified live against a real model call.

The encoder is hand-rolled rather than using the official `ag-ui-protocol`
Python SDK, because that SDK advertises "16 core event types" while AG-UI 1.0
defines 31 across 8 categories — it lags the TypeScript implementation.
Depending on a lagging package to emit six events would be a bad trade, and a
hand-rolled subset stays auditable in one screen. If the SDK reaches parity
this module is the only thing that changes.

`chat_simple_stream` was deliberately **not** touched. Translating its frames
would have been less code but would have inherited its lossy shape (tool
progress there is a free-text line), and `TOOL_CALL_*` is most of what AG-UI
offers. The new endpoint shares the helpers, not the code path.

Not emitted, deliberately: `STATE_SNAPSHOT`/`STATE_DELTA`/`MESSAGES_SNAPSHOT`
(conversation history already lives behind `/api/chat/threads/{id}`; duplicating
it would create two sources of truth), `REASONING_*`, `SUBAGENT_*`, `RAW`/`CUSTOM`.

### Stage 2 — the catalog (`a2ui_catalog.py`)

17 components mapped to Aria's real widgets (`Pill`→`Badge`, `Stat`→`KeyValue`,
`Empty`→`Empty`, and so on). This is the actual asset: the protocol is cheap,
the catalog is the work.

It is also the security boundary. A *closed* set means the model cannot invent
a component, because anything outside it is refused before render. Rejection is
loud and specific — a half-accepted surface renders as though it were
authoritative.

Enforced limits: 400 components, depth 12, 64 children per parent, 4000 chars
per field, 60 000 chars total, 256 KB per surface. Cycles are detected with a
visited set (a cycle is a DoS wearing a tree costume). Forbidden property
names — `html`, `innerHTML`, `style`, `className`, `src`, `href`, `onClick` —
are refused by name rather than falling through the generic unknown-property
message, because they are attacks rather than mistakes.

**Read-only by construction:** there is no text input and no submit action. A
generated form whose submission performs an action is an approval flow wearing
a disguise, and an agent must not be able to synthesise one.

### Auth (`auth.py`)

A per-launch token, generated on real startup, injected into the SPA shell as
`<meta name="aria-token">`, required as `X-Aria-Token` on every `/api/*` route.
A custom header rather than a cookie specifically because a cookie is sent
automatically cross-origin, which was the bug.

Verified live: no token → 403, wrong token → 403, token prefix → 403, correct
token → 200, cross-origin write with a valid token → 403, same-origin write with
a valid token → 200, `/api/health` public for supervisors.

Binding to `0.0.0.0`/`::` with no token **refuses to start** unless
`ARIA_ALLOW_REMOTE=1`. A warning that scrolls past is not a control.

**What it does not protect against, stated plainly:** another process running as
the same OS user (it can read `state/agent.token`), anything already on
loopback, and a browser extension with host permissions. This is origin
enforcement, not user authentication, and there is no user.

### Stage 3 — generated surfaces (`a2ui_surfaces.py`, `/api/a2ui/*`)

`generate` asks the model for a surface with the catalog injected into the
prompt, validates the reply **at generation time**, and returns 422 with the
specific reason if it fails. One retry, and the retry prompt carries the
diagnosis — without that the retry is byte-identical and simply wastes a call.

The generation call has **no tools**, so producing a UI cannot reach the
filesystem, the network, or an account. Caller context is flattened and bounded
(2 KB total, 200 chars per value, nested objects described by size) because it
is model input and therefore both an injection surface and a cost problem.

Every surface ships with a plain-text rendering so it can be reviewed without
being rendered — a generated UI nobody can inspect is one nobody should trust.

Verified live: a real model produced a valid 5-component surface on the first
attempt, values resolved through the data model, wire messages in the mandated
`createSurface → updateComponents → updateDataModel` order.

## What is not built

- **No console renderer.** The catalog maps components to Aria widgets, but no
  React component consumes a surface yet. `validate` and `generate` are real
  and tested; rendering one in Apps is the next piece of work, and it is the
  part that decides whether any of this is worth having.
- **No multi-window surfaces.** A2UI's strongest property is multiple named
  surfaces, which an Electron build could put in separate OS windows. That is
  the strongest future argument for revisiting this.
- **No `@ag-ui/client` rewrite.** The console keeps its own tested client.
- **No model-generated markup or code**, ever.

## What is genuinely hard here

Generative UI compliance is the weak point, and it is worth recording rather
than hiding. Getting a model to emit the catalog's shape took four prompt
iterations, each fixing a real observed failure:

1. Whole prompt in `system`, placeholder `"go"` in `user` → the model replied
   *"I'm ready! What are we doing?"*
2. Top-level keys shown only as an example → it invented
   `{summary, view_type, data_points}`.
3. Keys not stated explicitly → it emitted `{"type": "stat-grid"}` instead of
   `{id, component, ...}`.
4. Component properties not listed per component → it gave `MetricRow` the
   `label`/`value` of a `KeyValue`, and bound values as `{{total_runs}}` rather
   than `{"path": "/total_runs"}`.

That is four wasted LLM calls per failure mode in the worst case, and it is the
argument for the **catalog being curated rather than generated** — the prompt
steering is fragile in a way the validator is not.

## Remaining open work

- The Apps section has no UI for `generate`. Until it does, this is an API.
- Electron would change two recommendations: Monaco becomes the honest answer
  for the Code stub, and loopback binding plus this token becomes a real
  security boundary rather than a local-only mitigation.


---

## What they actually are

### AG-UI — the event layer

A bidirectional typed event stream, normally over SSE. Every run is
`RUN_STARTED` → … → `RUN_FINISHED` or `RUN_ERROR`. 1.0 ships 31 events in 8
categories:

| Category | Events |
|---|---|
| Lifecycle | `RUN_STARTED` `RUN_FINISHED` `RUN_ERROR` `STEP_STARTED` `STEP_FINISHED` |
| Text | `TEXT_MESSAGE_START` `TEXT_MESSAGE_CONTENT` `TEXT_MESSAGE_END` (+ `TEXT_MESSAGE_CHUNK`) |
| Tool calls | `TOOL_CALL_START` `TOOL_CALL_ARGS` `TOOL_CALL_END` `TOOL_CALL_RESULT` (+ `CHUNK`) |
| State | `STATE_SNAPSHOT` `STATE_DELTA` (RFC 6902 JSON Patch) `MESSAGES_SNAPSHOT` |
| Reasoning | `REASONING_*` including `REASONING_ENCRYPTED_VALUE` |
| Activity | `ACTIVITY_SNAPSHOT` `ACTIVITY_DELTA` |
| Subagents | `SUBAGENT_STARTED` `SUBAGENT_FINISHED` (1.0) |
| Escape hatches | `RAW` `CUSTOM` |

1.0 also adds a **strict `schema.json`**: an undeclared field fails validation
except under `metadata`. So "loose compatibility" from earlier versions is now
"strict, versioned, generated SDKs".

**A Python SDK exists and is the reason this is cheap for us**:
`ag-ui-protocol` on PyPI, Pydantic-based, `ag_ui.core` (types/events) +
`ag_ui.encoder` (SSE framing and `Accept`-header content negotiation).
FastAPI usage is documented as a `StreamingResponse` over an event generator.
There is also a `capabilities` endpoint so a client can discover SSE vs
WebSocket vs binary-protobuf support *before* starting a run.

SDKs: Kotlin, Go, Dart, Java, Rust, Ruby, C++ supported; .NET in progress.
Adopted by Google, Microsoft, Amazon, Oracle; works with LangChain, Mastra,
Strands, CrewAI, ADK, Semantic Kernel.

### A2UI — the rendering contract

The agent emits **declarative JSON describing UI intent**; the client renders
it with its *own* component library. Nothing in the payload is executed, which
is the property that makes it safe to cross a trust boundary. Wire format is
JSONL; v0.9 has exactly four message types:

```
{ "version":"v0.9", "createSurface":    { surfaceId, catalogId, theme?, sendDataModel? } }
{ "version":"v0.9", "updateComponents": { surfaceId, components:[ {id, component:"Text", ...} ] } }
{ "version":"v0.9", "updateDataModel":  { surfaceId, path:"/user", value:{...} } }
{ "version":"v0.9", "deleteSurface":    { surfaceId } }
```

Key mechanics:
- **Surfaces** — independent named render targets. Multiple at once.
- **Flat component list** with a `component: "Text"` discriminator and
  children referenced by ID (an adjacency list). v0.9 moved *away* from
  `{"Text": {...}}` specifically because LLMs generate the flat form reliably.
- **Data binding by JSON Pointer.** A component binds `{"path": "/user/email"}`
  and re-renders when that path changes.
- **`sendDataModel`** — when true, the client appends the whole data model to
  every message it sends back. That is the bidirectional loop: user interacts,
  agent gets the state.
- **`catalogId` is required in v0.9** — the agent must name the catalog it is
  speaking. One unified catalog of components *and* client functions.
- A **"Smart Wrapper"** lets a client wrap existing components (including a
  sandboxed iframe) into A2UI's binding and event system.

**Status:** v0.9.1 is the current production release; v1.0 is a release
candidate; v0.8 is legacy. The repo still carries an "early stage public
preview / expect changes" warning.

**Renderers:** React, Lit, Angular (all ✅ Stable for v0.9), Flutter via the
GenUI SDK (Stable). Jetpack Compose planned. All web renderers share
`@a2ui/web_core` (message processor, state, binding); each framework package
adds only the render layer.

> **Correction to the earlier version of this file.** It said to wait because
> "there is no React renderer yet, and one is the most likely blocker". That is
> wrong: `@a2ui/react` is **Stable**, Apache-2.0, ~80k weekly downloads. The
> renderer was never going to be the blocker. The catalog is.

**The two compose.** A2UI is designed to travel over A2A and AG-UI, and
Google's position is that any agent already speaking AG-UI can drive A2UI with
no custom integration — a small AG-UI middleware teaches the agent the message
shapes and wires streaming through.

---

## How Aria already compares

Our chat stream already emits typed frames with lifecycle boundaries. The shapes
map almost one-to-one:

| Aria (`/api/chat/simple/stream`) | AG-UI |
|---|---|
| `started{conversation_id}` | `RUN_STARTED` |
| `status{text}` (thinking + tool progress) | `REASONING_*` / `ACTIVITY_*` |
| `delta{text}` | `TEXT_MESSAGE_CONTENT` |
| live tool progress | `TOOL_CALL_START/ARGS/END/RESULT` |
| `done` | `RUN_FINISHED` |
| `error` | `RUN_ERROR` |

So this is a **rename plus a few missing events**, not a rewrite. That is the
argument for doing it.

The argument against rewriting the console to `@ag-ui/client`: it would replace
a client that works, is tested, and knows our views. Interop value is zero
until a second client exists.

---

## Recommended integration

### Stage 1 — AG-UI vocabulary behind a flag (do this)

Add `POST /api/agui` speaking AG-UI, driven by the *same* internals as
`/api/chat/simple/stream`. Emit `RUN_STARTED` / `TEXT_MESSAGE_*` /
`TOOL_CALL_*` / `RUN_FINISHED|RUN_ERROR`. Add `GET /api/capabilities`
declaring SSE support.

Why worth doing now:
- Small, additive, reversible.
- Validates our event stream against an external schema instead of our own
  opinion of it — we have already found real bugs that way in this session.
- The `ag_ui.encoder` handles `Accept` negotiation and SSE framing we would
  otherwise keep hand-rolling.
- It is the only route to A2UI later, if we ever want it.
- Any MCP/LangGraph/ADK interop becomes possible.

Cost: one endpoint, one flag, ~1 day, no console changes.

### Stage 2 — the A2UI catalog (do this before any A2UI)

The catalog is the actual asset and it is useful **with or without** A2UI. Map
Aria's own primitives into a catalog document: `Pill`, `Stat`, `Empty`, `Skel`,
`Rail`, `TopBar`, plus the data-display components the console already has
(run row, node card, cost row, document row, chunk hit, memory item).

Why: an agent emitting Google's generic `basic` catalog (`Button`, `Column`,
`Text`) produces UI that looks foreign in Aria and cannot express our design
system. The catalog is also what makes generated output *safe*: a closed,
curated set means the model cannot invent components.

### Stage 3 — one narrow A2UI surface (only after auth exists)

The honest candidate is **prefab view generation** (`prefab_views.py`): "build me
a view over runs filtered by skill and cost" → A2UI emits a filter form bound
to a data model → the user's interaction returns the model → the agent revises
the view. That is genuinely bidirectional and genuinely awkward in Markdown.

Candidates I would argue against:
- **Apps system overview** — deterministic data we already have. Agent-generated
  UI adds latency and a spoofing surface for no gain.
- **Run/DAG inspection** — the xyflow graph is better than anything A2UI could
  emit, and we already have it.

---

## The blocker, stated plainly

A2UI's own documentation says to treat agent output as untrusted, and names the
attacks: **phishing** (spoof a legitimate interface), **XSS** via property
values, and **DoS** via pathological layout complexity.

Aria currently has **no authentication, no CSRF defence, and an open SSRF
hole**. Adding agent-controlled UI to that is not a UI decision, it is a
security decision with a UI shape. So Stage 3 is gated on the auth work, not on
the protocol work. That ordering is deliberate.

---

## What Electron changes

Aria as a desktop shell is the strongest argument yet for doing this properly.

**In our favour:**
- **Loopback-only binding plus a per-launch token in a header** resolves most of
  the auth decision that currently blocks Stage 3. This is the highest-value
  thing Electron buys us and it is not a UI feature.
- **A2UI's real strength is multiple surfaces**, and a desktop app can put
  different surfaces in different OS windows — a part viewer while chatting
  elsewhere. That is the scenario the format was designed for and the web
  console cannot express.

**Also relevant:**
- **Monaco becomes the answer for the Code section.** Monaco is VS Code's
  editor core, MIT, browser-native. It would replace the overlay-plus-textarea
  work wholesale and give *real* multi-cursor, language services, and folding.
  The thing we hand-built last round becomes throwaway — which is fine, because
  it was built as a stub. Expect ~half a day of Vite worker wiring
  (`MonacoEnvironment.getWorkerUrl`).
- Native file dialogs, native menus with real accelerators, and OS
  notifications each remove a workaround.

**Harder, not looser:**
- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, strict
  CSP, and every IPC channel validated. With `nodeIntegration` on, an A2UI
  payload is a path to Node. A2UI's "no executable code" guarantee is about the
  *protocol*, not about the host process.

---

## Non-goals

- No model-generated markup, HTML, or component code rendered in the console.
- No `eval` of agent output.
- No `@ag-ui/client` rewrite of the console.
- No A2UI surface before the auth work lands.
