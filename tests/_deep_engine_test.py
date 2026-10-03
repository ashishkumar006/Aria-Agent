"""Deep engine-level test: exercises ComputerUseSkill.run() end-to-end
with a mock daemon to find real bugs in the cascade logic."""
from __future__ import annotations

import json
import sys
import traceback

sys.path.insert(0, ".")
import os

os.environ["COMPUTER_USE_ENABLED"] = "true"
os.environ["COMPUTER_USE_MODE"] = "dry-run"

import computer_use.safety
computer_use.safety.reset_shared_gates()
import computer_use.daemon as daemon_mod
from computer_use.engine import ComputerUseSkill

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail[:120]}" if not ok else ""))


# ── Mock daemon that simulates a real Calculator ─────────────────────────
class MockDaemon:
    """Mock daemon that simulates a real Calculator with persistent state."""

    def __init__(self):
        self.calls = []
        self._display = "0"
        self._pending = ""  # pending expression for eval

    def __call__(self, tool, args=None, timeout=60):
        self.calls.append((tool, args))
        if tool == "get_accessibility_tree":
            return {"windows": [{"pid": 1234, "window_id": 1, "title": "Calculator"}]}
        if tool == "get_window_state":
            # Return the CURRENT display state (persists across calls)
            tree = f"""
[0] Window "Calculator" [pid=1234]
  [1] Text "Display is {self._display}" [id=display]
  [2] Button "Clear" [id=clearButton]
  [3] Button "0" [id=num0Button]
  [4] Button "1" [id=num1Button]
  [5] Button "2" [id=num2Button]
  [6] Button "3" [id=num3Button]
  [7] Button "4" [id=num4Button]
  [8] Button "5" [id=num5Button]
  [9] Button "6" [id=num6Button]
  [10] Button "7" [id=num7Button]
  [11] Button "8" [id=num8Button]
  [12] Button "9" [id=num9Button]
  [13] Button "+" [id=plusButton]
  [14] Button "-" [id=minusButton]
  [15] Button "*" [id=multiplyButton]
  [16] Button "/" [id=divideButton]
  [17] Button "=" [id=equalButton]
""".strip()
            elements = [
                {"element_index": 2, "label": "Clear", "id": "clearButton", "role": "button"},
                {"element_index": 3, "label": "0", "id": "num0Button", "role": "button"},
                {"element_index": 4, "label": "1", "id": "num1Button", "role": "button"},
                {"element_index": 5, "label": "2", "id": "num2Button", "role": "button"},
                {"element_index": 6, "label": "3", "id": "num3Button", "role": "button"},
                {"element_index": 7, "label": "4", "id": "num4Button", "role": "button"},
                {"element_index": 8, "label": "5", "id": "num5Button", "role": "button"},
                {"element_index": 9, "label": "6", "id": "num6Button", "role": "button"},
                {"element_index": 10, "label": "7", "id": "num7Button", "role": "button"},
                {"element_index": 11, "label": "8", "id": "num8Button", "role": "button"},
                {"element_index": 12, "label": "9", "id": "num9Button", "role": "button"},
                {"element_index": 13, "label": "+", "id": "plusButton", "role": "button"},
                {"element_index": 14, "label": "-", "id": "minusButton", "role": "button"},
                {"element_index": 15, "label": "*", "id": "multiplyButton", "role": "button"},
                {"element_index": 16, "label": "/", "id": "divideButton", "role": "button"},
                {"element_index": 17, "label": "=", "id": "equalButton", "role": "button"},
            ]
            return {"tree_markdown": tree, "elements": elements, "snapshot_id": "snap1"}
        if tool == "click":
            # Simulate calculator logic with PERSISTENT state
            idx = args.get("element_index")
            if idx == 2:  # Clear
                self._display = "0"
                self._pending = ""
            elif idx == 17:  # =
                try:
                    self._display = str(eval(self._pending))
                except Exception:
                    self._display = "Error"
                self._pending = ""
            elif idx == 13:  # +
                self._pending += "+"
            elif idx == 14:  # -
                self._pending += "-"
            elif idx == 15:  # *
                self._pending += "*"
            elif idx == 16:  # /
                self._pending += "/"
            elif 3 <= idx <= 12:  # digits 0-9
                self._pending += str(idx - 3)
            return {"ok": True}
        if tool == "type_text":
            return {"ok": True}
        if tool == "press_key":
            return {"ok": True}
        return {"ok": True}


def run_test(name, goal, app_hint, expected_display=None, expected_layer=None, mode="dry-run"):
    """Run a single test case."""
    mock = MockDaemon()
    orig_call = daemon_mod.call
    orig_ensure = daemon_mod.ensure_daemon
    daemon_mod.call = mock
    daemon_mod.ensure_daemon = lambda *a, **k: True
    old_mode = os.environ.get("COMPUTER_USE_MODE")
    os.environ["COMPUTER_USE_MODE"] = mode
    computer_use.safety.reset_shared_gates()
    try:
        from computer_use.engine import ComputerUseSkill
        skill = ComputerUseSkill(llm_chat=lambda s, u, sc: {"verdict": "done"},
                                 llm_vision=None)
        res = skill.run(goal, app_hint=app_hint, max_turns=12)
        checks = []
        if expected_layer:
            checks.append(("layer", res.layer, expected_layer))
        if expected_display is not None:
            display = res.output.get("display", "")
            checks.append(("display", display, expected_display))
        ok = res.success
        for label, got, exp in checks:
            if got != exp:
                ok = False
                record(name, False, f"{label}: got {got!r}, expected {exp!r}")
                return
        if ok:
            record(name, True, f"layer={res.layer}, display={res.output.get('display', '')!r}")
        else:
            record(name, False, f"success=False, layer={res.layer}, error={res.error!r}")
    except Exception as e:
        record(name, False, f"EXCEPTION: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        daemon_mod.call = orig_call
        daemon_mod.ensure_daemon = orig_ensure
        if old_mode is not None:
            os.environ["COMPUTER_USE_MODE"] = old_mode
        computer_use.safety.reset_shared_gates()


def main():
    print("=" * 70)
    print("COMPUTER-USE ENGINE — DEEP BUG-FINDING TEST")
    print("=" * 70)

    # ── L2a: Calculator via live AX tree ─────────────────────────────────
    print("\n-- L2a: Calculator (live AX tree) --")
    run_test("L2a.calc.2+2", "compute 2 + 2", "Calculator",
             expected_display="4", expected_layer="L2a")
    run_test("L2a.calc.234x567", "compute 234 * 567", "Calculator",
             expected_display="132678", expected_layer="L2a")

    # ── L1: Extract (read-only) ──────────────────────────────────────────
    print("\n-- L1: Extract (read-only) --")
    mock = MockDaemon()
    orig_call = daemon_mod.call
    orig_ensure = daemon_mod.ensure_daemon
    daemon_mod.call = mock
    daemon_mod.ensure_daemon = lambda *a, **k: True
    try:
        skill = ComputerUseSkill(llm_chat=lambda s, u, sc: {"verdict": "done"},
                                 llm_vision=None)
        res = skill.run("what is on the calculator display", app_hint="Calculator", max_turns=4)
        record("L1.extract.read_display", res.success and res.layer == "L1",
               f"layer={res.layer}, output={json.dumps(res.output, default=str)[:150]}")
    except Exception as e:
        record("L1.extract.read_display", False, f"EXCEPTION: {e}")
    finally:
        daemon_mod.call = orig_call
        daemon_mod.ensure_daemon = orig_ensure

    # ── L2b: A11y judge (typing in Notepad) ──────────────────────────────
    print("\n-- L2b: A11y judge (Notepad) --")
    mock = MockDaemon()
    orig_call = daemon_mod.call
    orig_ensure = daemon_mod.ensure_daemon
    daemon_mod.call = mock
    daemon_mod.ensure_daemon = lambda *a, **k: True
    try:
        def judge_type(system, user, schema):
            judge_type.n += 1
            if judge_type.n > 2:
                return {"verdict": "done"}
            return {"verdict": "act", "action": {"type": "type", "value": "hello"}}
        judge_type.n = 0
        skill = ComputerUseSkill(llm_chat=judge_type, llm_vision=None)
        res = skill.run("type hello in notepad", app_hint="Notepad", max_turns=6)
        record("L2b.a11y.type_hello", res.success and res.layer in ("L2b", "L2b-dry-run"),
               f"layer={res.layer}, output={json.dumps(res.output, default=str)[:150]}")
    except Exception as e:
        record("L2b.a11y.type_hello", False, f"EXCEPTION: {e}")
    finally:
        daemon_mod.call = orig_call
        daemon_mod.ensure_daemon = orig_ensure

    # ── Safety: Protected path ────────────────────────────────────────────
    print("\n-- Safety: Gates --")
    from computer_use.engine import ComputerUseSkill
    skill = ComputerUseSkill(llm_chat=None, llm_vision=None)
    res = skill.shell_write_file("C:/Windows/test.txt", "malicious")
    record("Safety.protected_path", res.get("status") == "blocked",
           f"status={res.get('status')}")
    res = skill.shell_run_command("rm -rf /", force=False)
    record("Safety.approval_gate", res.get("status") in ("pending", "dry-run"),
           f"status={res.get('status')}")

    # ── Summary ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print("\n" + "=" * 70)
    print(f"DEEP TEST: {passed}/{len(RESULTS)} passed")
    if passed < len(RESULTS):
        print("FAILED:")
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  - {name}: {detail[:120]}")
    print("=" * 70)
    return 1 if passed < len(RESULTS) else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
