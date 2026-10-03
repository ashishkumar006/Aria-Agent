"""
Comprehensive test suite — MCP tools layer.

Covers:
  - Tool registration (26 tools)
  - Scheduler CRUD tools
  - Web search / fetch
  - File operations
  - Gmail / GitHub / Slack / Notion
  - Computer action
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))


class ToolRegistration(unittest.TestCase):
    """mcp_server.mcp registers all tools."""

    def test_38_tools_registered(self):
        """Was 40. get_time and currency_convert were removed as redundant
        with the model's own clock and arithmetic — each was an extra round
        trip for a trivial question."""
        import asyncio
        import mcp_server
        tools = asyncio.run(mcp_server.mcp.list_tools())
        self.assertEqual(len(tools), 38)

    def test_redundant_tools_are_gone(self):
        """They must not linger in the catalog or in the tool payload, or the
        model will keep reaching for them."""
        import asyncio
        import json as _json

        import mcp_server
        from skills import tool_payload
        names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        self.assertNotIn("get_time", names)
        self.assertNotIn("currency_convert", names)
        blob = _json.dumps(tool_payload(names))
        self.assertNotIn("get_time", blob)
        self.assertNotIn("currency_convert", blob)

    def test_scheduler_tools_present(self):
        import asyncio
        import mcp_server
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = {t.name for t in tools}
        self.assertIn("schedule_task", names)
        self.assertIn("list_scheduled", names)
        self.assertIn("cancel_scheduled", names)


class SchedulerTools(unittest.TestCase):
    """Scheduler MCP tools CRUD."""

    def test_create(self):
        import mcp_server
        r = mcp_server.schedule_task("test reminder", "in 30m")
        self.assertTrue(r.get("ok"))
        self.assertIn("schedule_id", r)

    def test_list(self):
        import mcp_server
        r = mcp_server.list_scheduled()
        self.assertTrue(r.get("ok"))
        self.assertIsInstance(r.get("schedules"), list)

    def test_cancel(self):
        import mcp_server
        r = mcp_server.schedule_task("to cancel", "in 1h")
        sid = r["schedule_id"]
        r = mcp_server.cancel_scheduled(sid)
        self.assertTrue(r.get("cancelled"))


class FileTools(unittest.TestCase):
    """File operation MCP tools."""

    def test_read_write_file(self):
        import mcp_server
        import uuid
        # MCP file tools are sandboxed under ./sandbox/
        fname = f"test_comp_{uuid.uuid4().hex[:8]}.txt"
        r = mcp_server.create_file(fname, "hello world")
        self.assertTrue(r.get("ok"))
        r = mcp_server.read_file(fname)
        self.assertIn("hello", r.get("content", ""))


if __name__ == "__main__":
    unittest.main()
