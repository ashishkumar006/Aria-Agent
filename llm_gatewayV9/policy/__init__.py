"""Policy engine: declarative guardrails OUTSIDE the LLM context.

Rules live in policy.yaml (hot-reloadable). Every tool call and every
adaptor send is evaluated BEFORE dispatch: first matching rule wins, ties
resolve to deny, default for `untrusted` is deny-all. Starts in dry-run
(report-only) mode; enforcement flips per rule via `enforce: true`.
"""
from __future__ import annotations

from .engine import PolicyEngine, PolicyVerdict, get_engine, reload_engine, spend_over_cap

__all__ = ["PolicyEngine", "PolicyVerdict", "get_engine", "reload_engine", "spend_over_cap"]
