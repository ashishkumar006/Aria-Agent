# AG-UI and A2UI integration map — 10

*Written by the coordinator after the assigned subagent timed out upstream twice. Verified by reading the
source; no live calls were possible (the agent on `:8500` is down). Every claim below is `file:line`-cited
or is a grep count.*

---

## Headline

**The protocols are fully implemented server-side and the console client uses none of them.**

```
console-frontend/src/**/*.ts(x)  ->  references to agui / a2ui / run_started / tool_call : 0
```

Zero. Across every file in the React app. Meanwhile the server ships a complete AG-UI event vocabulary,
a 17-component A2UI catalogue with data binding and bounds, an A2UI generation path with retry and
timeout limits, and four live routes — with the AG-UI path enabled by default.

This is an unusual and valuable position: the expensive, spec-shaped part is done and the adoption cost
is entirely on the client. It also means the trust model has **never been exercised**, which is the
subject of section 3 and the most important thing in this report.

---

## 1. What exists today

### AG-UI — `S9SharedCode/code/agui.py` (346 lines)

A complete event vocabulary, one helper per event:

| Function | Line | AG-UI event |
|---|---|---|
| `run_started` / `run_finished` | `:75` / `:79` | run lifecycle |
| `run_error` | `:89` | `AGENT_ERROR` |
| `step_started` | `:93` | per-step |
| `text_start` / `text_content` / `text_end` | `:97` / `:101` / `:108` | streamed message |
| `tool_call_start` / `tool_call_args` / `tool_call_end` / `tool_call_result` | `:112`–`:126` | full tool-call quartet |
| `activity_snapshot` | `:132` | progress/heartbeat message |
| `sse` / `content` / `new_ids` / `capabilities` | `:140` / `:149` / `:165` / `:179` | transport, id minting, capability advertisement |
| `parse_request` + `AguiRequestError` + `MAX_INPUT_CHARS = 100_000` | `:231` / `:223` / `:220` | request validation |

Routed at `agent_server.py:2240` (`POST /api/agui`), gated by the `chat.agui` flag which **defaults to
on** (`flags.py:35`, `_flag_on("chat.agui", True)` at `agent_server.py:2217` and `:2258`).

### A2UI — `a2ui_catalog.py` + `a2ui_surfaces.py` (294 lines)

`PROTOCOL_VERSION = "v0.9"`, `CATALOG_ID = "aria/catalog/v1"`, **17 components**:

```
Column  Row  Card  Divider  Spacer  Text  KeyValue  Badge  StatusRow  Empty
Progress  MetricRow  CodeBlock  DataList  Action  FilterChips  Link
```

Bounds are real and deliberate: `MAX_COMPONENTS=400`, `MAX_DEPTH=12`, `MAX_NODES_PER_PARENT=64`,
`MAX_TEXT_LEN=4000`, `MAX_TOTAL_TEXT=60000`, `MAX_ACTIONS_PER_COMPONENT=1`. There is `_resolve()` (`:427`)
for JSON-pointer data binding, `render_to_text()` (`:403`) for a text projection, `catalog_prompt()`
(`:209`) to hand the catalogue to a model, and `validate_surface()` (`:452`).

Generation (`a2ui_surfaces.py`) is bounded too: `MAX_QUERY=400`, `MAX_GENERATION_TOKENS=2000`,
`MAX_RETRIES=1`, `GENERATION_TIMEOUT_S=90.0`, a `_FENCE` regex that only accepts ```json fenced
objects, and a `GenerationRejected` exception. It calls `validate_surface` twice.

Routes: `GET /api/a2ui/catalog` (`agent_server.py:2497`), `POST /api/a2ui/validate` (`:2509`),
`POST /api/a2ui/generate` (`:2535`).

### Adoption: none

`prefab_views.py` (89 lines) does not reference the catalogue — it is a separate mechanism. So the
A2UI path has no renderer anywhere, server- or client-side.

---

## 2. Per-view map

Poll intervals below are from the break-test measurements and the client source.

| View | Today | Proposed | Protocol | Effort | Risk |
|---|---|---|---|---|---|
| `/console` | bespoke SSE frames (`started`/`log`/`meta`/`status`/`done`) from `/api/chat` | **keep bespoke**; optionally emit AG-UI alongside for external clients | none | — | Replacing this would lose the code editor and precise stream control |
| `/runs` | polls `/api/sessions` + `/api/sessions/{id}/graph`; 202 KB per full graph | **AG-UI subscription** for node state; keep bespoke DAG rendering | `step_started`, `tool_call_*`, `activity_snapshot` | 2–3 d | medium — a live DAG needs ordering guarantees the current poller gets for free |
| `/mission` | polls `/api/events` every **3 s**; server re-reads and ANSI-strips whole log files each poll | **AG-UI `activity_snapshot` stream** — this is exactly what the event is for | `activity_snapshot`, `run_*` | 1–2 d | low, and it fixes the O(log size) poll cost in report 05 F6 |
| `/scheduler` | polls `/api/schedule` every 15 s | AG-UI run lifecycle per job | `run_started`, `run_finished`, `run_error` | 1 d | low |
| `/ledger` | polls `/api/cost/by_skill` every **15 s** | AG-UI deltas per run; keep the summary table server-rendered | `tool_call_result` with cost payload | 1–2 d | low |
| `/memory` | polls `/api/memory`, debounced search | **keep bespoke** — dense table with per-drawer filters; a generated surface would be worse | none | — | — |
| `/documents` | polls `/api/documents` (currently 403-broken) | **A2UI surface** for chunk previews — `Card`+`Text`+`Badge` beats bespoke markup | catalogue | 2 d | medium |
| `/research` | bespoke result rendering | **A2UI surface** for the research report: `MetricRow`, `DataList`, `Link` for sources | catalogue | 2–3 d | medium |
| `/skills`, `/apps`, `/settings` | polls config/capabilities | **keep bespoke** — these are admin surfaces where predictability wins | none | — | — |
| `/code` | bespoke file tree + editor | **keep bespoke, and never A2UI** — see §4 | none | — | — |

**Best three adoptions:** `/mission` (fixes a real O(n) poll cost), `/runs` (live node state without
re-fetching a 202 KB graph), `/documents` chunk previews (the catalogue's `Card`/`Badge`/`Text` map
exactly onto it).

---

## 3. Trust model — read this before writing the renderer

An A2UI surface is **model-authored input rendered as UI**. Today it cannot execute because no renderer
exists. The moment someone writes `renderA2UI()`, everything below becomes live.

**What `FORBIDDEN_PROPS` already blocks** (`a2ui_catalog.py:176-181`):
```
html  innerHTML  dangerouslySetInnerHTML  src  href  url  script
onClick  onError  style  className  dangerously
```
That is a genuinely good list: it closes HTML injection (`html`, `innerHTML`,
`dangerouslySetInnerHTML`), event-handler injection (`onClick`, `onError`), asset and link exfiltration
(`src`, `href`, `url`), and both styling escapes (`style`, `className`). A catalogue that forbids
`href` and `onClick` cannot phish or exfiltrate through a rendered surface.

**What it does not block, and must be handled by the renderer:**
1. **Action semantics are unconstrained.** `Action` exists and `MAX_ACTIONS_PER_COMPONENT=1` limits how
   many, but nothing restricts *what an action may invoke*. A generated surface could ask the server to
   run any tool. **Rule: actions from a generated surface must be a fixed enum mapped to safe
   read-only endpoints, never passed through to the tool layer.**
2. **Component text is model output and will read as truth.** `Text` with `MAX_TEXT_LEN=4000` renders
   whatever the model produced. If a surface renders "Your request was approved" or a fake citation, the
   user sees chrome that looks native. **Rule: generated surfaces are visually marked as generated**
   (a border/badge, like a quote block), permanently.
3. **`DataList` + `_resolve()` JSON-pointer binding** reads from a caller-supplied `data` object. Ensure
   the binding cannot reach anything outside the surface's own payload.
4. **Volume**: `MAX_TOTAL_TEXT=60000` means one generated surface can push 60 KB of text into the DOM.
   Acceptable for one surface; a list of them is not. Cap surfaces rendered per view.
5. **The generation path takes free text and an LLM call** (`MAX_QUERY=400`,
   `MAX_GENERATION_TOKENS=2000`, `MAX_RETRIES=1`, `GENERATION_TIMEOUT_S=90.0`). It is bounded, but a
   90-second synchronous LLM call reachable from a button is a denial-of-service lever. **Rule: generate
   asynchronously with a progress state, or drop `GENERATION_TIMEOUT_S` to ~15 s.**

**A note on `href` being forbidden:** that is right for generated content, but it means a generated
surface cannot render a clickable source link. `research` is therefore better served by a *bespoke* view
that renders verified links server-side than by a generated surface with no links — a good example of
where the trust boundary should win over convenience.

---

## 4. Where A2UI would make things worse

Stated plainly, because an over-eager adoption would be a regression:

* **`/code`.** A file tree and an editor are highly interactive, stateful and keyboard-driven. A
  linear component tree is the wrong abstraction, and an agent-generated file tree is a
  *confused-deputy* waiting to happen.
* **`/console` streaming.** The bespoke frames already drive incremental rendering; replacing them with
  a generated surface would move a latency-critical loop under model control.
* **The `/runs` DAG.** A force-directed canvas with 30–180 nodes is not expressible as a bounded
  component tree without flattening it into something worse. Use AG-UI for the *events*, keep the DAG
  bespoke.
* **Any error or empty state.** `Empty` and `StatusRow` exist in the catalogue and should still be
  chosen by the server, not the model. Generated error states will confidently invent reasons.
* **Anything security-relevant.** Approval UI (computer-use) must never be agent-generated — the agent
  is the party requesting approval.

---

## 5. Worked trace

Static reconstruction from the source; **not executed**, because the agent is down. Labelled as such.

**AG-UI path** — `POST /api/agui {"threadId":"c-…","runId":"r-…","messages":[{"role":"user","content":"…"}]}`
→ `parse_request` (`agui.py:231`, capped at `MAX_INPUT_CHARS = 100_000`) →
`new_ids` (`:165`) → SSE stream: `run_started` (`:75`) → per node `step_started` (`:93`) →
`text_start`/`text_content`/`text_end` (`:97`/`:101`/`:108`) → `tool_call_start`/`_args`/`_end`/`_result`
(`:112`–`:126`) as tools run → `run_finished` (`:79`), or `run_error` (`:89`) on failure.

**A2UI path** — `POST /api/a2ui/generate {"request":"…"}` → `extract_json_object` on the ```json fence
(`a2ui_surfaces.py:48`) → `validate_surface` (`a2ui_catalog.py:452`, enforcing all six bounds and
`FORBIDDEN_PROPS`) → on failure `GenerationRejected` (`:179`); on success `surface_messages` (`:252`) /
`surface_summary` (`:278`). **Then nothing.** The console has no code to receive it, which is the
concrete end state today.

---

## 6. Phased plan

| Phase | Work | Effort | Value |
|---|---|---|---|
| 0 | Fix `/documents` and `/code` token injection (report 08 B16/finding 2 of the break-test) | 30 min | prerequisite for any UI work |
| 1 | **A2UI renderer + `GET /api/a2ui/catalog` client**, with the trust rules from §3 baked in from the first line: generated badge, action enum, no `href`/`src` | 3–5 d | Makes an existing catalogue usable; enables `/documents` previews |
| 2 | **AG-UI client subscription** for `/mission` (replacing the 3 s `/api/events` poll) | 1–2 d | Removes the O(log size) poll cost in report 05 F6 |
| 3 | AG-UI for `/runs` node state + `/ledger` deltas; keep the DAG and the table bespoke | 2–3 d | Removes 202 KB graph re-fetches |
| 4 | `/research` A2UI report **only if** a server-rendered verified-source list is kept alongside | 2–3 d | Optional; the trust cost is highest here |
| **Do not** | Generate `/code`, `/console`, the DAG, approval UI, or error states | — | see §4 |

**Recommendation:** do Phase 1 before Phase 2. The catalogue is the bigger unrealised asset, it is
already bounded server-side, and it teaches the team the trust rules while the blast radius is one
read-only preview pane rather than the whole console.