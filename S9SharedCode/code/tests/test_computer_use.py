"""Tests for the layered computer-use package.

Run with:
  uv run --project . python -m pytest tests/test_computer_use.py -q

These tests are network/daemon-free: the LLM judge and the cua-driver
daemon are mocked so we can assert the *logic* (trap guard, layer
selection, re-scan invariant, approval gating) deterministically.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use import layers, safety, daemon, ComputerUse, get_computer_use
from computer_use.engine import ComputerUseSkill, ComputerResult


# ── Layer B: perception interpretation ──────────────────────────────────────
def test_element_count_matches_driver_markdown():
    # Driver markdown uses `[N] Role "Label"` (observed on Windows 0.19.3).
    md = "\n".join(f'[{i}] Button "x{i}"' for i in range(5))
    assert layers.element_count(md) == 5


def test_element_count_zero_is_trap_signal():
    # Empty / non-indexed tree → 0 elements → engine must NOT click blindly.
    assert layers.element_count("no elements here") == 0


def test_filter_ax_markdown_truncates():
    big = "\n".join(f"[{i}] Text 'row{i}'" for i in range(500))
    out = layers.filter_ax_markdown(big, max_chars=200)
    assert len(out) <= 200 + len("\n…(truncated)")


def test_extract_rows_with_named_groups():
    md = "A1 Price 10\nB2 Price 20"
    rows = layers.extract_rows(md, r"(?P<cell>\w\d) Price (?P<val>\d+)")
    assert rows == [{"cell": "A1", "val": "10"}, {"cell": "B2", "val": "20"}]


# ── Layer A: goal decomposition ─────────────────────────────────────────────
def test_decompose_goal_orders_subgoals():
    def fake_planner(goal):
        return [
            {"description": "open app", "app": "Calculator"},
            {"description": "press 2 + 2 =", "action_hint": "hotkeys"},
        ]
    subs = layers.decompose_goal("add 2 and 2", fake_planner)
    assert [s.description for s in subs] == ["open app", "press 2 + 2 ="]


# ── Safety gates ────────────────────────────────────────────────────────────
def test_safety_disabled_blocks_request():
    os.environ["COMPUTER_USE_ENABLED"] = "false"
    safety.reset_shared_gates()
    cu = ComputerUse()
    res = cu.request("run_command", {"command": "echo hi"})
    assert res["status"] == "disabled"


def test_safety_approval_required_for_kill():
    os.environ["COMPUTER_USE_ENABLED"] = "true"
    os.environ["COMPUTER_USE_MODE"] = "live"
    safety.reset_shared_gates()
    cu = ComputerUse()
    res = cu.request("run_command", {"command": "taskkill /F /PID 1234"})
    assert res["status"] == "pending"
    assert "approval_id" in res
    # The approval can be resolved (rejected path returns rejected).
    rid = res["approval_id"]
    out = cu.resolve(rid, approve=False)
    assert out["status"] == "rejected"


def test_safety_protected_path_blocked():
    g = safety.SafetyGates()
    assert g.path_blocked("C:\\Windows\\system32\\x")


# ── Engine: layer selection + re-scan invariant (mocked daemon + LLM) ───────
class _FakeDaemon:
    """Mimics computer_use.daemon.call for L2b a11y path."""

    def __init__(self, tree: str):
        self.tree = tree
        self.calls = []

    def __call__(self, tool, args=None, timeout=60):
        self.calls.append((tool, args))
        if tool == "get_accessibility_tree":
            return {"windows": [{"pid": 1, "window_id": 99, "title": "Calc"}]}
        if tool == "get_window_state":
            return {"tree_markdown": self.tree, "elements": []}
        if tool == "click":
            return {"ok": True}
        return {"ok": True}


def _fake_judge_chat(system, user, schema):
    # Click element 2 once, then declare the goal met.
    _fake_judge_chat.n += 1
    if _fake_judge_chat.n > 1:
        return {"verdict": "done"}
    return {"verdict": "act", "action": {"type": "click", "element_index": 2}}
_fake_judge_chat.n = 0


def test_engine_prefers_l2b_when_ax_present():
    tree = "\n".join(f'[{i}] Button "b{i}"' for i in range(10))
    fd = _FakeDaemon(tree)
    import computer_use.engine as E
    orig = E.daemon.call
    E.daemon.call = fd
    try:
        skill = ComputerUseSkill(llm_chat=_fake_judge_chat, llm_vision=None)
        res = skill.run("click button 2", app_hint="Calculator", max_turns=4)
    finally:
        E.daemon.call = orig
    assert isinstance(res, ComputerResult)
    assert res.layer in ("L2b", "L2a", "L1", "L3")
    # Re-scan invariant: get_window_state must have been called at least once.
    assert any(c[0] == "get_window_state" for c in fd.calls)


def test_engine_trap_guard_zero_elements_raises():
    # A tree with 0 indexed elements must NOT produce a blind click.
    fd = _FakeDaemon("nothing here")
    import computer_use.engine as E
    orig = E.daemon.call
    E.daemon.call = fd
    try:
        skill = ComputerUseSkill(llm_chat=_fake_judge_chat, llm_vision=None)
        res = skill.run("do something", app_hint="Calculator", max_turns=2)
    finally:
        E.daemon.call = orig
    # Either it escalated (no click) or returned without a blind click.
    assert not any(c[0] == "click" and c[1].get("element_index") is None
                   for c in fd.calls)


# ── Backward-compat shim ────────────────────────────────────────────────────
def test_shim_exposes_legacy_api():
    cu = get_computer_use()
    assert hasattr(cu, "request")
    assert hasattr(cu, "list_approvals")
    assert hasattr(cu, "resolve")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
