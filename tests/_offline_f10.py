"""Offline test suite for the F10 fix (cross-session memory contamination).

Run:  .venv/Scripts/python.exe _offline_f10.py

Covers:
  1. turnlog append/recent/clear round-trip
  2. per-session isolation (session A's turns never leak into session B)
  3. trimming at _MAX_TURNS_PER_SESSION
  4. format_for_prompt rendering + empty handling
  5. render_prompt injects CONVERSATION HISTORY only when turns exist
  6. render_prompt hardened MEMORY HITS wording (never-presented-as-history)
  7. memory-excluded skills see neither block
  8. run_skill signature accepts prior_turns (wiring intact)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import turnlog
from schemas import AgentResult


class TurnLogTests(unittest.TestCase):
    def setUp(self):
        # Redirect the store to a temp file per test.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = turnlog.STATE_PATH
        turnlog.STATE_PATH = Path(self._tmp.name) / "turn_logs.json"

    def tearDown(self):
        turnlog.STATE_PATH = self._orig_path
        self._tmp.cleanup()

    def test_01_append_and_recent_roundtrip(self):
        turnlog.append("s8-test1", "What is 2+2?", "4")
        turnlog.append("s8-test1", "Capital of France?", "Paris.")
        turns = turnlog.recent("s8-test1")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["q"], "What is 2+2?")
        self.assertEqual(turns[0]["a"], "4")
        self.assertEqual(turns[1]["q"], "Capital of France?")
        self.assertIn("ts", turns[0])

    def test_02_session_isolation(self):
        """THE F10 BUG: session A's questions must never appear in B's log."""
        turnlog.append("s8-alice", "day of week of January 1, 2000?", "Saturday")
        turnlog.append("s8-bob", "hi", "Hello!")
        alice = turnlog.recent("s8-alice")
        bob = turnlog.recent("s8-bob")
        self.assertEqual(len(alice), 1)
        self.assertEqual(len(bob), 1)
        self.assertNotIn("2000", bob[0]["q"])
        # Unknown session gets nothing.
        self.assertEqual(turnlog.recent("s8-nobody"), [])

    def test_03_trimming(self):
        for i in range(60):
            turnlog.append("s8-trim", f"q{i}", f"a{i}")
        turns = turnlog.recent("s8-trim", n=100)
        self.assertLessEqual(len(turns), turnlog._MAX_TURNS_PER_SESSION)
        # Oldest trimmed, newest kept.
        self.assertEqual(turns[-1]["q"], "q59")

    def test_04_recent_n_cap(self):
        for i in range(10):
            turnlog.append("s8-cap", f"q{i}", f"a{i}")
        self.assertEqual(len(turnlog.recent("s8-cap", n=3)), 3)
        self.assertEqual(turnlog.recent("s8-cap", n=3)[0]["q"], "q7")

    def test_05_empty_answer_ok(self):
        turnlog.append("s8-empty", "broken query", "")
        turns = turnlog.recent("s8-empty")
        self.assertEqual(turns[0]["a"], "")

    def test_06_clear_single_session(self):
        turnlog.append("s8-c1", "q", "a")
        turnlog.append("s8-c2", "q", "a")
        removed = turnlog.clear("s8-c1")
        self.assertEqual(removed, 1)
        self.assertEqual(turnlog.recent("s8-c1"), [])
        self.assertEqual(len(turnlog.recent("s8-c2")), 1)

    def test_07_format_for_prompt(self):
        block = turnlog.format_for_prompt([
            {"q": "What is 2+2?", "a": "4"},
            {"q": "multi\nline", "a": "ans\nwer"},
        ])
        self.assertIn("USER: What is 2+2?", block)
        self.assertIn("AGENT: 4", block)
        self.assertNotIn("\n", "multi line")  # newlines flattened in q/a
        self.assertIn("multi line", block)
        self.assertEqual(turnlog.format_for_prompt([]), "")


class RenderPromptTests(unittest.TestCase):
    """render_prompt integration — needs the real skill registry."""

    @classmethod
    def setUpClass(cls):
        from skills import SkillRegistry
        cls.registry = SkillRegistry()
        cls.planner = cls.registry.get("planner")

    def _render(self, **kw):
        from skills import render_prompt
        return render_prompt(
            self.planner, "test query",
            [{"id": "USER_QUERY", "kind": "query", "value": "test query"}],
            **kw,
        )

    def test_08_history_block_injected_when_turns_exist(self):
        prompt = self._render(prior_turns=[{"q": "first question", "a": "first answer"}])
        self.assertIn("CONVERSATION HISTORY", prompt)
        self.assertIn("USER: first question", prompt)
        self.assertIn("AGENT: first answer", prompt)

    def test_09_no_history_block_when_no_turns(self):
        prompt = self._render(prior_turns=None)
        self.assertNotIn("CONVERSATION HISTORY", prompt)

    def test_10_memory_hits_wording_hardened(self):
        hits = [type("H", (), {
            "kind": "fact", "descriptor": "Question asking for the day of "
            "the week of January 1, 2000", "source": "user_query",
            "value": {"raw": "What is the day of the week of January 1, 2000?"},
        })()]
        prompt = self._render(memory_hits=hits)
        self.assertIn("MEMORY HITS", prompt)
        # Hardened wording must explicitly forbid presenting as history.
        self.assertIn("NOT part of this conversation", prompt)
        self.assertIn("NEVER be presented", prompt)

    def test_11_formatter_sees_neither_block(self):
        formatter = self.registry.get("formatter")
        from skills import render_prompt
        prompt = render_prompt(
            formatter, "q",
            [{"id": "n:1", "kind": "upstream", "skill": "planner", "output": {}}],
            memory_hits=[type("H", (), {
                "kind": "fact", "descriptor": "d", "source": "s", "value": {},
            })()],
            prior_turns=[{"q": "x", "a": "y"}],
        )
        self.assertNotIn("MEMORY HITS", prompt)
        self.assertNotIn("CONVERSATION HISTORY", prompt)


class WiringTests(unittest.TestCase):
    def test_12_run_skill_accepts_prior_turns(self):
        import inspect
        from skills import run_skill
        sig = inspect.signature(run_skill)
        self.assertIn("prior_turns", sig.parameters)

    def test_13_executor_passes_prior_turns(self):
        """flow.Executor.run must load the turn log and thread it through."""
        src = Path(__file__).with_name("flow.py").read_text(encoding="utf-8")
        self.assertIn("turnlog.recent(sid)", src)
        self.assertIn("_safe_log_turn", src)
        self.assertIn("prior_turns=prior_turns", src)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    )
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n{'=' * 60}\nF10 offline suite: {total - failed}/{total} passed\n{'=' * 60}")
    sys.exit(1 if failed else 0)
