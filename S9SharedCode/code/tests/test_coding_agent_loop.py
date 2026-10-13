"""The Coder skill must be able to verify its own work.

`create_file` returned ok:true for a file with a syntax error, and the node
reported success: a coding agent that cannot run a checker will ship code that
does not compile, and nothing in this system catches it afterwards.
"""
import pathlib

import pytest
import yaml

import mcp_server as mcp
import skills

CFG = pathlib.Path(skills.__file__).parent / "agent_config.yaml"
PROMPT = pathlib.Path(skills.__file__).parent / "prompts" / "coder.md"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Run the tools against a temporary SANDBOX.

    `_safe` refuses any path outside SANDBOX, which is correct and is itself
    worth keeping - so the tests have to live inside it rather than pointing at
    a tmp_path and having the guard (rightly) reject them.
    """
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    return tmp_path


@pytest.fixture(scope="module")
def coder_cfg():
    return (yaml.safe_load(CFG.read_text(encoding="utf-8")) or {})["coder"]


def test_the_coder_can_run_the_projects_own_checks(coder_cfg):
    assert "run_checks" in coder_cfg["tools_allowed"], (
        "the coder cannot verify its own work - it can only hope")


def test_the_coder_can_review_its_own_diff(coder_cfg):
    assert "git_inspect" in coder_cfg["tools_allowed"]


def test_the_coder_can_find_definitions_not_just_text(coder_cfg):
    assert "find_symbol" in coder_cfg["tools_allowed"]


def test_the_output_ceiling_can_hold_a_real_edit(coder_cfg):
    """1500 tokens truncated a new function mid-body, silently."""
    assert coder_cfg["max_tokens"] >= 16_000, coder_cfg["max_tokens"]


def test_the_prompt_states_the_loop_in_order():
    text = PROMPT.read_text(encoding="utf-8")
    read = text.find("READ before you write")
    edit = text.find("EDIT with")
    check = text.find("RUN `run_checks`")
    fix = text.find("Only when `run_checks` returns")
    assert -1 not in (read, edit, check, fix), "a step of the loop is missing"
    assert read < edit < check < fix, (
        "the edit->check->fix loop is not stated in order")


def test_the_prompt_says_not_to_trade_a_test_for_a_passing_check():
    low = PROMPT.read_text(encoding="utf-8").lower()
    assert "weaken an existing test" in low, (
        "the prompt does not forbid making checks pass by deleting them")


def test_the_prompt_forbids_claiming_success_without_a_check():
    low = PROMPT.read_text(encoding="utf-8").lower()
    assert "never report work as done while" in low


def test_run_checks_reports_a_real_exit_code(monkeypatch, sandbox):
    import subprocess

    class _P:
        returncode = 1
        stdout = "2 failed"
        stderr = "Traceback..."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    (sandbox / "pyproject.toml").write_text("[tool.pytest]", encoding="utf-8")
    out = mcp.run_checks(str(sandbox))
    assert out["ok"] is False
    assert out["results"][0]["exit_code"] == 1
    assert "2 failed" in out["results"][0]["stdout"]


def test_run_checks_passes_only_on_exit_zero(monkeypatch, sandbox):
    import subprocess

    class _P:
        returncode = 0
        stdout = "42 passed"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    (sandbox / "pyproject.toml").write_text("[tool.pytest]", encoding="utf-8")
    out = mcp.run_checks(str(sandbox))
    assert out["ok"] is True, out


def test_run_checks_says_so_when_nothing_is_detected(sandbox):
    out = mcp.run_checks(str(sandbox))
    assert out["ok"] is False
    assert "no checker detected" in out["error"]


def test_find_symbol_separates_definitions_from_usages(sandbox):
    (sandbox / "m.py").write_text(
        "def target():\n    return 1\n\nx = target()\n"
        "# target() in a comment\n", encoding="utf-8")
    out = mcp.find_symbol("target", str(sandbox))
    assert out["definition_count"] >= 1
    assert out["usage_count"] >= 2
    assert all("def " not in u["text"] for u in out["usages"]), (
        "a definition leaked into the usages list")


def test_the_sandbox_guard_still_rejects_escapes(sandbox):
    """The tools are new; the boundary around them is not."""
    import tempfile

    with tempfile.TemporaryDirectory() as outside:
        with pytest.raises(ValueError):
            mcp.find_symbol("x", outside)


def test_the_tools_are_advertised_not_just_registered():
    for t in ("run_checks", "git_inspect", "find_symbol"):
        assert t in skills._TOOL_CATALOG, f"{t} is not advertised to the model"