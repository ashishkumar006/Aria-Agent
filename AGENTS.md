# Aria — agent instructions

## Layout
- `S9SharedCode/code/` — agent server (`agent_server.py`), skills, executor (`flow.py`),
  React console (`console-frontend/`). Run agent commands from this dir.
- `llm_gatewayV9/` — LLM gateway (own venv). Run gateway commands from this dir.
- `agent-desktop/` — Electron shell. `build_desktop.ps1` (repo root) builds the
  portable app at `agent-desktop/dist/Aria/Aria.exe` and its shortcuts; the
  shell drives the same `:8500` console, it is not a second backend.

## Services (restart to pick up backend changes)
- Agent: `http://127.0.0.1:8500` — start with `uv run agent_server.py` in `S9SharedCode/code`
- Gateway: `http://127.0.0.1:8109` — start with `uv run main.py` in `llm_gatewayV9`
- Shortcut: `powershell -ExecutionPolicy Bypass -File start_aria.ps1` starts both
  (re-runs reuse whatever is already listening, waits for health, opens the console;
  `-NoBrowser` skips the browser). `stop_aria.ps1` stops the listeners on both ports.
- To restart: kill the listener on the port, then start fresh. Never assume a
  restart happened — verify with `/api/health` (agent) and `/v1/providers` (gateway).

## Verify loop (run after every backend/frontend change)
- Agent tests: `uv run python -m pytest tests/ -q` from `S9SharedCode/code`
  (NOT bare `uv run pytest` — the shim resolves the wrong interpreter and
  fails on `numpy`). Full suite ≈ 5–6 min; targeted files are fine.
- Gateway tests: `uv run python -m pytest -q` from `llm_gatewayV9` — NOT
  `tests/`. 14 of the 16 adaptor suites, plus `policy/` and
  `integrations/`, live outside `tests/`, so `pytest tests/ -q` passes
  happily while skipping 344 tests. Full suite ≈ 1.5 min.
- Frontend: `npx tsc --noEmit` then `npm run build` from `console-frontend/`.
- Live: `GET /api/health` must show `gateway_up: true`.

## Standing rules
- NEVER read `.env` files (enforced by `.opencode/plugins/env-protection.js`).
  Flip flags with blind byte-replacements and verify with a single-line grep.
- PowerShell 5.1: `;` chains commands (no `&&`); quote paths with spaces;
  `>` writes UTF-16 — prefer `Set-Content`/atomic writes via Python.
- Inline `python -c` breaks on quoting — write temp scripts under the system
  temp dir instead, delete when done.
- Scratch E2E/visual scripts (`*.cjs`, temp `.py`) live outside the repo and
  are deleted after the run they verify.

## Studio (`/studio`) — what is where
- The coding workspace: `views/studio/{Studio,AgentPanel,Composer,Explorer,
  ReviewEditor,Sessions,Dock,GitPanel,SearchReplace,BrowserPane}.tsx`,
  session state in `lib/studioSession.ts`.
- Backend modules, one concern each: `studio_workspace.py` (sessions/roots),
  `studio_review.py` (baselines, content-fingerprinted hunks, checkpoints),
  `studio_runs.py` (run registry + JSONL transcripts + replay),
  `studio_composer.py` (`@` mentions, `/` commands, commit messages),
  `studio_files.py` (file ops, search/replace plans, git argv — planners only,
  it never writes), `browser_session.py` (the session's persistent Chromium).
  `agent_server.py` owns the routes and the path vocabulary.
- **The code agent's system prompt is inline in `agent_server.py`**, not in
  `prompts/`. Editing `prompts/coder.md` changes nothing for the IDE agent.
  The prompt directory is `prompts/`; the tool payload is built by
  `skills.tool_payload`, and `tests/test_studio_prompt_truth.py` fails if the
  prompt names a tool the payload does not carry.
- Default code workspace is `state/code_workspace` (an empty scratch folder),
  and with a studio session bound it is that session's own roots — never the
  process default.
- Switches live in `flags.py`, not `agent_config.yaml`: `ui.code_ide`
  (off = the old `/code` page, untouched), `browser.tools` (off = no browser
  verb in the agent's payload and the pane routes answer `disabled`),
  `browser.headed` (visible Chromium on the next browser a session starts).
- `TEST.md` in `S9SharedCode/code` is the studio's verification checklist —
  read it before changing a studio surface, and add to it when you add one.
