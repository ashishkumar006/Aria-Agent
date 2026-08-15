# Plan: Computer-Use v2 — Layered `cua-driver` Desktop Agent

## TL;DR
Replace the shallow gated-shell `computer_use.py` with a **perception-driven
desktop agent** built on `cua-driver` (JSON daemon over a Unix socket, 34
tools, cross-platform AX tree + vision). Mirror the existing Browser
cascade (L1 extract → L2a deterministic → L2b a11y → L3 vision) onto the
desktop, add the scan-act-verify loop with its two invariants, the trap
table guard, and the five build-on-top layers (goal decomposition,
perception interpretation, action sequencing, error recovery, vision
fallback). Wire into S9 with one dispatch branch. Keep the gated approval +
dry-run safety net. Ship an ARCHITECTURE doc.

## Feasibility (verified)
- `cua-driver==0.19.3` resolves via `uv pip install --dry-run` (installable).
- Binary NOT yet on PATH; needs `uv add cua-driver` + `ensure_daemon()`.
- Spec is macOS-heavy (AppleScript/TCC); this host is **Windows** → branch
  OS-specific activation on `os.name` (Windows uses `bring_to_front`).
- V9 gateway already has `/v1/vision` (set-of-marks) + `/v1/chat` (a11y
  text) via `browser/client.py::V9Client` — reuse directly, no new gateway.
- Browser cascade in `browser/skill.py` + `browser/driver.py` is the
  structural template to mirror.

## Steps
1. **Add dep + daemon manager** — `uv add cua-driver`; create
   `computer_use/daemon.py` with `ensure_daemon()` (status check → spawn
   `cua-driver serve` → sleep), `call(tool, args)` JSON-socket wrapper,
   `PreconditionError`/`PermissionsError`, and a `capabilities()` probe
   (AX ok? screenshot ok? electron port? elevated?).
   *parallel with step 2*
2. **Rewrite `computer_use.py`** as the layered engine:
   - `ComputerUseSkill` class with `run(NodeSpec)` (mirrors `BrowserSkill`).
   - Layer dispatch: L1 extract (AX text/clipboard/file) → L2a hotkeys →
     L2b a11y (`get_window_state` markdown + cheap LLM action JSON by
     `element_index`) → L3 vision (screenshot + set-of-marks → V9
     `/v1/vision` → click (x,y)).
   - scan-act-verify loop honoring Invariant 1 (scan before act) & Invariant
     2 (re-scan after every state change).
   - Trap guard: `if state["element_count"]==0: raise PreconditionError(...)`.
   - Keep existing gates: ENABLED (off), MODE (dry-run), ALLOW/DENY,
     APPROVAL (pending → `/api/computer/resolve`), AUDIT log.
   - OS branch: macOS `osascript activate`; Windows `bring_to_front`.
   - Degrade to gated-shell mode if daemon unavailable.
   *parallel with step 3*
3. **Five build-on-top layers** (in `computer_use/layers.py`):
   - A goal decomposition (planner prompt → subgoals)
   - B perception interpretation (AX markdown → filtered/summarised)
   - C action sequencing (scan-act-verify orchestration)
   - D error recovery (reflow/modal/crash state carry)
   - E vision fallback (escalation trigger + set-of-marks call)
   *depends on 2*
4. **Wire into S9 runtime** (one line each):
   - `skills.py`: add `if skill.name == "computer":` branch →
     `ComputerUseSkill(...).run(node_spec)` (mirror browser branch ~L387).
   - `agent_config.yaml`: `computer` entry already exists — update
     `prompt: prompts/computer.md` + description to reflect layered driver.
   - `prompts/computer.md`: rewrite to the L1–L3 + scan-act-verify contract.
   *depends on 2,3*
5. **MCP tool surface** — extend `computer_action` in `mcp_server.py` to
   expose `launch_app`, `get_window_state`, `click`, `type_text`,
   `press_key`, `screenshot`, `start_recording`, `replay_trajectory`
   (thin wrappers over `daemon.call`). Keep `run_command/read_file/
   write_file/open_app` for the gated-shell fallback.
   *depends on 1*
6. **Web UI approval + status** — extend `agent_server.py` `/api/computer/*`
   to also report `capabilities()` and pending recordings; add a small
   approval panel to `web/` so destructive desktop actions need a click.
   *depends on 2,4*
7. **ARCHITECTURE doc** — write `COMPUTER_USE_ARCHITECTURE.md` (sections 1–12
   from the spec + novel ideas: cross-app DAG, semantic element cache,
   cost-budget auto-escalation, replay-as-test, permission pre-flight,
   voice-driven control).
   *parallel with 1–6*
8. **Tests + verification** — `tests/test_computer_use.py`: unit the trap
   guard + layer selection with a mocked daemon; integration smoke only if
   daemon starts on this host. Verify end-to-end via `/api/chat` with a
   weather/currency query (already proven) + a computer goal when enabled.

## Relevant files
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\computer_use.py` — rewrite as layered engine
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\computer_use\daemon.py` — NEW: daemon + socket wrapper
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\computer_use\layers.py` — NEW: five layers
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\skills.py` — add `computer` dispatch branch (~L387)
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\agent_config.yaml` — update `computer` entry
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\prompts\computer.md` — rewrite contract
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\mcp_server.py` — extend `computer_action`
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\agent_server.py` — extend `/api/computer/*`
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\web/*` — approval panel
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\browser\skill.py`, `browser\driver.py`, `browser\client.py` — REFERENCE (mirror cascade)
- `c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code\COMPUTER_USE_ARCHITECTURE.md` — NEW doc

## Verification
1. `uv run python -c "import computer_use, computer_use.daemon, computer_use.layers"` — clean import.
2. Unit: trap guard raises on `element_count:0`; L2b chosen over L3 when AX present; re-scan invariant enforced (mock daemon).
3. `uv run python -m pytest tests/test_computer_use.py -q`.
4. Daemon: `cua-driver status` → start if needed; `capabilities()` returns structured report.
5. End-to-end via `/api/chat`: weather + currency queries still pass (regression); computer goal passes when `COMPUTER_USE_ENABLED=true` and daemon up.
6. Approval flow: destructive action → `pending` → `/api/computer/resolve` approve/reject.

## Decisions / scope
- Keep gated-shell fallback (run_command/read_file/write_file/open_app) as L0 for when daemon is absent — preserves the earlier safety work.
- Reuse V9 `/v1/vision` + `/v1/chat` (no new gateway).
- OS-branch activation; Windows is the primary test host.
- Computer-use stays OFF + dry-run by default; sensitive actions need UI approval.
- Novel ideas captured in the ARCHITECTURE doc; implement the first 3 (semantic cache, cost-budget escalation, permission pre-flight) as stretch within layers.py if time permits.

## Further considerations
1. Should `cua-driver` run elevated on this Windows host to exercise the
   "elevated app" path, or stay user-level? (Recommend: stay user-level;
   document elevated path only.)
2. Which demo target app to use for the portfolio video — Calculator
   (L2a hotkeys) or Notepad (L1/L2b)? (Recommend: Calculator for a clean
   deterministic L2a demo + Notepad for L2b.)
3. Voice-driven desktop control (novel idea 6) — build now or defer to a
   later session? (Recommend: defer; core layered agent first.)
