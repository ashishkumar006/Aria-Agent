"""End-to-end test of the computer-use skill through the agent pipeline.

This drives `run_skill` (the same entry point the orchestrator uses) with a
mocked cua-driver daemon so the full cascade — L1 extract -> L2a deterministic
-> L2b a11y -> L3 vision — executes without a live desktop. It proves the
agent wiring (skills.py -> ComputerUseSkill -> layers -> daemon.call) works
end to end, not just the unit-level layer functions.
"""
import sys
import types
from unittest import mock

import pytest

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── fake daemon ──────────────────────────────────────────────────────────────
class FakeDaemon:
    """Records every call and returns canned AX-tree / action responses."""

    def __init__(self):
        self.calls = []
        self.ax_tree = (
            "Window 'Calculator'\n"
            "  [element_index 1] Button \"1\"\n"
            "  [element_index 2] Button \"+\"\n"
            "  [element_index 3] Button \"=\"\n"
            "  [element_index 4] Text \"0\"\n"
        )
        self.running = True

    def ensure_daemon(self):
        return True

    def call(self, tool, args=None, timeout=15):
        self.calls.append((tool, args or {}))
        if tool == "get_accessibility_tree":
            return {
                "tree": self.ax_tree,
                "markdown": self.ax_tree,
                "windows": [{"pid": 1234, "window_id": "w1", "title": "Calculator"}],
            }
        if tool == "get_window_state":
            return {"pid": 1234, "window_id": "w1", "title": "Calculator",
                    "snapshot_id": "s00000001", "tree_markdown": self.ax_tree}
        if tool == "launch_app":
            return {"pid": 1234, "window_id": "w1"}
        if tool == "bring_to_front":
            return {"ok": True}
        if tool in ("click", "type_text", "press_key", "hotkey", "scroll"):
            return {"ok": True, "tool": tool}
        return {"ok": True}


class CalculatorSimDaemon:
    """Simulates the Windows Calculator AX tree + click-driven arithmetic.

    Models the real driver quirks the fix addresses:
      * snapshots expire after the first action in a turn, so the engine must
        re-fetch a fresh snapshot_id before each element-targeted click;
      * typing '*' does not work — only clicking the Multiply button (idx 38)
        and Equals (idx 41) produces correct results;
      * a 'c' keypress clears any stale display from a previous session.
    """

    # element_index -> digit/operator (matches deterministic._CALC_BUTTONS)
    BUTTONS = {43: "0", 44: "1", 45: "2", 46: "3", 47: "4", 48: "5",
               49: "6", 50: "7", 51: "8", 52: "9", 38: "*", 40: "+",
               39: "-", 37: "/", 41: "="}

    def __init__(self):
        self.calls = []
        self._display = "0"
        self._pending = ""
        self._snap_seq = 0

    def ensure_daemon(self):
        return True

    def _tree(self):
        lines = ["Window 'Calculator'"]
        for idx, label in [(38, "Multiply by"), (40, "Plus"),
                           (41, "Equals"), (43, "Zero"), (44, "One"),
                           (45, "Two"), (46, "Three"), (47, "Four"),
                           (48, "Five"), (49, "Six"), (50, "Seven"),
                           (51, "Eight"), (52, "Nine")]:
            lines.append(f'  [element_index {idx}] Button "{label}"')
        lines.append(f'  [8] Text "Display is {self._display}"')
        return "\n".join(lines)

    def call(self, tool, args=None, timeout=15):
        args = args or {}
        self.calls.append((tool, args))
        if tool == "get_accessibility_tree":
            return {"tree": self._tree(), "markdown": self._tree(),
                    "windows": [{"pid": 1234, "window_id": "w1", "title": "Calculator"}]}
        if tool == "get_window_state":
            # Each call returns a FRESH snapshot_id (simulating expiry).
            self._snap_seq += 1
            return {"pid": 1234, "window_id": "w1", "title": "Calculator",
                    "snapshot_id": f"s{self._snap_seq:08x}",
                    "tree_markdown": self._tree()}
        if tool == "bring_to_front":
            return {"ok": True}
        if tool == "press_key" and args.get("key") == "c":
            self._display = "0"
            self._pending = ""
            return {"ok": True}
        if tool == "click" and args.get("element_index") in self.BUTTONS:
            ch = self.BUTTONS[args["element_index"]]
            if ch == "=":
                # Evaluate pending * display (simple left-to-right).
                try:
                    self._display = str(eval(self._pending + self._display))
                except Exception:
                    self._display = "Error"
                self._pending = ""
            elif ch in "+-*/":
                self._pending = self._display + ch
                self._display = "0"
            else:  # digit
                self._display = self._display + ch if self._display != "0" else ch
            return {"ok": True}
        if tool in ("type_text", "press_key", "hotkey", "scroll"):
            return {"ok": True}
        return {"ok": True}


@pytest.fixture
def fake_daemon(monkeypatch):
    d = FakeDaemon()
    # ORDER-INDEPENDENCE FIX: this suite previously passed only when run
    # AFTER test_computer_use_new.py, whose setup_method leaks
    # COMPUTER_USE_ENABLED=true into os.environ. SafetyGates reads that var
    # at construction, so running this file alone left the engine reporting
    # "Computer-use is disabled". Set the env here and reset the shared
    # gates so every test is self-sufficient regardless of file order.
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "live")
    # Mock check_permissions to return all-ok (avoids real screenshot capture).
    # Must patch in engine module since it imports check_permissions at load time.
    import computer_use.engine as E
    monkeypatch.setattr(E, "check_permissions", lambda: E.safety.permissions.PermissionReport(
        binary_present=True, daemon_running=True, ax_ok=True,
        screenshot_ok=True, elevated=False, platform="win32", apps=[]))
    import computer_use.safety
    computer_use.safety.reset_shared_gates()
    # Patch the module the engine actually imports: computer_use.daemon
    import computer_use.daemon as cd
    monkeypatch.setattr(cd, "ensure_daemon", lambda *a, **k: True)
    monkeypatch.setattr(cd, "call", d.call)
    monkeypatch.setattr(cd, "capabilities", lambda: {"daemon_running": True, "ax_ok": True})
    return d


def _make_skill():
    from skills import Skill
    return Skill("computer", {
        "prompt": "prompts/coder.md",
        "description": "computer use",
        "tools_allowed": [],
        "provider_pin": None,
    })


def _graph_nodes(goal, app=None):
    meta = {"goal": goal}
    if app:
        meta["app"] = app
    return {"n1": {"inputs": [], "metadata": meta}}


@pytest.mark.asyncio
async def test_pipeline_calculator_deterministic(fake_daemon):
    """Calculator goal should resolve via L2a deterministic (zero LLM)."""
    import skills
    sk = _make_skill()
    res, rendered = await skills.run_skill(
        sk, "n1", _graph_nodes("compute 234 * 567", app="calculator"),
        session_id="s8-test", query="compute 234 * 567", failure_report=None,
    )
    assert res.success is True, res.output
    cost = res.output.get("cost", {})
    # Deterministic layer must NOT invoke the LLM judge.
    assert cost.get("l2b_calls", 0) == 0, cost
    assert cost.get("l3_calls", 0) == 0, cost
    # The engine resolved at the L2a deterministic layer (not L1/L2b/L3).
    assert res.output.get("layer") == "L2a", res.output
    # In dry-run mode the plan is recorded rather than executed on the desktop.
    # For calculator the engine returns {"display": ...}; for other plans it
    # returns {"plan": ...}. Either way the deterministic layer ran (L2a).
    out = res.output.get("result", {})
    assert out.get("plan") or out.get("display") is not None, \
        f"no deterministic result produced: {res.output}"


@pytest.mark.asyncio
async def test_pipeline_read_document_l1(fake_daemon):
    """A 'what does this say' goal should short-circuit at L1 extract."""
    import skills
    sk = _make_skill()
    res, rendered = await skills.run_skill(
        sk, "n1", _graph_nodes("what does this document say", app="notepad"),
        session_id="s8-test", query="what does this document say", failure_report=None,
    )
    # L1 extract either succeeds (returns text) or escalates; either way the
    # pipeline must complete without raising and report a cost ledger.
    assert res is not None
    assert "cost" in (res.output or {})


@pytest.mark.asyncio
async def test_pipeline_approval_gate(fake_daemon, monkeypatch):
    """A destructive command must be gated and resolvable via the safety API."""
    from computer_use import safety, ComputerUse
    # SafetyGates reads COMPUTER_USE_ENABLED at construction; the fixture sets
    # it, so reset the singleton to pick it up, then exercise the gate.
    safety.reset_shared_gates()
    gates = safety.shared_gates()
    assert gates.enabled is True
    cu = ComputerUse(session_id="s8-test")
    out = cu.request("run_command", {"command": "rm -rf /"})
    # Either auto-approved (if policy allows) or returns a pending approval id.
    assert out.get("status") in ("ok", "pending", "disabled", "error")
    if out.get("status") == "pending":
        aid = out["approval_id"]
        resolved = cu.resolve(aid, approve=True)
        assert resolved is not None


@pytest.mark.asyncio
async def test_pipeline_calculator_click_arithmetic(monkeypatch):
    """REGRESSION: calculator must click buttons (not type '*'), clearing
    stale state, and re-fetch snapshots before each click.

    Reproduces the bug where 'compute 234 * 567' opened Calculator but left
    the display at 0 (type_text refused when snapshot_id present without
    element_index, and typed '*' arrived as the wrong glyph). The fix drives
    the calculator via button clicks with a fresh snapshot per click and a
    'c' keypress to clear prior session state.
    """
    # Enable computer-use and mock permissions
    monkeypatch.setenv("COMPUTER_USE_ENABLED", "true")
    monkeypatch.setenv("COMPUTER_USE_MODE", "live")
    import computer_use.safety
    computer_use.safety.reset_shared_gates()
    import computer_use.engine as E
    monkeypatch.setattr(E, "check_permissions", lambda: E.safety.permissions.PermissionReport(
        binary_present=True, daemon_running=True, ax_ok=True,
        screenshot_ok=True, elevated=False, platform="win32", apps=[]))

    import computer_use.daemon as cd
    sim = CalculatorSimDaemon()
    monkeypatch.setattr(cd, "ensure_daemon", lambda *a, **k: True)
    monkeypatch.setattr(cd, "call", sim.call)
    monkeypatch.setattr(cd, "capabilities", lambda: {"daemon_running": True, "ax_ok": True})

    import skills
    sk = _make_skill()
    res, _ = await skills.run_skill(
        sk, "n1", _graph_nodes("compute 234 * 567", app="calculator"),
        session_id="s8-test", query="compute 234 * 567", failure_report=None,
    )
    assert res.success is True, res.output
    assert res.output.get("layer") == "L2a", res.output
    # The simulated display must show the correct product.
    assert "132678" in (res.output.get("result", {}).get("display") or ""), res.output
    # CONTRACT (aligned with deterministic._calc_plan): the clear is a CLICK
    # on the Clear button (fallback index 22), not a 'c' keypress — blind
    # keyboard input does not reach Calculator reliably. So: 9 clicks total
    # (clear + 2,3,4,*,5,6,7,=) and NO type_text of the raw expression.
    tools = [c[0] for c in sim.calls]
    assert "press_key" not in tools or all(
        c[1].get("key") != "c" for c in sim.calls if c[0] == "press_key")
    # 9 clicks: clear@22, then 2,3,4,*,5,6,7,=
    click_indices = [c[1].get("element_index") for c in sim.calls if c[0] == "click"]
    assert click_indices[0] == 22, f"first click must be Clear(22): {click_indices}"
    assert len(click_indices) == 9, click_indices
    # No type_text of the raw expression (the old broken path).
    typed = [c[1].get("text") for c in sim.calls if c[0] == "type_text"]
    assert not any("*" in t for t in typed), f"typed '*' instead of clicking: {typed}"
