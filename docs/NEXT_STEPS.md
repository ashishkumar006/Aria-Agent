# Aria — next steps / work plan

Status as of 2026-11-19. This file captures what is **done**, what is **left**,
and the **decisions** behind them, so the remaining work can be picked up later
without re-deriving it.

---

## Done (this pass)

- **Frontend test fixes** — `/architecture` added to `prefetch.ts` and the two
  `ROUTES` invariant tests; `docsource.panel.test.ts` now expects `pptx → slides`.
  `tsc`, `vitest` (241/241) and `npm run build` are green.
- **Repair routing** — `_repair_json_with_model` tries **kilo → gemini**. The
  gateway treats a provider pin as a hard binding (`router.candidates` returns
  one entry), so the fallback is explicit in the agent. Tests in
  `tests/test_json_repair.py`.
- **Images** — the author-stage per-section pre-pass now uses short keyword
  queries (the full section heading returned 0 from Openverse), keeps a figure
  when the vision check is unavailable, clears the per-run topic cache, dedupes
  URLs, and runs in `run_skill` so single-call documents get figures too.
  `width_mm` sizing documented. Verified live: 4-page solar PDF = 1.02 MB with
  3 embedded images.
- **LaTeX engine** — `_prepare` now forces `typeset` onto the real
  `render_document` call (it was only forced on the sectioned/fallback paths);
  `_latex_requested_in` forces the engine when the user explicitly asks (the
  planner left `metadata.engine` empty in a live run). Page-count regex accepts
  `20-page` (was `20 pages` only). Verified live: **15-page, 277 KB, xdvipdfmx /
  LaTeX with hyperref, 8,782 words**.
- **Authoring run log** — coloured statuses (green complete / amber running /
  red failed / grey skipped), 12.5px font, per-node duration, live stopwatch,
  upsert so running→final updates in place.
- **Authoring receipt** — “What this run did” (nodes + durations) and “Sources
  checked” (deduped links) above the answer. `/produced` now returns per-node
  `duration` and `sources`.
- **Composer** — always-visible **Plan / Build** segmented control (replaces the
  Ask/Agent dropdown); model picker already lists free-first with cost labels.
- **Architecture** — real **SVG request-path diagram** (clickable boxes,
  arrows, data-plane connectors) above the walkthrough.

---

## OpenCode GUI — what it actually is (reference)

OpenCode Desktop is **not** a clean chat box. It is an Electron app with a
client/server model (its own background server + a SolidJS client), one of four
surfaces (desktop, TUI, web, mobile/SDK) over the same agent.

### Surfaces
- **Multi-session tabs** — each session is a tab; **parallel agents** run at
  once; jump between them as they finish.
- **Built-in browser pane** beside the agent — the app opens live next to it.
- **Side panels** (files, review/diff, browser, plugin panels) that can be
  **dragged to a bottom pane**; tabs close and reopen (`⌘⇧T`).
- **Diff viewer with line comments** — select code → “Add comment”.
- **Command palette** + **Jump to message** (`⌘⌥G`).
- **Recent sessions sidebar** — cross-server recency, pins, reorder, 7-day/30-row.
- **Settings → Extensions/Plugins/Providers** — plugin updates, provider
  accounts, model picker with a keyboard-navigable **variant flyout** (favorites,
  budget/reset).
- **Workspaces** — Git worktrees: each is an isolated copy on branch
  `opencode/<name>`; `⌘⌥O` chooses a worktree, `⌘⌥B` its base branch.
- **Cross-device** — desktop, terminal, phone.

### Agent model
- Agent = **prompt + model + permissions + mode** (`primary`/`subagent`/`all`)
  + `steps` + color. Built-ins: **Build, Plan, General, Explore**.
- Subagents run in **child sessions with fresh context**.
- **Permissions**: ordered `{action, resource, effect}` rules
  (`allow`/`ask`/`deny`), wildcards, a shell scanner (Bash/Zsh/POSIX/PowerShell),
  external-directory boundaries, approvals (`once`/`always`/`reject` with saved
  project-scoped patterns), and hard-deny policies.

### Extensibility
- A full **GUI plugin SDK** (`@opencode/plugin/gui`): plugins contribute panels,
  commands, settings pages, slots, titlebar items, composer pills, code-block
  renderers, with typed IPC/RPC. The UI is a plugin host, not a fixed app.

---

## Remaining work

### 1. Studio — workspaces (Git worktrees)  *(medium, highest value)*
The signature OpenCode feature and the biggest gap.

- **Backend** — `studio_files.py`: git argv planners for `worktree list`,
  `worktree add -b <branch> <path>`, `worktree remove`, and `switch`.
  Worktree dirs live beside the repo (e.g. `../<repo>-<name>`), branch
  `opencode/<name>`.
- **Frontend** — `GitPanel.tsx` / `Studio.tsx`: a “Workspaces” section listing
  worktrees, a “New workspace” action, and a switcher. A session records which
  worktree it runs in; new sessions default to the active one.
- **Verify** — create a workspace, run a session in it, edit, switch back, merge;
  `tsc` + `vitest`; live in `/studio`.

### 2. Studio — parallel sessions as tabs + side-panel dock  *(medium)*
- Surface parallel runs as **session tabs** (OpenCode-style) rather than only a
  rail.
- Make the Dock’s panels draggable between side and bottom (OpenCode behavior).
- Add **per-line comments** to `ReviewEditor.tsx` (select → comment).
- Add **Jump to message** (`⌘⌥G`) to the command palette.

### 3. Studio — visual alignment to OpenCode  *(medium)*
- Type: 12.5–13px UI, 11px meta; `font-mono` for code/paths.
- Borders: 1px `white/10`; remove gradients/large radii in chrome.
- Diffs: red/green line backgrounds with hunk headers.
- Reduce chrome; keep the rail-free workbench and right activity bar.
- Files: `views/studio/*.tsx`, `index.css`.
- **Verify** — screenshots at the desktop window width (not 598px).

### 4. One-command installer  *(larger)*
The ARIA_VS_HERMES doc’s #1 gap.

- Add `install.ps1` (Windows), `install.sh` (macOS/Linux) at the repo root.
- Steps: check Python 3.11+ and Node 20+, create `.venv` in `S9SharedCode/code`
  and `llm_gatewayV9`, `uv sync`, `npm install && npm run build`, then
  `start_aria.ps1`.
- Note the TeX engine is optional (docgen falls back to ReportLab).
- **Verify** — fresh clone into a temp dir, run the installer, hit
  `/api/health` (`gateway_up: true`).

### 5. Auto user-model + shortfall → skill improvement  *(larger)*
Aria already emits a machine-readable `shortfall` block; Hermes closes the loop.

- **User model** — a `user_model` drawer updated from corrections, injected into
  prompts (not a replacement for explicit memory).
- **Skill improvement** — when `shortfall.report_to_planner` is true, propose a
  concrete prompt/tool edit (a review queue, **not** auto-applied).
- Files: `memory/`, `skills.py`, `flow.py`, `prompts/`.
- **Verify** — unit tests + a live run with a forced shortfall.

### 6. Follow-ups / small gaps
- `prompts/author/deck.md` never mentions `callout` (the native fragment does).
- `_WANTS_FIGURES_R` only fires when the request names figures/images/pictures/
  diagrams. Consider a flag for “always fetch figures”.
- Single-call author image path: add a test that a non-sectioned document gets a
  FIGURES AVAILABLE list.
- The receipt’s “what it did” could also list produced files + word counts.

---

## Decisions recorded

- **Author format** — one `author` skill (plus a legacy `deck` alias); format is
  metadata; the author receives only the matching fragment; the render is
  **forced**. `_latex_requested_in` is a small deterministic backstop for when
  the LLM planner forgets `metadata.engine`.
- **Repair** — kilo first, gemini fallback (agent-side). The gateway pin is a
  hard binding; a soft preference would need a new request field.
- **Images** — author-stage per-section pre-pass (Python, vision-checked,
  bounded to 8), not researcher-stage. Researchers keep their tools for explicit
  “show me a picture” requests.
- **Cost** — free-model-first routing; surface “spent on free models” in the
  ledger. Hermes’ paid WhatsApp/keys are a cost Aria structurally avoids.

---

## Verification commands

```sh
# frontend
cd S9SharedCode/code/console-frontend
npx tsc --noEmit
npx vitest run src
npm run build

# agent (≈5–6 min; not bare `uv run pytest`)
cd S9SharedCode/code
uv run python -m pytest tests/ -q

# gateway (use `pytest -q`, not `tests/`)
cd llm_gatewayV9
uv run python -m pytest -q

# live
powershell -ExecutionPolicy Bypass -File start_aria.ps1 -NoBrowser
curl -s http://127.0.0.1:8500/api/health   # gateway_up: true
```