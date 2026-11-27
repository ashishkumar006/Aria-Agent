# Computer-use Layers (`computer_use/layers/`)

The build-on-top layers (charter §6–§10). Each is pure logic with the LLM/network
injected, so it is unit-testable. The engine (`../engine.py`) orchestrates them
in the L0→L3 cascade.

| File | Layer | Responsibility |
|---|---|---|
| `goal.py` | A | Goal decomposition — split a natural-language goal into ordered app-level subgoals (`Subgoal`). Planner injected. |
| `extract.py` | 1 | Read content directly from the AX tree / clipboard / file — **no LLM**. Clipboard trick (Ctrl+A, Ctrl+C) for rendered doc text. |
| `deterministic.py` | 2a | Registry of app-specific hotkey sequences; `try_deterministic(goal, app_hint)` runs 0-LLM. |
| `perception.py` | B | Interpret the AX tree: `filter_ax_markdown` (reduce without hiding anything), summarise long trees, regex-extract rows to skip an LLM call. |
| `sequencing.py` | C | The scan-act-verify loop. Invariant 1: scan before act; Invariant 2: re-scan after every state-changing action. |
| `recovery.py` | D | `RecoveryPolicy` — decide what to do when a turn fails; carries minimal state so it never loops forever; maps the four "traps that look the same" to distinct directives. |
| `vision.py` | E | Vision fallback: escalation trigger + set-of-marks builder (draw numbered boxes, legend → click at `(x,y)`). ~10× L2b cost, so last resort. |

## Notes
- `perception.filter_ax_markdown` is the biggest cost knob (the full tree is sent;
  the filter only summarises, it does not hide elements).
- `sequencing` enforces the two invariants that make the loop safe.
- `recovery` is what stops an infinite rescan loop.