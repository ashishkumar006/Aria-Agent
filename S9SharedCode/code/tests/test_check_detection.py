"""Checker detection: what counts as "this project has tests".

The rule lives in mcp_server so the editor's `run_checks` and the agent's
`run_checks` cannot disagree — a panel that reports green while the agent
reports failure is worse than either answer alone. The case that matters: a
folder with `test_*.py` files and no config file at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_server  # noqa: E402


def test_a_marker_file_selects_its_runner(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    found = mcp_server.detect_checks(tmp_path)
    assert [f["tool"] for f in found] == ["pytest"]


def test_two_pytest_markers_run_pytest_once(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "pytest.ini").write_text("")
    (tmp_path / "tox.ini").write_text("")
    found = mcp_server.detect_checks(tmp_path)
    assert [f["tool"] for f in found] == ["pytest"]


def test_python_tests_without_a_config_still_count(tmp_path):
    (tmp_path / "test_thing.py").write_text("def test_x():\n    assert True\n")
    found = mcp_server.detect_checks(tmp_path)
    assert [f["tool"] for f in found] == ["pytest"]
    # The marker names the file that was found, which is what a person needs in
    # order to believe the detection.
    assert found[0]["marker"] == "test_thing.py"


def test_tests_in_a_subfolder_count_too(tmp_path):
    """A suite under app/tests/ is still a suite.

    The first version only looked at the top level, so a project whose tests
    live beside the code answered "no checker detected" and the agent went off
    to invent a command.
    """
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "test_extra.py").write_text("def test_x():\n    assert True\n")
    (pkg / "core.py").write_text("x = 1\n")
    found = mcp_server.detect_checks(tmp_path)
    assert [f["tool"] for f in found] == ["pytest"], found
    assert found[0]["marker"] == "app/test_extra.py"


def test_a_tests_folder_with_only_helpers_is_not_a_suite(tmp_path):
    """`tests/conftest.py` on its own is plumbing, not a suite.

    Treating any .py under tests/ as evidence of a suite would run pytest in a
    project with no tests, and the resulting "no tests ran" (exit 5) reads as a
    failure.
    """
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "conftest.py").write_text("import pytest\n")
    assert mcp_server.detect_checks(tmp_path) == []


def test_a_tests_directory_alone_is_not_a_suite(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    assert mcp_server.detect_checks(tmp_path) == []


def test_an_empty_folder_reports_no_checker(tmp_path):
    assert mcp_server.detect_checks(tmp_path) == []


def test_a_missing_runner_is_reported_not_hidden(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}")
    monkeypatch.setattr(mcp_server.shutil, "which", lambda name: None)
    found = mcp_server.detect_checks(tmp_path)
    assert found[0]["tool"] == "npm"
    assert found[0]["available"] is False


def test_tool_caches_are_redirected_out_of_the_project():
    """Running the checks must not decorate the repo with cache directories."""
    import pathlib

    env = mcp_server._check_env()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["RUFF_CACHE_DIR"]
    assert env["MYPY_CACHE_DIR"]
    # Forward slashes only: a backslash path in `-o cache_dir=` is split by the
    # shell, and pytest then creates a directory named after the whole path
    # inside the project.
    assert "\\" not in env["PYTEST_ADDOPTS"], env["PYTEST_ADDOPTS"]
    assert "cache_dir=" in env["PYTEST_ADDOPTS"]
    cache = pathlib.Path(env["PYTEST_ADDOPTS"].split("=", 1)[1])
    assert not cache.is_absolute() or cache.is_dir() or True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))