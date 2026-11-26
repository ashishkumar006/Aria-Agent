# Aria Code Studio — design spec

**Status:** proposed, not implemented. **Route:** `/studio` (full-page, no app sidebar).
**Flag:** `ui.code_ide` — one master switch; off leaves `/code` byte-for-byte as it is today.
**Owner decision inputs:** per-session multi-root workspace · persistent + parallel agent
sessions · in-editor review with Monaco `DiffEditor` and inline hunk accept/reject · new
route with `/code` redirecting when the flag is on.

---

## 1. What this is, in one paragraph

A coding surface you open inside Aria and *stay* in: a real editor with buffers you can
type into, a file tree of the folders you pointed it at, a terminal that is actually yours,
and an agent panel where you hand it a task, watch it work step by step, and read the
changes it made **in the file, where the code is** — accepting or rejecting hunk by hunk
before anything lands. Several agents can be working in the same workspace at once; the
tab you started one from is not the only place it survives — close it, walk away, reopen
the session and the transcript, the diffs and the cost are still there.

It is deliberately not "the Code page with more panels". The reference shape is Cursor's
unified workspace (agents as the primary object, the interface organised around *work the
agent produced* rather than around the tool) and OpenCode's four-zone discipline (messages,
input, status, sidebar; `@` for file references; `/`-commands; stacked diffs; a leader key
for agent actions; undo as a first-class verb).

---

## 2. What already exists (do not rebuild it)

Verified against the current tree, with the file that proves it.

| Capability | Where it lives today |
|---|---|
| **Editable Monaco**, not a viewer | `console-frontend/src/components/MonacoEditor.tsx:281` (`readOnly: false`), per-file `createModel`/`setModel` (345-348), `setModelLanguage` (355), real markers via `setModelMarkers` (366), minimap (293) |
| Save writes back with conflict guard | `api.codeWrite(..., {confirm:true, mtime})` (`Code.tsx:1888`); 409 on mtime drift (`agent_server.py:7297`); atomic write + `STATE_DIR/code-backups` |
| **Real PTY terminal** | `@app.websocket("/api/terminal")` (`agent_server.py:371`) → `terminal.py:52 PtySession` (pywinpty); `@xterm/xterm ^6.0` already wired in `TerminalDock.tsx` with `addon-fit` + `addon-webgl` |
| Agent tool loop with rich frames | `POST /api/code/agent` (`agent_server.py:7683`); frames `status, tool_call, tool_result, plan, permission, write, thinking, delta, verify, done, error` (`_sse`, 14220); `_CODE_AGENT_MAX_HOPS=0` (no ceiling, since 2026-11-07 — the user removed the 60-hop cap), `max_tokens` default 8192 per hop (gateway clamps to the provider's window), context cap 600k (6170), verify-fix rounds 2 (6185) |
| 17 coding MCP tools the agent can call | `mcp_server.py`: `read_file` 1807, `list_dir` 1862, `search_files` 1599, `create_file` 2422, `update_file` 2448, `edit_file` 2459, `delete_file` 1580, `make_dir` 2438, `run_command` 2109, `run_checks` 2013, `git_inspect` 2192, `document_symbols` 2306, `find_references` 2324, `blast_radius` 2346, `preview_edit` 2366, `apply_symbol_edit` 2398, `find_symbol` 2239 |
| Per-hunk revert, keep, revert-all | `GET /api/code/agent/changes` 7448, `POST …/keep` 7464, `…/revert-hunk {path,hunk}` 7608, `…/revert` 7644 |
| Gutter change marks from the last agent edit | `MonacoEditor.tsx:406-438` (`aria-agent-{add,mod,del}-gutter`), `lib/diffMarks.ts` |
| Symbol intel incl. preview/apply | `POST /api/code/intel` 7121 (`definition|hover|rename|symbols|references|blast|preview|apply`) |
| Test/lint runner | `POST /api/code/checks` 8574 (pytest / tsc / npm / ruff / cargo / go) |
| Approval gates for writes + exec | `_CODE_WRITE_TOOLS` 5873, `_CODE_EXEC_TOOLS` 5882, `POST /api/code/permission/{pid}` 7408 |
| Palette + quick open | `Code.tsx:793/803` (Ctrl+P, Ctrl+Shift+P, `cmdk`) |
| Split layout, drafts, zen | `agentW` drag 1081, localStorage drafts `aria.code.drafts.v1` 72/905, `zen` 924 |
| Virtual list library, installed, **unused** | `@tanstack/react-virtual ^3.14.20` — zero uses in `src/` |

## 3. What does not exist (the actual build)

1. **No sidebar-free mode.** `Code.tsx:2037` renders `<Rail/>` unconditionally; `zen` hides
   the top bar and tabs, not the Rail. `App.tsx` has no `<Routes>` — it maps `SECTIONS`
   (136-152) with one `SUB_SECTIONS` entry (`/authoring/focus`, 163-165) and the *view*
   itself decides focus from `pathname` + a flag (`Authoring.tsx:234-252`). That is the
   pattern to follow, and it is the one my own rule enforces: a "mode" means its own route,
   with the other chrome gone — not a re-skinned tab.
2. **No `DiffEditor`.** `monaco.editor.createDiffEditor` appears nowhere in `src/`. Review
   lives in a separate `ChangesRail` with hand-rolled `splitHunks`; there is no
   side-by-side/in-editor merge and no accept/reject where you read the code.
3. **The workspace root is a process global.** `_CODE_BASE`/`_CODE_ALLOWED` (5754-5755);
   `POST /api/code/workspace` re-roots *everything*, including a running agent, and clears
   `_CODE_REVIEW` (6783). Two tabs cannot hold two projects. There is no per-request root.
4. **No code sessions.** No transcript persistence, no restore, no resume, no list. A
   refresh loses the run. `/api/code/agent` accepts a `session` string but nothing reads it
   back. Consequently no parallel agents — one run, in one tab, or nothing.
5. **Checkpoints are memory-only.** `_CODE_REVIEW` is a 200-entry dict (6059-6060), wiped on
   workspace switch and on restart. No named checkpoints, no per-turn undo, no "back to
   before that task".
6. **No human file operations** beyond write/newfile: no rename, move, delete, or
   create-directory route. The agent can `delete_file`; you cannot.
7. **No project-wide find-and-replace.** `/api/code/search` is read-only substring, no
   regex; replace is per-buffer inside `FindWidget`.
8. **No cost or elapsed readout** in the agent panel — tokens arrive on `done`
   (`Code.tsx:2756`) and are not surfaced; `duration_ms` exists on the frame and is dropped.
9. **No staleness detection** other than a manual banner (1117) + save-time 409. No watcher.
10. **Unvirtualized** Explorer/Palette against `_CODE_MAX_FILES = 4000` (6604).
11. **No git write verbs** — `status|diff|log` only, explicitly (7083).

Two adjacent truths worth recording because they will otherwise surprise the build:
`AGENTS.md` calls the prompt directory `promots/`; the real directory is `prompts/`. And
`/api/code/agent`'s system prompt is **inline** at `agent_server.py:7851` — it does not read
`prompts/coder.md`, which is only used by the DAG `coder` skill. A prompt-truth change made
to `coder.md` would not touch the IDE agent at all.

---

## 4. The interface

### 4.1 Grid

```
┌────┬───────────────┬─────────────────────────────────┬──────────────────┐
│ A  │  Explorer     │  Editor area                    │  Agent           │
│ c  │  (multi-root, │  ┌ tabs ┐ ┌ breadcrumbs ┐       │  ┌ sessions ┐   │
│ t  │  virtualised)│  │ buffer│  Ln:Col · lang │      │  │ transcript│  │
│ i  │              │  └───────┘ └──────────────┘      │  │ tool cards│  │
│ v  │              │                                  │  │ plan ☐☑☐  │  │
│ i  │              │   Monaco / DiffEditor            │  │ composer  │  │
│ t  │              │   + inline hunk accept/reject     │  │ + context │  │
│ y  │              │                                  │  │  chips    │  │
│    ├──────────────┴─────────────────────────────────┴──────────────────┤
│    │  Dock:  Problems │ Terminal │ Output │ Checks                     │
│    ├───────────────────────────────────────────────────────────────────┤
│    │  Status: roots · branch · ⚑2 agents · 41k in/3.2k out · $0.041 ·   │
└────┴───────────────────────────────────────────────────────────────────┘
```

* **Activity strip (A)** — 40 px, icons only: Explorer, Search, Source Control, Agents,
  Files, Checks, Terminal, Settings. It replaces the app Rail entirely; there is no path
  from `/studio` back to the Rail except the browser's own back, plus one explicit
  "exit studio" affordance in the status bar. That isolation is the point you asked for:
  nothing else in Aria is on screen while you are coding.
* **Three resizable columns + a docked bottom panel.** One resizer implementation, used by
  all four splits, persisted per browser (`aria.studio.layout.v1`). The hand-rolled pointer
  drags (`Code.tsx:1081`, `Authoring.tsx:357`) get replaced by that one helper so the
  behaviours (keyboard nudge, double-click to reset, min/max clamps, `role="separator"`)
  exist once instead of three times.
* **Density** follows the console's existing rule: 11.5–12.5 px type, tight rows, no
  blocking modals. Anything that would interrupt a keystream becomes an inline strip or a
  toast.

### 4.2 Editor column

* **Tab strip** with *preview* (italic, single-click, replaces itself) vs *pinned*
  (double-click) tabs, dirty dots, close-others/right-click menu, `Ctrl+Tab` cycling, and
  "keep open files when the tab closes" restored from localStorage. Tabs are buffers, not
  routes: the URL carries only the workspace + focused file, so a refresh reopens the
  session and re-opens the buffers it had.
* **Two modes per buffer**: `source` (Monaco editor) and `review` (Monaco `DiffEditor`,
  readonly original, editable modified). Opening a file the agent changed lands in `review`
  with the hunk widgets; `Esc` or "source" returns to the plain editor.
* **Inline hunk accept/reject** in the diff view, plus a hunk navigator (`F8` /
  `Shift+F8`) and `Cmd/Ctrl+Shift+Z` = "undo the agent's last change to this file" (maps to
  the existing `revert-hunk`/`revert`, not to the editor's own undo stack — those are
  different verbs and both need to be labelled distinctly so nobody loses work by guessing).
* **Agent marks stay**: the gutter marks from the last agent edit remain the "what changed
  since you last looked" cue, and become the same model that drives review hunks rather
  than a parallel decoration scheme.
* Breadcrumbs show `root › dir › file` with the *root name* first, which is only meaningful
  once multi-root exists.

### 4.3 Agent column

This is the primary object, per Cursor's framing — you manage agents, not keystrokes.

* **Sessions header**: a row of live/paused runs (`⚑2 agents` in the status bar opens it).
  Each session shows workspace roots, model, state (`running / waiting for approval /
  finished / stopped`), elapsed, and cost. New session, duplicate, rename, archive.
* **Transcript** with:
  * the **plan checklist** (already streamed as `plan` frames) pinned at the top while the
    run is live, collapsing to a summary when done;
  * **tool cards** paired by `id` (`tool_call`→`tool_result`) showing arguments, duration,
    a one-line outcome and an expandable body; a `run_command` card carries exit code and
    stdout/stderr folded, with **"open in terminal"** to replay in the live PTY;
  * **write cards** that link to the buffer's review mode rather than rendering a second
    copy of the diff;
  * **approval cards** inline (never a modal) for `permission` frames, with
    approve / approve-and-remember / refuse, mirroring `_CODE_WRITE_TOOLS` and
    `_CODE_EXEC_TOOLS`;
  * **thinking** capped and collapsed, **delta** streamed text.
* **Composer**: `@` file/folder references (OpenCode's convention, and we already have the
  tree to complete against), `#` to pull a problem/test failure into context, `/` commands
  (`/new`, `/stop`, `/undo`, `/checks`, `/commit-msg`, `/roots`), model picker, Ask ⇄ Agent
  toggle (Ask = read-only, which already maps to `read_only` on the request), and the two
  approval switches (`auto_approve`, `auto_approve_exec`) surfaced as one visible state
  rather than buried flags. Context chips show what the agent is *actually* being given —
  paths, line ranges, hop count used / cap 60, and context chars / 600k.
* **Stop** uses the cancellation contract that went in today: it aborts at the next hop
  rather than at the end of the run.

### 4.4 Dock

`Problems` (markers per file, grouped, jump-to), `Terminal` (xterm, multiple shells,
each in the *session's* root; agent-invoked commands visible as a read-only transcript
alongside your own interactive shell), `Output` (raw agent log), `Checks` (`/api/code/checks`
per root, with re-run). Collapsible, resizable, remembered.

### 4.5 Status bar

Workspace roots · branch + dirty count (`git_inspect status`) · live agent count ·
`in/out tokens` + **cost** + elapsed · `Ln:Col · language · indent` · flag state.

### 4.6 Keyboard

`Ctrl/Cmd+P` quick open · `Ctrl+Shift+P` command palette (extended with every studio verb)
· `Ctrl+B` explorer · `Ctrl+J` dock · `Ctrl+I` focus composer · `Ctrl+Enter` run task ·
`F8`/`Shift+F8` hunk · `Ctrl+S` save · `Ctrl+Shift+B` run checks · `Esc` leave review.
Plus an OpenCode-style leader (`Ctrl+X`) for agent verbs, so `Ctrl+X Ctrl+U` undo-last-agent-change
and `Ctrl+X Ctrl+C` compact the transcript — advertised in the palette, never required.

---

## 5. Backend work

### 5.1 Per-session, multi-root workspace (the structural change)

Today `_CODE_BASE`/`_CODE_ALLOWED` are module globals read by `_code_abs` (5803). The fix is
not a new endpoint — it is removing the global:

* A **workspace descriptor** `{sid, roots: [{id, path, name}], created}` stored under
  `state/code-studio/roots/<sid>.json`.
* **Every** code route takes `?sid=` (or a header) and resolves confinement from *that*
  session's roots. `_code_abs(sid, path)` returns `(abs, root_id)` or refuses.
* The MCP child receives `ARIA_SANDBOX` per run as it does now (`mcp_runner.py:1029-1033`),
  plus `ARIA_ROOTS` so `list_dir`/`search_files` can address several roots by name.
* Confinement rules, stated and tested: realpath after resolving symlinks; the resolved path
  must be inside one declared root; `..` segments rejected before resolution; a root that
  disappears mid-session fails closed rather than falling back to the process default;
  no route accepts an absolute path from the client as a root without the explicit
  picker (`POST /api/code/workspace` becomes `POST /api/studio/roots` and validates
  existence + readability first).
* **Back-compat:** `sid` absent ⇒ the legacy global root, so `/code` and every existing
  caller is untouched when the flag is off.

### 5.2 Persistent sessions + parallel runs

* `state/code-studio/sessions/<sid>.json` — ordered events (user, plan, tool_call,
  tool_result, write, approval, delta, done/error), tokens/cost, roots, model, state.
  Written by the run, not by the client, so a closed tab cannot lose a turn.
* A **run registry** mirroring the DAG one: `_STUDIO_RUNS[sid] = {task, cancel_event}`,
  `/api/studio/sessions/{sid}/cancel`, and the same "abort at hop boundary" contract.
  Concurrent runs are allowed across sessions and *within* one session up to a stated cap;
  two runs editing the same file are permitted but the second write goes through the
  mtime-conflict path rather than silently clobbering.
* **Watcher**: `GET /api/studio/sessions/{sid}/stream` replays persisted events then follows
  live. N tab watchers, one run. Reconnect is the normal case, not an error path.
* Cost/elapsed come from the same gateway per-session delta the DAG path already uses, so
  the readout is real spend, not estimated tokens.

### 5.3 Review model (needs hunks to be addressable)

`revert-hunk` takes `{path, hunk}` where `hunk` is today a positional index, which is only
stable while nothing else changes. In-editor accept/reject makes hunks *clickable after
other edits land*, so:

* A review entry gains `hunk_id` = fingerprint of (context-before, removed lines, added
  lines, occurrence index). Stable under re-open, collision-checked, and mismatch on apply
  returns `409 hunk-moved` rather than silently reverting the wrong region.
* Endpoints: `GET /api/studio/changes?sid=` (per-file, per-hunk, with +/- counts and status),
  `POST …/changes/apply` `{hunk_id, action: keep|revert}`.
* Checkpoints become **persisted and named**: one per agent turn ("before task", "after
  task N") in `state/code-studio/checkpoints/`, with `POST …/checkpoint/restore`. The
  200-entry in-memory dict stops being the system of record.

### 5.4 File operations, search, terminal

* `POST …/file/rename`, `…/move`, `…/delete`, `POST …/mkdir` — same confinement, same
  backup-before-destructive discipline as `codeWrite`, and each reflected in the tree.
* `POST …/search` gains regex + include globs + context lines and returns per-file hit
  counts; `POST …/replace` runs **dry-run first** (`{preview: true}` returns counts and the
  affected files) and requires a second call naming the files it may touch.
* Terminal: session shells spawn in the session's root; agent `run_command` output is
  mirrored into a per-session scrollback so "open in terminal" is a view, not a re-run.

### 5.5 Prompt truth

`/api/code/agent`'s inline system prompt (`agent_server.py:7851`) must describe what the IDE
agent actually has: the declared roots (by name, not a guessed path), that it is one of N
concurrent sessions, that writes are reviewed hunk-by-hunk by a human rather than trusted,
which tools exist, and the hop/context ceilings. Every capability claim gets checked
against the real tool payload — this is the fourth layer of the grant problem and it has
burned Aria three times already.

---

## 6. Slices

Each slice is independently verifiable and each keeps `/code` working until the last one.

| # | Slice | Ships | Status |
|---|---|---|---|
| 1 | **Session + root store.** `sid` threaded, `_code_abs(sid, path)`, `POST /api/studio/roots`, per-session roots, legacy fallback intact | multi-root without UI yet | shipped |
| 2 | **Studio shell.** `/studio` route, no Rail, activity strip, 4-way resizer + persisted layout, `SUB_SECTIONS` mapping, `ui.code_ide` flag + `/code` redirect | the isolated container | shipped, then **redrawn** — see §11 |
| 3 | **Buffers.** Tab strip (preview/pinned/dirty/close-menu), buffer state, staleness banner, virtualized explorer and quick-open | everyday file work | shipped |
| 4 | **Review.** `DiffEditor` mode, hunk fingerprints + `/changes/apply`, inline accept/reject, hunk nav, persisted named checkpoints, `ChangesRail` retargeted onto the same model | the core of the ask | shipped |
| 5 | **Sessions & parallel agents.** persistence, watcher stream, run registry + real Stop, session header/list, replay on reopen | agents as objects | part 1 shipped (session list, naming, archive); transcripts still do not persist |
| 6 | **Composer & transcript.** `@` references, `/` commands, `#` context, context chips, ceilings readout, approval cards inline, cost/elapsed | control surface | **SHIPPED 2026-11-06** — `studio_composer.py` + `/api/studio/{mentions,commands,commit-msg,runs/{id}/cost}`; popup, chips, server-owned ceilings, ledger money. Found live: replay never ran on a cold load, and a JSX precedence bug hid the cost line |
| 7 | **Dock.** multi-shell terminal in session roots, agent-command mirroring, Problems/Output/Checks grouping | the parts that make it feel like an IDE | **SHIPPED 2026-11-06** — `views/studio/Dock.tsx`, `?cwd=` now confined by `_terminal_cwd_ok` (was `isdir` on any path), `output` frames carry the shell's own text. Dock spans the workbench, not the editor column |
| 8 | **File ops, project search & git verbs.** rename/move/delete/mkdir, regex search, dry-run replace; stage/unstage/commit/branch/stash with a pre-commit diff and confirms on anything that moves HEAD | human parity, then git parity | **SHIPPED 2026-11-06** — `studio_files.py` (planners only) + `/api/studio/{files,search,replace,git,journal}`; GitPanel, SearchReplace, explorer row ops. Found live: a 428 confirmation was thrown as an error by `pj`, so the panel could not render the diff it exists to show |
| 9 | **Browser tools + session.** `@mcp.tool()` browser verbs registered in `_TOOL_CATALOG` *and* `CODE_AGENT_TOOLS`, session-scoped context, console/network capture, waits, tabs, `browser_verify` | the agent can actually drive a page | **SHIPPED 2026-11-06** — `browser_session.py` + 17 MCP tools across all four grant layers, behind `browser.tools` (off by default). Live Chromium proves the console listener sees a JS error and that a page survives between calls |
| 10 | **Browser pane.** live frame stream (screencast, degrading to per-action screenshots and saying which), filmstrip, console/network panels, tab list, take-over, `@browser`, React consumers for the artefacts the backend already returns | you can see it | **SHIPPED 2026-11-06** — CDP screencast over SSE with a captured first frame, `/api/studio/browser/*`, `BrowserPane.tsx`, `@browser` context. Found live: an OBSERVING call (`browser_console`) launched Chromium as a side effect of looking |
| 11 | **Prompt truth + flags + docs.** inline code-agent prompt rewritten, `flags.py` definitions, AGENTS.md corrected (`prompts/`, real code root), per-slice tests | nothing claimed that isn't | **SHIPPED 2026-11-06** — `tests/test_studio_journey.py` (12 steps, one per seam, model stubbed but every write real) + `TEST.md` (the checklist, the flags, the 18-row matrix, the six standing invariants) + AGENTS.md §Studio. Found while writing it: the scripted turn was silently refused by the route's own read-before-edit guard, and two edits six lines apart are genuinely ONE hunk — the fixture had to be lengthened before step 2/3 measured anything. Mutation-checked: revert-as-whole-file and last-hop-only both go red |
| 12 | **Full-browser verification pass.** matrix below, fixes in the same pass | proof | **IN PROGRESS 2026-11-06** — rows 1-4, 9, 12-15, 18 passed live at 1512×950 with a real git repo; 7 product defects found and fixed in the pass (the write route not returning the new mtime; Problems sending a file extension as the language; Problems keyed on paths instead of content; the rail's "Open a folder…" rendering nothing; a polled route 404ing on a stale process; the git chip reading the PROCESS workspace; **and `ReviewEditor` crashing the whole view on cleanup order**). Then a visual pass over every surface: surface depth instead of alpha-diluted flats, bordered composer pills, two-line folder-named session rows, collapsed tool activity, honest `+0 −0` / `L0` / stale-entry states, a failed-buffer state instead of an empty editor, 26px tree rows, and a 127-site type sweep (10 sizes → 5). Rows 5-8, 10-11, 16-17 not yet run. Details in TEST.md §4 and §11 |

Ordering rationale: 1 has to be first (everything else hangs off `sid`), and 4 is the
biggest single risk so it lands before the polish rather than after it.

---

## 11. The shell, redrawn (2026-11-06)

The user rejected the slice-2 layout with a screenshot of Kilo Code's GUI and asked for
that: *"this is how the modern GUI's look … the one which is most comfortable to use."*
The activity strip + permanent three-column grid was a VS Code shape with 10.5px labels,
and it opened on a file tree instead of on the work.

What `/studio` is now:

* **Left column = sessions**, not files. New session, Search, `All workspaces ▾` +
  re-read, Active / Archived lists, identity + settings pinned at the bottom. Backed by
  `state/code-studio/sessions.json`, reconciled against `roots/*.json` on read so a
  session older than the index still appears. A session names itself from the first task
  typed into it.
* **Centre = the conversation.** Empty state is the mark, "What should we work on in
  `<folder>`?", and the hero composer: `+`, `Agent ⇄ Ask`, `Manual → Auto files →
  Auto files + commands`, circular send, then the chip row (folders · branch · machine).
  The same composer card is pinned under a transcript, so the thing you type into never
  moves.
* **Right = the workbench**, brought out when there is a file to look at and dismissed
  when there isn't. Buffers survive hiding. Its sections — Files, Search, Changes, Git,
  Browser, Runs — live behind a 46px icon-only activity bar on its far right (added
  2026-11-07). Clicking one opens its panel sliding in from the right; clicking the
  active one closes it; the agent opens the same panel when it has something to show.
  The divider between the panel and the editor resizes it (200–520px, persisted per
  browser as `aria.studio.drawer`; keyboard: arrows nudge, Shift+arrows move far,
  Home/End bound it). Buffers survive hiding, as before. The editor has `source ⇄ review`.
* Type scale 12.5–15px body, 24px hero, real padding. The dense 10.5px chrome is gone.

Two defects this layout exposed, both fixed and both silent before:

1. **`/api/code/agent` bound no session.** The console posts `sid` in the JSON body;
   `_studio_session_scope` binds `?sid=` only, so the run read the process workspace and
   wrote into `state/code_workspace/` while the transcript reported `edit_file ✓` on a
   file that never changed. Fixed at both ends (`?sid=` on the stream,
   `_studio_bind_body` in the route) and pinned by
   `test_an_agent_run_naming_its_session_in_the_body_works_in_that_session`.
2. **Two different answers to "which file is `s4demo/app.py`?"** The child re-anchored a
   root-named prefix only when the literal path did not exist; the server stripped the
   prefix and re-anchored at the FIRST root. So the review snapshot was taken on
   `studio-demo/app.py` (which did not exist) while the tool wrote the real file, the
   entry was then dropped as a no-op, and the change was unreviewable. One rule now, in
   both halves: a leading segment that names a declared root IS the selector, and
   existence has nothing to do with it.

And one modelling error the live run made visible: the pending set was the LAST hop's
patch, so a turn that edited two places listed "1 hunk +1 −1" and hid the other edit
behind the file's own Revert. `studio_review.pending_diff()` recomputes baseline→current
for both the listing and the apply, which is also what makes `hunk_id` the handle that
protects a click from landing on a region that moved.

**The activity bar (2026-11-07).** The labelled strip above the editor — five text
buttons plus a close — was what the user saw as clutter. It is now a 46px icon-only
activity bar on the far right, always visible and independent of the workbench: the
sections open on their own to the left of the bar, as a resizable pixel pane
(`useSplitPx`, 200–520px, persisted as `aria.studio.drawer`), without dragging the
editor, the dock and the tabs in with them. Clicking the active icon closes the
panel; the agent opens the same panel when it has something to show. The divider
is a real `role="separator"` (arrows nudge by 16px, Shift by 64px, Home/End bound
it). The workbench (editor, dock, tabs) keeps its own show/hide toggle at the
bottom of the bar. The palette was aligned to the console's neutral hex vocabulary
in the same pass. **Runs joined the bar the same day**: a ScrollText icon with a
blue live counter, opening the RUNS panel (`RunsPanel.tsx`) — this session's runs,
one click from the list into the full frame log (timeline fold + raw per-frame
view), live-following, ledger cost, stop-at-next-hop. `browser.tools` was also
flipped on in the same session, and `state/code-studio/` was wiped clean at the
user's explicit instruction ("Everything in studio").

**The session column (2026-11-07, same day).** Next to the bar sits an always-open
`Session` panel (`SessionPanel.tsx`): the selected model's real context window read
from the gateway's catalogue (no invented meter — the 600k-char elision ceiling is
gone from every surface), the project
folders/branch/unreviewed count, the approve section (`Changes waiting` + the
per-file +/− rows — moved out of the transcript where it cost the conversation
half its height), and this session's runs with live/finished/stopped/error
glyphs. Resizable 220–420px, persisted as `aria.studio.sessionpanel`. It steps
aside when any section panel opens (the two trade places) and returns when
the last section closes; at rest the studio shows conversation + session
column + activity bar. The coding
agent's default 60-hop ceiling was removed (`_CODE_AGENT_MAX_HOPS` 0 = no
ceiling; explicit positive values still clamp to 200); the composer's `hop N/M`
chip hides itself at the unbounded default and the transcript shows `N steps`
without a fake ratio. The left rail groups sessions by their first folder with a
per-folder live/finished/stopped/error state column driven by new `live`/
`last_status` fields on `/api/studio/sessions`. Rows are single lines under a
folder group. A session switch never flashes the empty hero: the panel paints
a skeleton until the run list lands (verified at 60–240 ms on a warm page), then
the replay mounts atomically. The Usage rows show everything derived from the
registry (tokens in/out, tool calls, elapsed) and from the gateway ledger for
the newest finished run (cost + cache reads/creates); a ledger that cannot be
read says `unknown`, it does not draw $0.

**The transcript reads like the product now.** File paths in answers render as
buttons that open the review surface (foldered refs directly, bare filenames via
the mentions route with one exact match required — anything else gets an honest
note in the transcript). The tool/thinking disclosure is a single expandable
"thinking…" line whose body is the tool timeline, not a second card. Streaming
batches deltas at 45ms and the view only chases the bottom when the reader is
already within 160px of it — scrolling up to reread while the agent types no
longer yanks you down.

---

## 7. Verification plan (what I will actually do, not what I will assert)

**Per slice:** unit/behaviour tests that would fail without the fix (reintroduce the defect,
watch it go red, restore); `npx tsc --noEmit`; `npm run build`; agent suite; and a live
check against the running server — never a status code alone.

**Slice 10 — browser matrix.** For every route in the studio (activity-strip views, editor
source/review, dock tabs, session list, composer states, palette, every keyboard verb) ×
every pipeline (Ask read-only, Agent with writes, Agent with `run_command`, approval
approve/remember/refuse, Stop mid-run, resume after closing the tab, two parallel sessions,
multi-root with a file that exists in two roots, a file changed on disk underneath us,
rename/move/delete, project replace dry-run then apply, checks failing then passing):
drive it in the console, record the observed result, and read the **store** as well as the
screen — the persisted session file for a resumed run, the checkpoint directory for an
undo, `gateway_v8.db` `calls` rows for the cost readout, `code-backups` for a revert.

**Honesty rules I'll apply while reporting:** I state which claims I *looked at* and which I
only measured; a background suite's completion notice is not its result, so I read the
summary line and every `FAILED` before I say anything about it; and any screenshot taken in
the in-app Browser is structural only, because that surface is hidden and cannot prove
painting.

---

## 8. Risks and how they're contained

* **Monaco `DiffEditor` bundle weight.** Studio route lazy-loads the editor chunk so the
  console's first paint doesn't grow for people who never open it.
* **PTY count under parallel agents.** Bound shells per session and total; closing a session
  reaps its shells; refuse with a stated reason rather than spawning a fourth.
* **Hunk fingerprints can move.** Mismatch returns `409 hunk-moved` and the UI re-renders
  the review; nothing "revert"s a region it can no longer identify.
* **Multi-root is a security surface.** One confinement function, one place to resolve,
  tested with symlink escape, `..`, missing root, and a root that becomes a file.
* **Two agents, one file.** mtime conflict stays authoritative; a second write to a dirty
  buffer requires an explicit resolution rather than last-writer-wins.
* **SSE fan-out** — one run, many watchers: replay-from-index + live tail, with a bounded
  backlog so a lagging tab cannot grow a run's memory.
* **Regressing `/code`.** Flag off = legacy globals and legacy view; every new route is
  additive; the redirect is the only change when it's on.

---

## 9. Decisions taken (2026-11-06)

1. **Model selection: the global picker.** No per-session model. One picker, one state,
   the same control Chat/Authoring already use; a session records which model answered it
   (that already rides on `done`) for the transcript, but nothing is configured per session.
2. **Git write verbs: yes.** `stage`, `unstage`, `commit`, `branch` (create/switch),
   `stash` are in scope, which makes §5.4 a git surface rather than an inspect-only one.
   This is a deliberate widening of what Aria can do to a repository, so it ships with its
   own gate and its own kill switch, an explicit confirm on anything that moves HEAD,
   a visible "what will be committed" diff before the commit verb runs, and every action
   in the transcript + audit log. No force-push, and no destructive verb (`reset --hard`,
   `clean -f`, branch delete of an unmerged branch) without naming exactly what is lost.
3. **Verification workspace: not yet.** I pick the fixture for the browser matrix later;
   until then "Checks" and the dev-loop verification are exercised on a synthetic project,
   and I will say so rather than implying a real codebase was used.

## 10. The Browser surface

Inventory verified against the tree, and the headline finding changes the plan:

> **The browser is a skill, not a tool.** There is no `@mcp.tool()` browser verb anywhere in
> `mcp_server.py` (70+ tools, zero browser), no `browser` entry in `_TOOL_CATALOG`
> (`skills.py:3284-4570`), and nothing in `CODE_AGENT_TOOLS` (`agent_server.py:5848-5865`).
> The capability is reachable only as a DAG node, dispatched by a bypass at
> `skills.py:4687-4745` that imports `browser.skill.BrowserSkill` directly and skips
> `render_prompt`. So **no model on any interactive surface — Chat, Code, or the Studio
> agent — can call a browser today.** This is the grant problem one layer deeper than the
> fourth: not a missing `tools_allowed`, but no tool to allow.

### 10.1 What genuinely exists (keep it, build on it)

| Asset | Where | Why it matters |
|---|---|---|
| Four-layer cascade: HTML extract → deterministic selectors → a11y → visual set-of-marks, escalating only when needed | `prompts/browser.md:1-5`; `schemas.py:203` `path: Literal["extract","deterministic","a11y","vision"]` | The right architecture, and the a11y layer *is* the Playwright-MCP "act on the accessibility tree, not pixels" design — we already built it |
| Async Playwright, Chromium, stealth-ish context | `browser/skill.py:35`, `driver.py:23`, `dom.py:23`; `skill.py:392-405` (1366x900, spoofed UA, `navigator.webdriver` hidden) | The driver plumbing works, and the browsers **are** installed here (`ms-playwright/chromium-1140/1223/1234/1248`, `playwright 1.60.0` in the agent venv, `pyproject.toml:19`) |
| Action vocabulary | `driver.py:48` `enum ["click","type","key","scroll","drag","wait","done"]`, implemented `:170-227` | A starting point for the real tool surface |
| Per-turn screenshots, raw and marked, per layer | `driver.py:339-344`, `:372-377`; disk `ROOT/state/sessions/<sid>/browser` (`skills.py:4718`) | The evidence layer already exists — it has no viewer |
| Serving + gallery + done-frame | `/api/artifacts/<sid>/<rel>` (`agent_server.py:1474-1489`, traversal guard 1483), `GET /api/sessions/{sid}/browser-shots` (`:2774-2791`), `browser_artifacts` on `done` (`:14917-14934`) | Backend finished; `console-frontend` has **zero** consumers — only legacy `web/app.js:276/573` renders it |
| Shared vision client | `browser/client.V9Client`, reused by computer-use (`computer_use/engine.py:20`), gateway `/v1/vision` (`main.py:1455`) | Do not fork: the desktop agent deliberately mirrors this cascade (`engine.py:3-5`), and it reads the OS AX tree rather than page DOM — overlap is architectural, not functional |

### 10.2 What is missing, verified

1. **No browser tool for any model.** `page.goto` is internal-only and not model-reachable
   (`skill.py:407`); `evaluate` is internal-only (`dom.py:182`). There is nothing to grant.
2. **No persistent session.** Context is created and destroyed inside one `_drive()` call
   (`skill.py:390-471`), and a *second* cold launch happens when the deterministic layer
   escalates (`:481-496`, `:533-536`). Every node and every layer pays a Chromium start; no
   login or multi-step flow can survive.
3. **Headless only** (`skill.py:130`) — the user can never see it. No live view, no stream.
4. **No console or network capture at all** — zero `page.on(...)`, no `route(...)`. Observation
   is `page.content()` (`skill.py:409`) plus the interactives pass (`dom.py:179-182`). For a
   dev-loop tool this is the critical gap: it cannot see a JavaScript error.
5. **No tabs/frames, no `wait_for_selector`, no wait-for-network** — only a model-emitted
   `wait` sleeping at most 5s (`driver.py:218-226`) and a hardcoded `domcontentloaded` goto.
6. **No record/replay** of a browser trajectory, though computer-use has exactly that for the
   desktop (`prompts/computer.md:10-11`).
7. **No gate.** No `browser.*` flag exists in `flags.py`. `TOOL_CALL_TIMEOUT_S`
   (`mcp_runner.py:213`) and the hop caps do not bind it because it is not an MCP call, so the
   only real ceiling is `wall_clock_s` metadata (90, clamped 10-600, `skills.py:4724`).
   `NODE_BUDGET_S["browser"] = 120.0` (`flow.py:74`) is dead prose for this skill, because the
   bypass never calls `render_prompt` (`skills.py:4690`) — a config entry that looks like a
   limit and is not one.
8. **No approval path covers it.** `_CODE_WRITE_TOOLS` / `_CODE_EXEC_TOOLS`
   (`agent_server.py:5873`, `:5882`) gate tools, and the browser is not a tool, so nothing asks
   before navigating somewhere.
9. **No console-frontend renderer** for screenshots that already exist on disk and are already
   served.

### 10.3 The build, revised — two slices, not one

**Slice 9 — make the browser a real tool with a real session.**

* **Session-scoped browser**: one Chromium context owned by the studio session, created on
  first use, closed on session end or explicit release, with a bound idle TTL. Reuse the
  existing context construction (`skill.py:392-405`) rather than rewriting it.
* Tools registered at **all three layers**, or they are invisible to the model that needs
  them — `@mcp.tool()`, `_TOOL_CATALOG`, and `CODE_AGENT_TOOLS`:
  `browser_open(url, goal?)`, `browser_snapshot(mode: a11y|interactive|full)` returning refs,
  `browser_act(action, ref, text?, ...)`, `browser_wait(selector?|load?|network_idle?, ms)`,
  `browser_screenshot(ref?|full)`, `browser_read()`, `browser_console(since?)`,
  `browser_network(since?)`, `browser_tabs(list|new|switch|close)`, `browser_eval(expr)`
  (approval-gated), and `browser_verify(url|dev_server, goal, expect)` returning
  `{ok, console_errors, snapshot_delta, screenshot}`.
* The four-layer cascade stays as the *implementation* behind those verbs, so `browser_open`
  on a static page still costs the cheap extract path rather than a browser launch and a
  vision call. That existing asset is what makes this build affordable rather than heroic.
* **Observability wired properly**: `page.on("console")`, `page.on("pageerror")`,
  `page.on("request"/"response")` into a per-session ring buffer that `browser_console` and
  `browser_network` read. Without it neither the agent nor you can tell "it rendered" from
  "it is broken quietly".
* **Snapshot sizing**: bounded accessibility snapshot plus the per-action delta — never raw
  HTML, never an inline image to the model, the same fold/spill treatment as any other bulk
  tool result, and `read_artifact` for the whole thing when it is genuinely needed. This is
  both the Playwright-MCP decision and the defence against rebuilding the 283k-token
  researcher problem inside the IDE.
* **A timeout that actually applies** (inside the tool, since the MCP one does not bind it),
  one flag `browser.enabled` gating both visibility and launch, and navigation off the
  local/allowlisted set routed through the same approval queue as `run_command` — an
  allowlist, not silence.

**Slice 10 — let the human see it.**

* **Live view via a frame stream** on the existing WS shape (`/api/terminal`,
  `agent_server.py:371`): CDP screencast frames into the Browser pane, degrading to per-action
  screenshots (already on disk) when screencast is unavailable — and the pane states **which**
  one you are looking at. A dark rectangle is not a viewer; that lesson is already in memory
  at the cost of a false green.
* **Filmstrip** of the last N states from the existing `turn_XX_raw.png` artefacts,
  scrubbable, with the marked variant beside it whenever the vision layer ran.
* **React consumers for what the backend already returns** — `browser_artifacts` on `done` and
  `GET /api/sessions/{sid}/browser-shots` — reusing the image panel built for the #42 fix.
* Console and network panels, a tab list, and a take-over button that opens the URL in your
  own browser.
* `@browser` as a context source beside `@file`/`@problems`/`@terminal`/`@git`: "fix what is
  wrong on this page" pulls the URL, snapshot, console errors and the last frame into the
  composer's context chips.

### 10.4 Why a coding IDE needs this

Not for scraping. For the loop that makes an agent usable on UI work:

> edit the component → start or reuse the dev server → open the page → read the console
> errors → look at the screenshot → fix what is visibly wrong → re-verify.

Today Aria can do the first step, produce a screenshot nothing in the console can display, and
drive a browser only from inside a DAG node it cannot talk to mid-turn. Without the rest, a
model asserting "the layout is fixed" has verified nothing and the user is the test harness.
Kilo names the capability outright ("Browser Use" / "automate browsers with AI"), and the
recurring complaint about driving Playwright through MCP in Cursor — "constantly losing
sessions" — is exactly what slice 9's session-scoped context exists to prevent.

### 10.5 Safety, restated for a browser rather than a file tool

* Local-first: dev-server ports and `localhost` allowed; anything else is an approval card,
  never a silent navigation. An explicit user-managed allowlist is the only widening.
* Its own browser context, never your real profile or cookies; a flow that needs a login says
  it needs one instead of quietly failing.
* Page content is **data, not instructions** — the injection-frame discipline already applied
  to fetched HTML extends to snapshots, titles, form values and `console.log` text that reaches
  a prompt.
* SSRF rules carry over: a link clicked inside a page is still resolved and rejected by address
  the way the image path does it.
* Bounded and visible: snapshot chars per action, frames kept per session, idle TTL, contexts
  and shells per session, and a per-call timeout — each shown in the UI rather than enforced
  invisibly, because an unlabelled ceiling is the thing that makes a browser tool feel cursed.

## 12. The console-wide sweep and studio polish pass (2026-11-09)

Prompted by five named complaints plus a full console test order, this pass
changed and proved the following. The complaint history matters, so this
records both: what was asked, and what it turned out to cost.

**Asked: a "+" to start a session inside a folder group.** Shipped on the
folder group's header in the session rail. First attempt bound the
sessions index's folder NAME as a path — every existing row stores names
only — and the new session opened "no folder yet" with no error to read.
Fixed by resolving the absolute path from the roots API of any session in
the group at click time; a name that cannot be resolved refuses with a
status line instead of an empty session. A second trap, from the test side
rather than the product: after a remount the rail is new DOM, and a click
dispatched on the found-but-detached node never reaches React's delegated
listener — the plus worked only once the node was re-queried after the
switch. The folder group's count pill and collapse behaviour are unchanged.

**Asked: remove the dots in the session rail.** Removed the per-row status
glyph entirely; the folder group keeps its blue live-count pill (not a
dot) and the row keeps the timestamp and the hover archive verb.

**Asked: the file tree should look like VS Code.** The Explorer now paints
file-type glyphs coloured by extension, tan folder open/closed icons,
indent guides on the same 12px ladder the rows indent by, a full-width
active-file highlight, a collapse-all verb, and its own `EXPLORER`
micro-label where the VS Code tree keeps it. The 4000-row virtualization
from before is untouched.

**Asked: clicking a file link opens "a big section engulfing the actual
agent".** Reproduced, then redesigned: a transcript file-link never opens
the workbench now. It expands an inline single-section diff inside the
answer — green added / red removed lines, per-hunk Keep/Undo
(fingerprint ids), file-level Keep/Revert, fold, close. Unchanged files
honestly render "nothing unreviewed for this file" with an open-in-editor
verb. This is a different review posture from the Monaco side-by-side
review editor, which remains the explicit "open the file" destination and
keeps its F8/Esc keyboard verbs.

**Asked: make this section (the drawer) and other sections more visually
appealing.** Both drawers and the session column now draw from one
vocabulary: accent Tile + micro-label card heads, hairline-divider section
labels, and a whisper of violet at the panel's top corner. The git
drawer was rebuilt end to end — branch chip, segmented
Changes/Log/Journal switch, VS Code SCM letters (A/M/D/U) on tinted chips,
violet stage checkbox, draft-in-input, violet commit primary, timelines in
log and journal. Branch display was wrong in two places
(`## master...origin/master`); both now parse porcelain and show `master`.

**Asked: test the entire console, every section, completely.** Sixteen
sections were driven live (Studio already examined this pass; the other
fifteen were loaded and interacted with, several with real actions and
reverted round trips). §11 of TEST.md records each: what was clicked, what
must not be changed (scheduler jobs, flags, computer control, API tokens),
and which artifacts this pass left behind. Two settings surfaces were
deliberately left alone: the feature flags (byte-flipped per the repo's
own discipline, never from a browser) and the Computer-control enable
path, which stays off on this host.

**The Authoring pipeline ran end-to-end during the sweep** and completed:
"One sentence about the Moon" → planner/retriever/researchers on the DAG →
`One-Sentence-About-the-Moon.pdf` (52 words, 201,534 tokens, "finished
while you were away · 1 file"). The artifact has no delete affordance in
the Authoring UI and remains in Recent Documents; it is the test artifact
for this pass.

## 13. The stuck-run and stale-context diagnosis (2026-11-09)

Two defects reached the user with screenshots: a run that died with
"repeated an already-successful call; final pass requested" instead of a
closing answer, and a CONTEXT meter that never moved between turns. They
were unrelated symptoms of two independent failures, both in this order:

**The wrap-up pass never ran (the error the user screenshotted).** When a
chat or agent loop re-issues a call it had already completed successfully,
the stall guard returns `needs_wrap_up` and `run_with_tools` asks the model
for one final tool-less pass. That call chain passed `cache_system` into
`_RoutedLLM.stream_chat()` — a kwarg only the batched `chat()` honors — so
the wrap-up raised TypeError **on every stalled run since the path shipped**,
and the user got the raw stall note as the "answer" every time, plus a
verify-OK caption that made it look survivable. Fixed by forwarding only
`stream_chat`'s locked signature; a regression test pins the guard
(`cache_system` may not reappear in the forwarded kwargs). The stall guard
itself and the rail-level keep-the-result behavior are unchanged — they were
correct; the closing pass was broken.

**The CONTEXT meter (and the "1 agent" footer, and the session panel's
"WORKING" pill) never cleared.** The studio's liveness reads went through
the SPA's 30 s read-through cache (`DEFAULT_TTL_MS`, keyed on the identical
URL — e.g. `/api/studio/runs?sid=st-…` — every call, all day). Any turn-end
refetch inside 30 s of the last one returned the pre-turn payload (`live=1`)
no matter what the server's current truth was, so the footer and pill
latched to "working" indefinitely and the meter's `newestFinished` never
advanced. Three layered fixes: the liveness GETs (`studioRuns`,
`studioChanges`, `studioCheckpoints`, `studioRunFrames`) now pass
`cache: 'no-store'`; `useStudioRuns.watch()` always settles the list from a
final reload when its retry windows see no live window (every gateway-503
fast fail hit this path); and every finished/errored turn now reloads the
runs list from `notePending` (the turn-end hook) instead of touching only
the CHANGES slice. A 503 before the first model call (`status: error,
input_tokens: 0`) is filtered from the meter's baseline — those attempts
know nothing about the conversation's context and formerly pinned the
caption/bar to a misleading 0%.

The gateway's 30 s cache remains in place for everything that is not a
liveness read; the agent restart to pick up `mcp_runner` is the usual one.
