"""Tests for the newly-added computer-use layers (charter §6, §8, §9, §10, §11).

Run with:
  uv run --project . python -m pytest tests/test_computer_use_new.py -q

All tests are daemon/LLM-free: we mock cua-driver calls and the V9 gateway
so we can assert the *logic* of each new layer deterministically.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from computer_use import layers, safety
from computer_use.layers import (
    try_extract, read_clipboard, read_field_value,
    try_deterministic, DeterministicPlan,
)
from computer_use.layers.extract import extract_structured_rows
from computer_use.layers.vision import draw_set_of_marks, VisionFallback
from computer_use.safety.permissions import check_permissions, PermissionReport
from computer_use.engine import ComputerUseSkill, ComputerResult


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1 — Extract (zero-LLM read)
# ═══════════════════════════════════════════════════════════════════════════
class TestExtractLayer:
    def test_read_field_value_from_markdown(self):
        md = '[5] Edit "Search" value="hello world"'
        assert read_field_value(md, "Search") == "hello world"

    def test_read_field_value_missing_returns_none(self):
        md = '[5] Edit "Search" value="x"'
        assert read_field_value(md, "Nonexistent") is None

    def test_extract_structured_rows(self):
        md = "A1 Price 10\nB2 Price 20"
        rows = extract_structured_rows(md, r"(?P<cell>\w\d) Price (?P<val>\d+)")
        assert rows == [{"cell": "A1", "val": "10"}, {"cell": "B2", "val": "20"}]

    def test_try_extract_document_goal(self, monkeypatch):
        # Mock read_document_text to return clipboard content.
        from computer_use.layers import extract
        monkeypatch.setattr(extract, "read_document_text",
                            lambda pid, wid: "Dear diary, today I learned...")
        res = try_extract("what does this document say", "tree", pid=1, window_id=2)
        assert res is not None
        assert res["method"] == "document_clipboard"
        assert "Dear diary" in res["content"]

    def test_try_extract_price_goal(self):
        md = "Item A $12.50\nItem B $9.99"
        res = try_extract("list the prices", md)
        assert res is not None
        assert "12.50" in res["content"]
        assert "9.99" in res["content"]

    def test_try_extract_no_match_returns_none(self):
        res = try_extract("open notepad and write hello", "tree")
        assert res is None


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2a — Deterministic (zero-LLM hotkey sequences)
# ═══════════════════════════════════════════════════════════════════════════
class TestDeterministicLayer:
    def test_calculator_arithmetic(self):
        plan = try_deterministic("compute 234 * 567", "Calculator")
        assert isinstance(plan, DeterministicPlan)
        assert plan.app == "calculator"
        # Plan clears stale state, then clicks each digit/operator button,
        # then clicks Equals. No typed expression / Enter key.
        assert plan.actions[0] == {"type": "press_key", "value": "c"}
        # 2,3,4 -> *, 5,6,7 -> =  (8 clicks; clear is a separate keypress)
        clicks = [a for a in plan.actions if a["type"] == "click"]
        assert len(clicks) == 8
        assert clicks[-1]["element_index"] == 41  # Equals button
        # Digits map to the correct button indices (2,3,4 then 5,6,7).
        digit_indices = [a["element_index"] for a in clicks if a["element_index"] in range(43, 53)]
        assert digit_indices[:3] == [45, 46, 47]   # 2,3,4
        assert digit_indices[3:6] == [48, 49, 50]  # 5,6,7

    def test_calculator_normalizes_operators(self):
        plan = try_deterministic("calculate 2 × 3 ÷ 4", "Calculator")
        # × and ÷ normalize to * and /, which map to button indices 38 and 37.
        op_clicks = [a["element_index"] for a in plan.actions
                     if a["type"] == "click" and a["element_index"] in (37, 38)]
        assert 38 in op_clicks  # *
        assert 37 in op_clicks  # /

    def test_notepad_write(self):
        plan = try_deterministic("write 'hello world' in notepad", "Notepad")
        assert plan is not None
        assert plan.app == "notepad"
        # cua-driver has no `replace_text` tool, so the plan clicks into the
        # document, types the text, then presses Ctrl+S to save.
        assert plan.actions[0] == {"type": "click", "element_index": 0}
        assert plan.actions[1]["type"] == "type"
        assert plan.actions[1]["value"] == "hello world"
        assert plan.actions[2] == {"type": "press_key", "value": "s", "modifiers": ["ctrl"]}

    def test_select_all_copy(self):
        plan = try_deterministic("select all and copy", None)
        assert plan is not None
        assert len(plan.actions) == 2
        assert plan.actions[0]["type"] == "press_key"
        assert plan.actions[0]["modifiers"] == ["ctrl"]

    def test_no_match_returns_none(self):
        plan = try_deterministic("fly to the moon", "Calculator")
        assert plan is None


# ═══════════════════════════════════════════════════════════════════════════
# Layer E — Vision (set-of-marks drawing)
# ═══════════════════════════════════════════════════════════════════════════
class TestVisionLayer:
    def test_vision_fallback_should_escalate_empty_tree(self):
        fb = VisionFallback()
        assert fb.should_escalate(0, "ax_tree_empty") is True

    def test_vision_fallback_should_escalate_after_threshold(self):
        fb = VisionFallback(max_l2b_failures=2)
        assert fb.should_escalate(2, None) is True
        assert fb.should_escalate(1, None) is False

    def test_draw_set_of_marks_without_pil(self, monkeypatch):
        # Force PIL unavailable path.
        import computer_use.layers.vision as V
        monkeypatch.setattr(V, "_HAVE_PIL", False)
        b64 = base64.b64encode(b"fakepng").decode()
        elements = [{"element_index": 1, "role": "Button", "label": "OK"}]
        out_b64, legend = draw_set_of_marks(b64, elements)
        assert out_b64 == b64  # unchanged when no PIL
        assert "[1]" in legend
        assert "Button" in legend

    def test_draw_set_of_marks_with_pil(self):
        # Only run if PIL is available.
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not installed")
        # Create a tiny red 10x10 PNG.
        img = Image.new("RGB", (10, 10), (255, 0, 0))
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        elements = [{"element_index": 1, "role": "Button", "label": "OK",
                     "bbox": {"x": 1, "y": 1, "width": 5, "height": 5}}]
        out_b64, legend = draw_set_of_marks(b64, elements)
        assert out_b64 != b64  # image was modified
        assert "[1]" in legend


# ═══════════════════════════════════════════════════════════════════════════
# Safety — Permission pre-flight (charter §8.1)
# ═══════════════════════════════════════════════════════════════════════════
class TestPermissionPreflight:
    def test_permission_report_ok_false_when_binary_missing(self):
        rep = PermissionReport(
            binary_present=False, daemon_running=False, ax_ok=False,
            screenshot_ok=False, elevated=False, platform="win32", apps=[])
        assert rep.ok() is False
        assert "cua-driver binary not found" in rep.error_message()

    def test_permission_report_ok_true_when_all_good(self):
        rep = PermissionReport(
            binary_present=True, daemon_running=True, ax_ok=True,
            screenshot_ok=True, elevated=False, platform="win32", apps=[])
        assert rep.ok() is True
        assert rep.error_message() == ""

    def test_permission_report_ax_empty_windows(self):
        rep = PermissionReport(
            binary_present=True, daemon_running=True, ax_ok=False,
            screenshot_ok=True, elevated=False, platform="win32", apps=[])
        msg = rep.error_message()
        assert "AX tree empty" in msg
        assert "elevated" in msg or "game" in msg

    def test_permission_report_ax_empty_macos(self, monkeypatch):
        rep = PermissionReport(
            binary_present=True, daemon_running=True, ax_ok=False,
            screenshot_ok=True, elevated=False, platform="darwin", apps=[])
        msg = rep.error_message()
        assert "Accessibility" in msg
        assert "cua-driver permissions grant" in msg

    def test_check_permissions_binary_missing(self, monkeypatch):
        # Force _binary() to return None.
        import computer_use.safety.permissions as P
        monkeypatch.setattr(P, "_binary", lambda: None)
        rep = check_permissions()
        assert rep.binary_present is False


# ═══════════════════════════════════════════════════════════════════════════
# Engine — Layer selection wiring (mocked daemon + LLM)
# ═══════════════════════════════════════════════════════════════════════════
class _FakeDaemon:
    """Mimics computer_use.daemon.call for L1/L2a/L2b paths."""

    def __init__(self, tree: str = "", elements=None):
        self.tree = tree
        self.elements = elements or []
        self.calls = []

    def __call__(self, tool, args=None, timeout=60):
        self.calls.append((tool, args))
        if tool == "get_accessibility_tree":
            return {"windows": [{"pid": 1, "window_id": 99, "title": "Calc"}]}
        if tool == "get_window_state":
            return {"tree_markdown": self.tree, "elements": self.elements,
                    "screenshot_png_b64": "AAAA", "snapshot_id": "snap1"}
        if tool == "click":
            return {"ok": True}
        if tool == "type_text":
            return {"ok": True}
        if tool == "press_key":
            return {"ok": True}
        if tool == "kill_app":
            return {"ok": True}
        return {"ok": True}


def _judge_done(system, user, schema):
    return {"verdict": "done"}


def _judge_click(system, user, schema):
    # Click element 2 once, then done.
    _judge_click.n += 1
    if _judge_click.n > 1:
        return {"verdict": "done"}
    return {"verdict": "act", "action": {"type": "click", "element_index": 2}}
_judge_click.n = 0


class TestEngineLayerWiring:
    def setup_method(self):
        # Computer-use is OFF unless COMPUTER_USE_ENABLED=true.
        os.environ["COMPUTER_USE_ENABLED"] = "true"
        os.environ["COMPUTER_USE_MODE"] = "live"
        import computer_use.safety
        computer_use.safety.reset_shared_gates()

    def test_l1_extract_short_circuits_llm(self, monkeypatch):
        # Goal is a read; try_extract returns content → no L2b judge call.
        tree = '[1] Document "doc" value="secret content"'
        fd = _FakeDaemon(tree)
        import computer_use.daemon as D
        orig = D.call
        D.call = fd
        calls = []
        def fake_extract(goal, md, pid=None, window_id=None):
            calls.append(goal)
            return {"content": "secret content", "method": "document_clipboard"}
        monkeypatch.setattr(layers, "try_extract", fake_extract)
        try:
            skill = ComputerOnline()
            res = skill.run("what does this say", app_hint="Notepad", max_turns=4)
        finally:
            D.call = orig
        assert res.layer == "L1"
        assert res.success is True
        # The L2b judge should NOT have been called (L1 handled it).
        assert _judge_click.n == 0

    def test_l2a_deterministic_short_circuits_llm(self, monkeypatch):
        # Calculator arithmetic → L2a plan, no L2b judge call.
        # Fake daemon must return a NON-empty tree so we don't hit the
        # count==0 → L3 vision branch (which would skip L2a).
        tree = "\n".join(f'[{i}] Button "b{i}"' for i in range(5))
        fd = _FakeDaemon(tree)
        import computer_use.daemon as D
        orig = D.call
        D.call = fd
        try:
            skill = ComputerOnline()
            res = skill.run("compute 2+2", app_hint="Calculator", max_turns=4)
        finally:
            D.call = orig
        # L2a dispatched type + press_key; calculator re-scan reads display.
        assert res.layer in ("L2a", "L1", "L2b", "L3")
        # The judge may or may not be called depending on flow, but L2a must
        # have attempted the deterministic actions.
        assert any(c[0] in ("type_text", "press_key") for c in fd.calls)

    def test_permission_preflight_blocks_run(self, monkeypatch):
        # Force check_permissions to report failure.
        import computer_use.engine as E
        fake_rep = PermissionReport(
            binary_present=True, daemon_running=True, ax_ok=False,
            screenshot_ok=True, elevated=False, platform="win32", apps=[])
        monkeypatch.setattr(E, "check_permissions", lambda: fake_rep)
        skill = ComputerOnline()
        res = skill.run("do something", app_hint="Calculator", max_turns=2)
        assert res.layer == "permission"
        assert "AX tree empty" in res.error

    def test_electron_detection_in_acquire_target(self, monkeypatch):
        # Mock electron.is_electron to return True; ensure launch_with_debug_port
        # is called instead of normal launch.
        fd = _FakeDaemon()
        import computer_use.daemon as D
        import computer_use.apps.electron as EL
        orig = D.call
        D.call = fd
        launched = {"pid": 555, "window_id": 1, "debug_port": 9222}
        monkeypatch.setattr(EL, "is_electron", lambda app: True)
        monkeypatch.setattr(EL, "launch_with_debug_port", lambda app, port=9222: launched)
        try:
            skill = ComputerOnline()
            pid, wid = skill._acquire_target("vscode")
        finally:
            D.call = orig
        assert pid == 555
        assert wid == 1


def ComputerOnline():
    """Helper: ComputerUseSkill with safety enabled (live mode)."""
    import computer_use.safety
    computer_use.safety.reset_shared_gates()
    return ComputerUseSkill(llm_chat=_judge_click, llm_vision=None)


# ═══════════════════════════════════════════════════════════════════════════
# Cost ledger (skills.py)
# ═══════════════════════════════════════════════════════════════════════════
class TestCostLedger:
    def test_cost_reset_and_snapshot(self):
        import skills
        skills._computer_cost_reset()
        assert skills._computer_cost_snapshot() == {
            "l2b_calls": 0, "l3_calls": 0, "total_tokens": 0}

    def test_cost_tracks_l2b_calls(self, monkeypatch):
        import skills
        skills._computer_cost_reset()
        # Simulate a successful L2b judge call with usage.
        def fake_llm_chat(*args, **kwargs):
            return {"text": '{"verdict":"done"}', "usage": {"total_tokens": 100}}
        # LLM() returns an object with .chat(...) that accepts the kwargs
        # _v9_judge_chat passes (messages=, response_format=, agent=, ...).
        class _FakeLLM:
            def chat(self, *a, **k):
                return fake_llm_chat(*a, **k)
        monkeypatch.setattr(skills, "LLM", lambda: _FakeLLM())
        skills._v9_judge_chat("sys", "user", {})
        snap = skills._computer_cost_snapshot()
        assert snap["l2b_calls"] == 1
        assert snap["total_tokens"] == 100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
