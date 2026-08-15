"""Layer C — action sequencing (charter §7, §10).

The scan-act-verify loop. Honors both invariants:
  Invariant 1: scan BEFORE act (builds element_index cache).
  Invariant 2: re-scan after every state-changing action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class TurnResult:
    action_taken: dict | None
    post_state: dict | None
    verified: bool = False
    note: str = ""


def scan_act_verify(
    scan: Callable[[], dict],
    act: Callable[[dict], dict],
    verify: Callable[[dict, dict], tuple[bool, str]],
    action: dict,
) -> TurnResult:
    """One scan-act-verify turn.

    `scan`   → returns window state (with element_index cache)
    `act`    → performs the action addressed by element_index
    `verify` → (pre_state, post_state) → (ok, note)
    """
    pre = scan()            # Invariant 1
    res = act(action)
    post = scan()           # Invariant 2: re-scan after state change
    ok, note = verify(pre, post)
    return TurnResult(action_taken=res, post_state=post, verified=ok, note=note)
