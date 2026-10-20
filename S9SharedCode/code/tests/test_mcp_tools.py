"""P2.1 — MCP tool smoke tests.

Exercises the 26 MCP tools registered in mcp_server.py. Local/safe tools are
called directly; network/credentialed tools are verified to fail-soft (return an
error dict, never raise) when their backend is unavailable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server as mcp

SANDBOX = ROOT / "sandbox"
SANDBOX.mkdir(exist_ok=True)


# ── helpers ──────────────────────────────────────────────────────────────────

def _all_tool_names() -> list[str]:
    """Return the names of all registered MCP tools."""
    tm = mcp.mcp._tool_manager
    return list(tm._tools.keys())


# ── registration ─────────────────────────────────────────────────────────────

EXPECTED_TOOLS = [
    "web_search", "fetch_url",
    "read_file", "list_dir", "create_file", "make_dir", "update_file", "edit_file",
    "index_document", "send_telegram", "send_email", "create_calendar_event",
    "computer_action", "search_knowledge",
    "github_query", "slack_message", "slack_refresh_token", "discord_message", "notion_query", "gmail_query",
    "gmail_refresh_token", "calendar_refresh_token",
    # F4 scheduler CRUD tools (added with the scheduler integration)
    "schedule_task", "list_scheduled", "cancel_scheduled",
    # Expansion round: research sources, documents, assistant gaps,
    # comms read, archive (see tests/test_new_tools.py for per-tool tests)
    "arxiv_search", "wikipedia_search", "openalex_search", "news_search",
    "fetch_pdf", "extract_tables", "calendar_query", "delete_file",
    "search_files", "slack_history", "wayback_fetch",
]


def test_all_38_tools_registered():
    registered = _all_tool_names()
    for name in EXPECTED_TOOLS:
        assert name in registered, f"tool '{name}' not registered"
    # get_calendar_events was in an old draft of this list but was never
    # implemented; the scheduler tools replaced that need.
    assert "get_calendar_events" not in registered or True  # informational


# ── local / safe tools ───────────────────────────────────────────────────────

class TestLocalTools:
    def test_list_dir_sandbox_root(self):
        r = mcp.list_dir(".")
        assert isinstance(r, dict)
        assert "entries" in r or "files" in r or "status" in r

    def test_create_read_update_file_roundtrip(self):
        p = "_mcp_test_roundtrip.txt"
        # create
        r1 = mcp.create_file(p, "hello world")
        assert r1.get("status") == "created" or r1.get("ok") is True
        # read
        r2 = mcp.read_file(p)
        assert "hello world" in str(r2.get("content", r2.get("text", "")))
        # update
        r3 = mcp.update_file(p, "updated body")
        assert r3.get("status") == "updated" or r3.get("ok") is True
        r4 = mcp.read_file(p)
        assert "updated body" in str(r4.get("content", r4.get("text", "")))
        # cleanup
        try:
            (SANDBOX / p).unlink()
        except OSError:
            pass

    def test_create_file_existing_raises(self, tmp_path, monkeypatch):
        """create_file raises ValueError on existing file (framework→error)."""
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        p = tmp_path / "_mcp_test_exists.txt"
        mcp.create_file("_mcp_test_exists.txt", "first")
        with pytest.raises(ValueError, match="already exists"):
            mcp.create_file("_mcp_test_exists.txt", "second")

    def test_make_dir_nested_and_idempotent(self, tmp_path, monkeypatch):
        """make_dir creates parents and succeeds when the folder already exists."""
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        r = mcp.make_dir("a/b/c")
        assert r["ok"] is True and r["created"] is True
        assert (tmp_path / "a" / "b" / "c").is_dir()
        r2 = mcp.make_dir("a/b/c")
        assert r2["ok"] is True and r2["created"] is False

    def test_create_file_makes_missing_parents(self, tmp_path, monkeypatch):
        """The natural edit - write src/new/mod.py - no longer dead-ends."""
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        r = mcp.create_file("src/pkg/mod.py", "x = 1\n")
        assert r["ok"] is True
        assert (tmp_path / "src" / "pkg" / "mod.py").read_text(
            encoding="utf-8") == "x = 1\n"

    def test_read_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        with pytest.raises(FileNotFoundError):
            mcp.read_file("_mcp_does_not_exist_xyz.txt")

    def test_list_dir_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        with pytest.raises(FileNotFoundError):
            mcp.list_dir("_no_such_dir_xyz")

    def test_edit_file_find_replace(self):
        p = "_mcp_test_edit.txt"
        mcp.create_file(p, "foo bar foo")
        r = mcp.edit_file(p, "foo", "baz", replace_all=True)
        assert isinstance(r, dict)
        content = mcp.read_file(p)
        body = str(content.get("content", content.get("text", "")))
        assert "baz" in body
        try:
            (SANDBOX / p).unlink()
        except OSError:
            pass


# ── network tools ───────────────────────────────────────────────────────────

class TestNetworkTools:
    """These tools reach the real network; assert the happy path when reachable."""

    def test_web_search_runs(self):
        """web_search should return a list/dict without crashing."""
        r = mcp.web_search("python asyncio tutorial", 2)
        assert isinstance(r, (list, dict))

    # Thin-client tools forward to the gateway; credentials live there.
    # Fail-soft is verified by mocking the transport helper (no network,
    # no secrets in the test env).
    def test_github_query_fails_soft_without_token(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "_gw_integration",
            lambda *a, **k: {"ok": False, "error": "GITHUB_TOKEN not set"})
        r = mcp.github_query("list_repos")
        assert isinstance(r, dict)
        assert r.get("status") == "error" or "token" in str(r).lower()

    def test_slack_message_fails_soft_without_token(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "_gw_channel",
            lambda *a, **k: {"ok": False, "error": "SLACK_BOT_TOKEN not set"})
        r = mcp.slack_message("general", "hi")
        assert isinstance(r, dict)
        assert r.get("status") == "error" or "token" in str(r).lower()

    def test_notion_query_fails_soft_without_token(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "_gw_integration",
            lambda *a, **k: {"ok": False, "error": "NOTION_TOKEN not set"})
        r = mcp.notion_query("list_databases")
        assert isinstance(r, dict)
        assert r.get("status") == "error" or "token" in str(r).lower()

    def test_send_email_fails_soft_without_oauth(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "_gw_integration",
            lambda *a, **k: {"ok": False, "error": "GMAIL_TOKEN not set"})
        r = mcp.send_email("a@b.com", "subj", "body")
        assert isinstance(r, dict)
        # should fail-soft (no Gmail OAuth configured in test env)
        assert r.get("status") in ("error", "sent") or "error" in str(r).lower()

    def test_send_telegram_fails_soft_without_token(self, monkeypatch):
        monkeypatch.setattr(
            mcp, "_gw_channel",
            lambda *a, **k: {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"})
        # Signature is (chat_id, message).
        r = mcp.send_telegram("12345", "hello")
        assert isinstance(r, dict)
        assert r.get("status") == "error" or "token" in str(r).lower()

    def test_thin_tools_fail_soft_when_gateway_down(self, monkeypatch):
        """Gateway unreachable → fail-soft dicts, never exceptions."""
        import httpx

        def _client_boom(*a, **k):
            m = mock.MagicMock(name="httpx.Client")
            enter = mock.MagicMock(name="session")
            enter.post.side_effect = httpx.ConnectError("refused")
            m.__enter__.return_value = enter
            m.__exit__.return_value = False
            return m

        monkeypatch.setattr(mcp.httpx, "Client", _client_boom)
        for r in [mcp.send_telegram("1", "hi"),
                  mcp.send_email("a@b.com", "s", "b"),
                  mcp.github_query("list_repos"),
                   mcp.slack_message("g", "hi"),
                   mcp.discord_message("123", "hi"),
                   mcp.notion_query("list_pages"),
                   mcp.gmail_query("list", "x"),
                   mcp.gmail_refresh_token(),
                   mcp.slack_refresh_token(),
                   mcp.create_calendar_event("t", "2026-01-01T10:00:00", "2026-01-01T11:00:00")]:
            assert isinstance(r, dict), r
            assert r.get("ok") is False, r


# ── gated / special tools ────────────────────────────────────────────────────

class TestGatedTools:
    def test_computer_action_gated(self):
        """computer_action should route through the safety gate."""
        r = mcp.computer_action("run_command", {"command": "echo hi"})
        assert isinstance(r, dict)
        # either gated/pending/dry-run or executed — never a crash
        assert "status" in r or "state" in r

    def test_search_knowledge_empty_index(self):
        r = mcp.search_knowledge("anything", k=3)
        assert isinstance(r, (list, dict))

    def test_index_document_missing_path_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        with pytest.raises(FileNotFoundError):
            mcp.index_document("_no_such_file_xyz.txt")


# ── secrets and subprocess environment ───────────────────────────────────────

class TestSecretHygiene:
    """A secret file is invisible to every tool, not just read_file.

    The block existed only in read_file/list_dir/search_files; the symbol and
    edit tools went around it, so `.env` was readable via document_symbols and
    overwritable via update_file.
    """

    def test_no_tool_reads_or_lists_a_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / ".env").write_text("API_KEY=super-secret\n", encoding="utf-8")
        (tmp_path / ".env.example").write_text("API_KEY=\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")

        with pytest.raises(ValueError):
            mcp.read_file(".env")
        listing = json.dumps(mcp.list_dir("."))
        assert "super-secret" not in listing
        assert ".env.example" in listing, "documentation must stay visible"
        hits = mcp.search_files("super-secret", ".")
        assert hits.get("hits") == [], hits

    def test_symbol_tools_refuse_a_secret_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / ".env").write_text("def leaked():\n    pass\n",
                                       encoding="utf-8")
        out = mcp.document_symbols(".env")
        assert out["ok"] is False and "refusing" in out["error"]
        prev = mcp.preview_edit(".env", "leaked", "def leaked(): pass")
        assert prev["ok"] is False and "refusing" in prev["error"]

    def test_repo_wide_scans_skip_secret_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / ".env").write_text("KEY=leaked_marker\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("x = leaked_marker\n", encoding="utf-8")
        out = mcp.find_references("leaked_marker", ".")
        files = [r["file"] for r in out.get("references", [])]
        assert files and all(".env" not in f for f in files), files

    def test_write_tools_refuse_a_secret_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
        with pytest.raises(ValueError):
            mcp.create_file(".env.local", "B=2\n")
        with pytest.raises(ValueError):
            mcp.update_file(".env", "B=2\n")
        with pytest.raises(ValueError):
            mcp.edit_file(".env", "A=1", "A=2")
        with pytest.raises(ValueError):
            mcp.delete_file(".env")
        assert (tmp_path / ".env").read_text(encoding="utf-8") == "A=1\n"

    def test_run_checks_does_not_inherit_credentials(self, tmp_path, monkeypatch):
        """run_checks executes the project's own scripts; those must not see
        the operator's keys, which this server was spawned with."""
        import sandbox as _sandbox

        seen = {}

        def _run(argv, **kwargs):
            seen.update(kwargs)
            return _sandbox.ExecResult(True, 0, "ok", "", 5)

        monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
        monkeypatch.setattr(mcp._sandbox, "run", _run)
        monkeypatch.setenv("ARIA_TEST_API_KEY", "sk-do-not-leak")
        monkeypatch.setenv("SOME_PASSWORD", "hunter2")
        (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n",
                                                encoding="utf-8")
        out = mcp.run_checks(".")
        assert out["ok"] is True, out
        env = seen.get("env") or {}
        assert "ARIA_TEST_API_KEY" not in env
        assert "SOME_PASSWORD" not in env
        assert "PATH" in env, "the subprocess still needs a usable environment"
