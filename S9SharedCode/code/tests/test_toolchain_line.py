"""The toolchain line: measured once, cached, and never re-probed by the model.

Live: five run_command calls in a row asking `python --version`,
`pytest --version`, `where ruff` before a single file was written.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402


def test_the_line_names_what_is_installed():
    line = A._toolchain_line()
    assert line.startswith("Toolchain: ")
    assert "Python" in line
    # Whatever is present must be named once, not twice ("pytest pytest 9.0.3").
    for tool in ("pytest", "ruff", "git"):
        assert f"{tool} {tool}" not in line


def test_the_line_is_computed_once():
    first = A._toolchain_line()
    A._TOOLCHAIN_CACHE["line"] = "SENTINEL"
    assert A._toolchain_line() == "SENTINEL"
    A._TOOLCHAIN_CACHE["line"] = first