"""
Comprehensive test suite — computer-use layer.

Covers:
  - Safety gates (enabled, mode, approval, protected paths)
  - Layer 0: Gated shell (read-only)
  - Layer 1: AX tree extract
  - Layer 2a: Deterministic calculator
  - Layer 2b: A11y judge
  - Layer 3: Vision fallback
  - Permissions pre-flight
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


class SafetyGates(unittest.TestCase):
    """computer_use.safety.gates."""

    def test_gates_singleton(self):
        from computer_use.safety import shared_gates, reset_shared_gates
        reset_shared_gates()
        g = shared_gates()
        self.assertIsNotNone(g)

    def test_protected_path_blocked(self):
        from computer_use.engine import ComputerUseSkill
        import os
        os.environ["COMPUTER_USE_ENABLED"] = "true"
        os.environ["COMPUTER_USE_MODE"] = "dry-run"
        from computer_use.safety import reset_shared_gates
        reset_shared_gates()
        skill = ComputerUseSkill(llm_chat=None, llm_vision=None)
        res = skill.shell_write_file("C:/Windows/test.txt", "x")
        self.assertEqual(res.get("status"), "blocked")

    def test_approval_gate(self):
        from computer_use.engine import ComputerUseSkill
        import os
        os.environ["COMPUTER_USE_ENABLED"] = "true"
        os.environ["COMPUTER_USE_MODE"] = "dry-run"
        from computer_use.safety import reset_shared_gates
        reset_shared_gates()
        skill = ComputerUseSkill(llm_chat=None, llm_vision=None)
        res = skill.shell_run_command("rm -rf /", force=False)
        self.assertIn(res.get("status"), ("pending", "dry-run"))


class Layer0Shell(unittest.TestCase):
    """Layer 0: Gated shell fallback."""

    def test_shell_command_planned(self):
        from computer_use.engine import ComputerUseSkill
        import os
        os.environ["COMPUTER_USE_ENABLED"] = "true"
        os.environ["COMPUTER_USE_MODE"] = "dry-run"
        from computer_use.safety import reset_shared_gates
        reset_shared_gates()
        skill = ComputerUseSkill(llm_chat=None, llm_vision=None)
        res = skill.shell_run_command("wmic logicaldisk get size,freespace,caption")
        self.assertIn(res.get("status"), ("done", "dry-run"))


class Layer1Extract(unittest.TestCase):
    """Layer 1: AX tree extract."""

    def test_read_field_value(self):
        from computer_use.layers import read_field_value
        md = '[5] Edit "Search" value="hello world"'
        self.assertEqual(read_field_value(md, "Search"), "hello world")


class Layer2aDeterministic(unittest.TestCase):
    """Layer 2a: Deterministic calculator."""

    def test_calc_plan(self):
        from computer_use.layers import try_deterministic
        plan = try_deterministic("compute 234 * 567", "Calculator")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.app, "calculator")
        self.assertGreater(len(plan.actions), 0)


class Layer2bA11y(unittest.TestCase):
    """Layer 2b: A11y judge."""

    def test_a11y_plan(self):
        from computer_use.layers import try_deterministic
        plan = try_deterministic("write 'hello' in notepad", "Notepad")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.app, "notepad")


class Layer3Vision(unittest.TestCase):
    """Layer 3: Vision fallback."""

    def test_vision_fallback_escalation(self):
        from computer_use.layers import VisionFallback
        fb = VisionFallback()
        self.assertTrue(fb.should_escalate(3, "ax_tree_empty"))


class Permissions(unittest.TestCase):
    """Permissions pre-flight."""

    def test_check_permissions(self):
        from computer_use.safety.permissions import check_permissions
        rep = check_permissions()
        self.assertIsNotNone(rep)


if __name__ == "__main__":
    unittest.main()
