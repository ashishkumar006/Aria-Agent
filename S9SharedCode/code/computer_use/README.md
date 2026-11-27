# Computer Use (`computer_use/`)

The layered desktop agent. Drives a real desktop app via **cua-driver** with a
perception-driven cascade, so the agent can open apps, click, type, and read the
screen. **Disabled by default** (`COMPUTER_USE_ENABLED`), **dry-run** by default;
sensitive actions require in-UI approval.

## Modules
| File | Responsibility |
|---|---|
| `engine.py` | `ComputerUseSkill` — the L0→L3 cascade + scan-act-verify loop. The heart. |
| `daemon.py` | Back-compat shim re-exporting `core.daemon` (canonical impl there). |
| `core/` | Canonical daemon (cua-driver lifecycle, JSON-RPC over the named pipe, `capabilities()`). |
| `tool.py` | In-process `computer_action` dispatch (shares the agent's approval store the UI resolves). |
| `shell.py` | Gated-shell L0 helpers (`run_command`/`read_file`/`write_file`/`open_app`). |
| `private_desktop.py` | Isolated desktop surface for recordings/replay. |
| `safety/` | Gates + permissions (see its README). |
| `layers/` | The build-on-top layers A–E (see its README). |
| `prompts/` | Judge (a11y) + vision prompts. |
| `apps/` | App-specific fixtures/targets. |

## Layer cascade (per goal)
- **L0 gated-shell** — fallback when the daemon is unavailable, behind safety gates.
- **L1 extract** — read AX tree / clipboard / file directly (0 LLM).
- **L2a deterministic** — known hotkey sequences (0 LLM).
- **L2b a11y** — `get_window_state` markdown + cheap LLM → `element_index` act.
- **L3 vision** — screenshot + set-of-marks → V9 `/v1/vision` → click `(x,y)`.

The engine runs **scan-act-verify** up to `max_turns` (default 12) with recovery
(rescan / escalate / abort) and vision fallback when AX is insufficient. Judge /
vision default to a fast model (`COMPUTER_USE_JUDGE_MODEL` /
`COMPUTER_USE_VISION_MODEL` / `COMPUTER_USE_REASONING` override). Harness extras:
`open_app`, change-detection settle, no-relaunch on a stale window, action history
to the judge, and a re-ask on a malformed/targetless click.

## Safety
Protected paths (`C:\Windows`, `C:\Program Files`, `~/.ssh`, `~/.aws`) are never
writable. Sensitive commands create a pending approval; the UI panel is gated
behind `?cu=1`.

## Connects
`skills.py` (`if skill.name == "computer"`) builds a `ComputerUseSkill` with
injected V9 judge/vision callables. MCP `computer_action` and the web
`/api/computer/*` routes use the same gates.