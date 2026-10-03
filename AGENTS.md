# Aria — agent instructions

## Layout
- `S9SharedCode/code/` — agent server (`agent_server.py`), skills, executor (`flow.py`),
  React console (`console-frontend/`). Run agent commands from this dir.
- `llm_gatewayV9/` — LLM gateway (own venv). Run gateway commands from this dir.

## Services (restart to pick up backend changes)
- Agent: `http://127.0.0.1:8500` — start with `uv run agent_server.py` in `S9SharedCode/code`
- Gateway: `http://127.0.0.1:8109` — start with `uv run main.py` in `llm_gatewayV9`
- To restart: kill the listener on the port, then start fresh. Never assume a
  restart happened — verify with `/api/health` (agent) and `/v1/providers` (gateway).

## Verify loop (run after every backend/frontend change)
- Agent tests: `uv run python -m pytest tests/ -q` from `S9SharedCode/code`
  (NOT bare `uv run pytest` — the shim resolves the wrong interpreter and
  fails on `numpy`). Full suite ≈ 5–6 min; targeted files are fine.
- Gateway tests: `uv run python -m pytest tests/ -q` from `llm_gatewayV9`.
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
