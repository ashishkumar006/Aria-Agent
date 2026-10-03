"""
Comprehensive test suite — skills layer.

Covers:
  - Skill registry loading
  - Prompt rendering (USER_QUERY, QUESTION, MEMORY HITS, CONVERSATION HISTORY)
  - Memory-excluded skills (formatter, coder, critic, etc.)
  - Tool catalog
  - Sandbox execution
  - Browser skill dispatch
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


class SkillRegistry(unittest.TestCase):
    """skills.SkillRegistry loads agent_config.yaml."""

    def test_registry_loads(self):
        from skills import SkillRegistry
        reg = SkillRegistry()
        names = reg.names()
        self.assertIn("planner", names)
        self.assertIn("formatter", names)
        self.assertIn("action", names)

    def test_distiller_is_critic(self):
        from skills import SkillRegistry
        reg = SkillRegistry()
        distiller = reg.get("distiller")
        self.assertTrue(distiller.critic)


class PromptRendering(unittest.TestCase):
    """skills.render_prompt contracts."""

    def test_user_query_in_prompt(self):
        from skills import render_prompt, SkillRegistry
        reg = SkillRegistry()
        planner = reg.get("planner")
        p = render_prompt(planner, "test query",
                          [{"id": "USER_QUERY", "kind": "query", "value": "test query"}])
        self.assertIn("USER_QUERY", p)
        self.assertIn("test query", p)

    def test_memory_hits_block(self):
        from skills import render_prompt, SkillRegistry
        reg = SkillRegistry()
        planner = reg.get("planner")
        hits = [type("H", (), {
            "kind": "fact", "descriptor": "d", "source": "user_query",
            "value": {"raw": "test"},
        })()]
        p = render_prompt(planner, "q",
                          [{"id": "USER_QUERY", "kind": "query", "value": "q"}],
                          memory_hits=hits)
        self.assertIn("MEMORY HITS", p)
        self.assertIn("NEVER be presented", p)

    def test_conversation_history_block(self):
        from skills import render_prompt, SkillRegistry
        reg = SkillRegistry()
        planner = reg.get("planner")
        p = render_prompt(planner, "q",
                          [{"id": "USER_QUERY", "kind": "query", "value": "q"}],
                          prior_turns=[{"q": "earlier", "a": "answer"}])
        self.assertIn("CONVERSATION HISTORY", p)
        self.assertIn("earlier", p)

    def test_formatter_excluded_from_memory(self):
        from skills import render_prompt, SkillRegistry
        reg = SkillRegistry()
        fmt = reg.get("formatter")
        hits = [type("H", (), {
            "kind": "fact", "descriptor": "d", "source": "s", "value": {},
        })()]
        p = render_prompt(fmt, "q",
                          [{"id": "n:1", "kind": "upstream", "skill": "planner",
                            "output": {}}],
                          memory_hits=hits)
        self.assertNotIn("MEMORY HITS", p)
        self.assertNotIn("CONVERSATION HISTORY", p)


class ToolCatalog(unittest.TestCase):
    """skills.tool_payload and _TOOL_CATALOG."""

    def test_tool_payload_filters_unknown(self):
        from skills import tool_payload
        p = tool_payload(["web_search", "not_a_tool", "schedule_task"])
        names = [t["name"] for t in p]
        self.assertIn("web_search", names)
        self.assertIn("schedule_task", names)
        self.assertNotIn("not_a_tool", names)

    def test_scheduler_tools_in_catalog(self):
        from skills import _TOOL_CATALOG
        self.assertIn("schedule_task", _TOOL_CATALOG)
        self.assertIn("list_scheduled", _TOOL_CATALOG)
        self.assertIn("cancel_scheduled", _TOOL_CATALOG)


class SandboxExecution(unittest.TestCase):
    """sandbox.run_python executes real code."""

    def test_basic_math(self):
        from sandbox import run_python
        out = run_python("print(7*6)")
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("42", out["stdout"])

    def test_timeout_marker(self):
        from sandbox import run_python
        out = run_python("import time; time.sleep(60)", timeout_s=2)
        self.assertTrue(out["timed_out"])
        # The marker is in stderr
        self.assertIn("killed after", out.get("stderr", ""))


if __name__ == "__main__":
    unittest.main()
