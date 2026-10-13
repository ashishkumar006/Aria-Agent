"""The editor must be able to ASK, and must not be able to WRITE.

`/api/code/ask` and `/api/code/checks` are what turn the Code view from a text
editor into an AI one. The safety property matters more than the feature: a
question typed into a panel must not be able to rewrite files behind the
user's back, so the AI surface is read-only over HTTP.
"""
import inspect

import agent_server
import pytest


def _routes():
    return {(r.path, m) for r in agent_server.app.routes
            for m in getattr(r, "methods", set())}


def test_both_ai_endpoints_exist():
    paths = {p for p, _ in _routes() if p.startswith("/api/code")}
    assert "/api/code/ask" in paths, "the editor cannot ask the agent"
    assert "/api/code/checks" in paths, "the editor cannot run real checks"


def test_the_ai_surface_exposes_no_write_verb():
    """A question must never be able to edit a file."""
    for p in ("/api/code/ask", "/api/code/checks"):
        verbs = {m for path, m in _routes() if path == p}
        assert not (verbs & {"PUT", "PATCH", "DELETE"}), (
            f"{p} exposes a write verb: {verbs}")


def test_ask_sends_the_draft_not_just_the_disk_copy():
    """Unsaved edits are what the user is looking at."""
    src = inspect.getsource(agent_server.code_ask)
    assert 'body.get("text")' in src or "body.get(\"text\")" in src, (
        "ask does not receive the draft buffer")
    assert "buffer_text" in src


def test_ask_refuses_an_empty_question():
    assert '"question is required"' in inspect.getsource(agent_server.code_ask)


def test_ask_bounds_the_question_and_the_buffer():
    src = inspect.getsource(agent_server.code_ask)
    assert "too long" in src, "an unbounded question can blow the context"
    assert "_CODE_MAX_BYTES" in src, "the draft buffer is unbounded"


def test_ask_context_is_path_confined():
    """Attached files go through the same guard as every code endpoint."""
    src = inspect.getsource(agent_server.code_ask)
    assert "_code_abs(" in src, "attached files skip path confinement"


def test_checks_share_the_agents_detector():
    """Two definitions of "has a checker" means the panel can say green while
    the agent says fail."""
    from mcp_server import _CHECK_PROFILES
    src = inspect.getsource(agent_server.code_checks)
    assert "_CHECK_PROFILES" in src
    assert len(_CHECK_PROFILES) >= 6, "the detector lost entries"


def test_the_pytest_argv_is_not_a_marker_filter():
    """`-m pytest` against the resolved pytest executable matches nothing.

    `shutil.which("pytest")` is the console script, so the args must be the
    executable's own. It ran `pytest -m pytest`, matched no test, and exited 5,
    which reads as "tests failed".
    """
    from mcp_server import _CHECK_PROFILES
    for marker, tool, argv in _CHECK_PROFILES:
        assert argv[0] != "-m", (
            f"{tool} (for {marker}) starts with '-m': the executable is "
            f"already resolved, so '-m' is parsed as a marker filter")


def test_checks_reports_honest_failures():
    src = inspect.getsource(agent_server.code_checks)
    assert "no checker detected" in src, (
        "a project with no checker must say so, not imply it passed")
    assert '"ok": ran' in src or "ok: ran" in src.replace(" ", " ")


def test_checks_defaults_to_a_real_root():
    """An empty path must not run pytest in whatever the cwd happens to be."""
    src = inspect.getsource(agent_server.code_checks)
    assert "_CODE_ROOTS" in src, "checks has no default working directory"