"""The harness's post-write verification: which file does a path mean?

A project folder and a package inside it often share a name (`kvstore/kvstore`),
and then "kvstore/store.py" is both "the root's name plus a path" and "a real
path from the root". Resolving only the first reading made the check report
`checked: 0` on a real Python edit, which the agent then had to surface as
"not verified" — a verification step that verifies nothing is worse than none.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402


def test_a_plain_tool_relative_path(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    got = A._code_fast_verify(["a.py"], tmp_path)
    assert got["checked"] == 1
    assert [c["path"] for c in got["checks"]] == ["a.py"]
    assert got["ok"] is True


def test_a_client_path_with_the_root_name(tmp_path):
    (tmp_path / "a.py").write_text("y = 2\n")
    got = A._code_fast_verify([f"{tmp_path.name}/a.py"], tmp_path)
    assert got["checked"] == 1


def test_the_root_and_package_sharing_a_name(tmp_path):
    """The live case: project `kvstore` containing package `kvstore`."""
    pkg = tmp_path / "kvstore"
    pkg.mkdir()
    (pkg / "store.py").write_text("z = 3\n")
    got = A._code_fast_verify(["kvstore/store.py"], tmp_path)
    assert got["checked"] == 1, got
    assert got["ok"] is True


def test_a_broken_file_is_reported_not_skipped(tmp_path):
    (tmp_path / "bad.py").write_text("def nope(:\n")
    got = A._code_fast_verify(["bad.py"], tmp_path)
    assert got["checked"] == 1
    assert got["ok"] is False
    assert got["checks"][0]["kind"] == "python-ast"


def test_a_path_that_is_gone_is_not_counted(tmp_path):
    got = A._code_fast_verify(["missing.py"], tmp_path)
    assert got["checked"] == 0
    assert got["checks"] == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))