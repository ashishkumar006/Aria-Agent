"""The coding agent must see exactly the workspace the user sees.

Three properties, each a live bug once:

- the tool child is rooted at the workspace FOLDER, not its parent - the
  parent exists only so client paths read "name/...", and handing it to the
  agent exposed siblings (with the shipped default, the state dir and
  agent.token inside it)
- the system prompt must not describe a tree the agent is not in - it used to
  tell the model the repo was at S9SharedCode/code while the shipped default
  workspace is an empty scratch folder, so the model hunted, found nothing
  and looped
- an apply_symbol_edit proposal must ship a real diff to the review dialog,
  or the human approves by summary alone
"""
import inspect
from pathlib import Path

import agent_server
import mcp_server
import pytest


@pytest.fixture()
def ws(tmp_path):
    """A fake workspace in the shipped shape: base = parent, one root."""
    root = tmp_path / "ws"
    root.mkdir()
    old_base, old_allowed = agent_server._CODE_BASE, agent_server._CODE_ALLOWED
    agent_server._CODE_BASE = tmp_path
    agent_server._CODE_ALLOWED = (root.resolve(),)
    yield tmp_path, root
    agent_server._CODE_BASE, agent_server._CODE_ALLOWED = old_base, old_allowed


def test_the_agent_root_is_the_folder_not_its_parent(ws):
    tmp_path, root = ws
    assert agent_server._code_agent_root() == root.resolve()


def test_agent_paths_are_folder_relative(ws):
    tmp_path, root = ws
    (root / "pkg").mkdir()
    (root / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    # The client speaks "ws/pkg/a.py"; the tool child must see "pkg/a.py".
    assert agent_server._code_agent_rel("ws/pkg/a.py") == "pkg/a.py"
    assert agent_server._code_agent_abs("pkg/a.py") == (root / "pkg" / "a.py")
    # And the parent's contents are not reachable from the agent root.
    (tmp_path / "sibling.py").write_text("secret\n", encoding="utf-8")
    assert agent_server._code_agent_abs("../sibling.py") is None
    assert agent_server._code_agent_abs("ws/../sibling.py") is None


def test_the_shipped_default_never_roots_the_agent_at_the_state_dir():
    assert agent_server._code_agent_root() == agent_server._CODE_PLAIN
    # i.e. the state dir - agent.token's home - is NOT the tool sandbox.
    assert agent_server._code_agent_root() != agent_server._CODE_PLAIN.parent


def test_the_prompt_never_names_directories_the_agent_cannot_see():
    src = inspect.getsource(agent_server.code_agent)
    assert "All tool paths are relative to the project root" not in src, (
        "the prompt still tells the model it is in the repo when the shipped "
        "default is an empty scratch folder")
    assert "scratch workspace" in src


def test_apply_symbol_edit_proposals_carry_a_real_diff(ws):
    tmp_path, root = ws
    (root / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    summary, diff = agent_server._code_write_diff(
        "apply_symbol_edit",
        {"path": "mod.py", "name": "f", "code": "def f():\n    return 2",
         "mode": "replace"})
    assert "f" in summary
    assert diff.strip(), "the review dialog was shown an empty diff"
    assert "-    return 1" in diff and "+    return 2" in diff


def test_env_files_are_secret_but_the_example_is_not():
    assert mcp_server._is_secret_file(".env")
    assert mcp_server._is_secret_file(".env.local")
    assert mcp_server._is_secret_file(".ENV")
    assert not mcp_server._is_secret_file(".env.example")
    assert not mcp_server._is_secret_file("env.py")


def test_read_file_refuses_env_even_inside_the_sandbox(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "SANDBOX", tmp_path)
    with pytest.raises(ValueError):
        mcp_server.read_file(".env")


def test_search_never_walks_into_env_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("SECRET_MARKER=1\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("PRESENT = True\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "SANDBOX", tmp_path)
    leaked = mcp_server.search_files("SECRET_MARKER", ".")
    assert leaked["hits"] == [], leaked
    found = mcp_server.search_files("PRESENT", ".")
    assert found["hits"], found


def test_list_dir_hides_env_files(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("KEY=secret\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "SANDBOX", tmp_path)
    names = mcp_server.list_dir(".")["names"]
    assert ".env" not in names
    assert "ok.py" in names
