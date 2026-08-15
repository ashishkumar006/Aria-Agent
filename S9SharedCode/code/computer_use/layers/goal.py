"""Layer A — goal decomposition (charter §10).

Split a natural-language goal into ordered app-level subgoals. The planner
function is injected (calls the V9 gateway) so this layer stays free of
network code and is unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class Subgoal:
    index: int
    description: str
    app: str | None = None          # target app hint (Calculator, Notepad, ...)
    action_hint: str | None = None  # e.g. "hotkeys", "type_text", "click"
    done: bool = False


def decompose_goal(goal: str, planner_fn: Callable[[str], list[dict]]) -> list[Subgoal]:
    """Split a natural-language goal into ordered subgoals.

    `planner_fn` is injected (calls the V9 gateway with the planner prompt)
    so this layer stays free of network code. Returns Subgoal list.
    """
    raw = planner_fn(goal)
    out: list[Subgoal] = []
    for i, r in enumerate(raw or []):
        out.append(Subgoal(
            index=i,
            description=r.get("description", ""),
            app=r.get("app"),
            action_hint=r.get("action_hint"),
        ))
    return out
