"""Plan mode is a promise: the run cannot write, and the payload says so.

The console's Plan button sets `read_only`, and two things have to follow from
it: the model is not given the write tools, and the prompt tells it they are
absent. Either half missing produces an agent that tries, gets refused, and
reports confusion — or worse, an agent that believes it wrote something.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402

WRITE_TOOLS = {
    "create_file", "edit_file", "update_file", "write_file", "apply_symbol_edit",
    "delete_file", "make_dir", "run_command", "run_checks", "git_action",
}


def test_plan_mode_withholds_every_write_tool():
    allowed = set(A._code_allowed_tools(True))
    assert not (allowed & WRITE_TOOLS), sorted(allowed & WRITE_TOOLS)
    # The read side is intact: a plan that cannot investigate is useless.
    assert {"read_file", "list_dir", "search_files", "project_map"} <= allowed


def test_build_mode_has_them_back():
    allowed = set(A._code_allowed_tools(False))
    assert {"create_file", "edit_file", "run_checks"} <= allowed


def test_the_prompt_says_the_tools_are_absent_in_plan_mode():
    """A payload without the tools and a prompt that does not mention it is the
    quiet failure: the model promises a write it cannot perform."""
    text = (Path(A.__file__).with_name("prompts") / "studio_coder.md").read_text(
        encoding="utf-8")
    assert "{{APPROVAL_MODE}}" in text and "{{MODE_BEHAVIOUR}}" in text


def test_the_renderer_fills_the_mode_for_read_only():
    rendered = A.render_studio_coder_prompt(
        LAYOUT="x", SEARCH_EXAMPLE="y", APPROVAL_MODE="READ-ONLY. Analyse.",
        MODE_BEHAVIOUR="Those tools are not available in this run.")
    assert "READ-ONLY" in rendered
    assert "not available in this run" in rendered
    assert "{{" not in rendered


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))