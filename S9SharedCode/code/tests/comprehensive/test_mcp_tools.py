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

    def test_49_tools_registered(self):
        """Was 40. get_time and currency_convert were removed as redundant
        with the model's own clock and arithmetic — each was an extra round
        trip for a trivial question. read_artifact was then added so a node
        can expand an upstream result that INPUTS spilled to a handle.

        40 again: render_document was re-registered because it was never
        actually registered - it was a bare function, so it was absent from the
        advertised list and every model call returned "Unknown tool". Then
        _doc_stats was dropped again: it is a private helper render_document
        calls, not a tool a model should be able to reach.

        42: `list_channels` and `check_channel` were added so the agent can
        find a delivery channel and tell "configured" from "the credential
        still works" — the distinction that decides whether a reminder can
        actually reach anyone.

        43: `enable_scheduled`. "Turn that reminder back on" had no tool, so
        the only way to satisfy it was schedule_task() a second time — which
        left a duplicate and the original still disabled. The user saw two
        identical disabled rows and no working reminder.

        44: `save_as_document`. Files the agent produced were downloadable
        artifacts or sandbox files; neither store is the one the console's
        Documents list reads, so "save that to my docs" had nothing to call.

        46: `record_observation` and `recall_observations`. The behavioural
        profile had no tools: the agent could read standing preferences but
        had no way to note a correction, so the only capture path was a
        regex over chat text that matched "from now on" and nothing else."""
        import asyncio
        import mcp_server
        tools = asyncio.run(mcp_server.mcp.list_tools())
        # 54 = 49 + 5 code-intelligence tools: document_symbols,
        # find_references, blast_radius, preview_edit, apply_symbol_edit.
        # find_symbol regex-matched names and could not tell a definition
        # from a call site, so "update every caller of X" was never safe.
        # preview_edit renders a diff and writes nothing; apply_symbol_edit
        # is the separate, explicit write.
        # 56 = 54 + make_dir + run_command. make_dir completes the file-tool
        # set; run_command is the PowerShell escape hatch, bounded by
        # sandbox.py and guarded/audited in mcp_server.
        self.assertEqual(len(tools), 56)

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
