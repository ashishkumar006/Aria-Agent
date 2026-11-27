# Agent Core (`S9SharedCode/code`)

The Aria agent: the DAG orchestrator, the skill registry, the tool layer, the
web server + console API, and the specialist subsystems (computer use, browser,
Studio, sandbox). Runs on **`:8500`**. The agent owns orchestration and the UI;
it calls the gateway (`:8109`) for models, credentials, memory, and documents —
it never holds a key.

## Entry points
- **`agent_server.py`** — FastAPI app; serves the console, the per-launch token,
  and the JSON/SSE API (`/api/chat` streams `log`/`done`/`error` frames).
- **`flow.py`** — `Executor().run(query)`: the DAG scheduler, waves, recovery.
- **`skills.py`** — skill registry, `_TOOL_CATALOG`, per-node dispatch, JSON repair.

## File guide
| File | Responsibility |
|---|---|
| `agent_server.py` | HTTP server, routes, SSE chat, health (`gateway_up`). |
| `flow.py` | The executor: wave scheduling, budgets, Stop, recovery/splice, graph guards. |
| `skills.py` | Skill dispatch, prompt rendering, tool payload, `_TOOL_CATALOG`, `_JSON_CONTRACT_SKILLS`, JSON repair, `CHANNEL_TOOLS`, `effective_tools`. |
| `recovery.py` | `classify_failure`, `plan_recovery`, critic verdict handling. |
| `sandbox.py` | Resource/process boundary: `run`, `run_python`, `clean_env` (Job Object). |
| `gateway.py` | V9 client (`LLM().chat/embed`, `ensure_gateway`). |
| `mcp_server.py` / `mcp_runner.py` | MCP tool surface (stdio) + the tool loop runner. |
| `action.py` | Action-skill helpers (integration confirmations). |
| `channel_chat.py` | Inbound channel message → DAG turn (`run_kind="chat"`). |
| `agent_config.yaml` | Skill catalogue: prompt, `tools_allowed`, temperature, `max_tokens`. |
| `flags.py` | Runtime kill-switches (`ui.code_ide`, `browser.tools`, `browser.headed`). |
| `studio_*.py` | Studio workspace/review/runs/composer/files (see `studio_workspace.py` etc.). |
| `computer_use/` | Layered desktop agent (see its README). |
| `browser/` | Browser skill, 4-layer cascade (see its README). |
| `prompts/` | Per-skill system prompts + `planner.md` (see its README). |
| `console-frontend/` | React console (see its README). |
| `tests/` | Pytest suite + `TEST.md` checklist (see its README). |
| `apps.py`, `a2ui_*.py`, `app_builder.py` | A2UI app surfaces + builder. |
| `parts_*.py`, `blender_service.py` | 3D parts service/store + Blender bridge. |
| `memory.py`, `vector_index.py` | Memory + FAISS vector index. |
| `scheduler.py`, `sleeps.py`, `notify.py` | Scheduled tasks, self-wake, notifications. |
| `code_index.py`, `code_intel.py`, `terminal.py` | Code workspace indexing/intel/terminal. |
| `schemas.py`, `delta.py`, `persistence.py` | Shared types, deltas, persistence. |

## How it connects
Console → `agent_server.py` → `flow.py` (DAG) → `skills.py` (per node) →
`mcp_runner.py`/`sandbox.py`/`browser`/`computer_use` for tools, and
`gateway.py` → **gateway `:8109`** for every model/credential/document call.

## Run / test
```
cd S9SharedCode/code
uv run agent_server.py                    # :8500
uv run python -m pytest tests/ -q         # full suite ~5–6 min
```
See `TEST.md` for the Studio verification checklist.