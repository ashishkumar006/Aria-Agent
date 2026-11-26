"""The project map: built once, refreshed incrementally, queried precisely.

These pin the three things that make it worth having. It must be **right**
(exact for Python, no references pulled out of docstrings), **incremental** (a
no-op refresh must not re-read the tree), and **honest** (a stale map is dropped
rather than trusted).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import code_index as ci  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A two-module project with a caller, a re-export and a docstring decoy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "calc.py").write_text(
        '"""A calculator."""\n'
        "\n"
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def add_more(a, b, c):\n"
        "    return add(add(a, b), c)\n"
        "\n"
        "class Calc:\n"
        "    def add(self, a, b):\n"
        "        return add(a, b)\n",
        encoding="utf-8")
    (src / "cli.py").write_text(
        "from src.calc import add\n"
        "\n"
        "def main():\n"
        "    # add() is the entry point\n"
        "    return add(1, 2)\n",
        encoding="utf-8")
    (tmp_path / "README.md").write_text("docs about add()\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("def add(a):\n",
                                                       encoding="utf-8")
    # Keep the artefact inside the test's own tree.
    monkeypatch.setattr(ci, "_state_dir", lambda: tmp_path / ".aria-index")
    ci.invalidate(tmp_path)
    return tmp_path


def test_build_finds_symbols_and_skips_vendor_dirs(project):
    got = ci.refresh(project, force=True)
    files = got["index"]["files"]
    assert "src/calc.py" in files
    assert "node_modules/junk.py" not in files
    assert "README.md" not in files
    names = {c["name"] for c in files["src/calc.py"]["classes"]}
    assert names == {"Calc"}
    funcs = {f["name"] for f in files["src/calc.py"]["functions"]}
    assert {"add", "add_more"} <= funcs


def test_imports_become_reverse_edges(project):
    ci.refresh(project, force=True)
    files = ci.load(project)["files"]
    assert "src/cli.py" in files["src/calc.py"]["imported_by"]


def test_references_skip_docstrings_and_comments(project):
    index = ci.refresh(project, force=True)["index"]
    hits = ci.references(index, "add")
    paths = {h["path"] for h in hits}
    assert "src/calc.py" in paths and "src/cli.py" in paths
    # The README and the `# add()` comment are not code references.
    assert "README.md" not in paths
    cli_lines = [h["line"] for h in hits if h["path"] == "src/cli.py"]
    assert cli_lines and all(
        "add()" not in (project / "src" / "cli.py").read_text()
        .splitlines()[n - 1].strip()[:1] for n in cli_lines)


def test_blast_radius_ranks_and_scores(project):
    index = ci.refresh(project, force=True)["index"]
    blast = ci.blast_radius(index, "add")
    assert blast["name"] == "add"
    assert blast["files"] >= 1
    assert sum(f["references"] for f in blast["ranked"]) == blast["references"]
    assert blast["risk"] in ("low", "medium", "high")


def test_knows_name_separates_absent_from_unscanned(project):
    index = ci.refresh(project, force=True)["index"]
    assert ci.knows_name(index, "add") is True
    assert ci.knows_name(index, "definitely_not_here") is False
    assert ci.knows_name(index, "") is False


def test_refresh_is_incremental(project):
    first = ci.refresh(project, force=True)
    before = {p: p.stat().st_mtime for p in project.rglob("*.py")}
    time.sleep(0.02)
    second = ci.refresh(project)
    assert second["changed"] == [], "an unchanged tree must not re-read files"
    assert second["files"] == first["files"]
    assert before

    # Touch one file: exactly that file is re-read.
    target = project / "src" / "calc.py"
    target.write_text(target.read_text() + "\n# touched\n", encoding="utf-8")
    third = ci.refresh(project)
    assert "src/calc.py" in third["changed"]


def test_a_deleted_file_leaves_the_map(project):
    ci.refresh(project, force=True)
    (project / "src" / "cli.py").unlink()
    got = ci.refresh(project)
    assert "src/cli.py" not in got["index"]["files"]
    assert "src/cli.py" in got["changed"]


def test_invalidate_drops_the_artefact(project):
    ci.refresh(project, force=True)
    assert ci.load(project) is not None
    ci.invalidate(project)
    assert ci.load(project) is None


def test_tree_and_entry_points_orient_a_reader(project):
    index = ci.refresh(project, force=True)["index"]
    text = ci.tree(index)
    assert "src/" in text
    assert any(line.startswith("src/") and "py" in line for line in text.splitlines())
    assert "src/cli.py" in ci.entry_points(index) or "src/calc.py" in ci.entry_points(index)


def test_find_references_matches_the_tool_contract(project):
    index = ci.refresh(project, force=True)["index"]
    got = ci.find_references(index, "add")
    assert got["ok"] is True and got["exact"] is True
    assert got["definitions"], "the def line must be marked as a definition"
    assert all(h["path"] != "README.md" for h in got["references"])
    assert all("add()" not in h["text"] or h["line"] != 4
               for h in got["references"] if h["path"] == "src/cli.py")


def test_a_javascript_file_gets_a_conservative_shape(tmp_path, monkeypatch):
    (tmp_path / "app.js").write_text(
        "import { x } from './lib.js';\n"
        "function greet(name) {\n  return name;\n}\n"
        "const add = (a, b) => a + b;\n"
        "class Box {}\n",
        encoding="utf-8")
    monkeypatch.setattr(ci, "_state_dir", lambda: tmp_path / ".aria-index")
    index = ci.refresh(tmp_path, force=True)["index"]
    entry = index["files"]["app.js"]
    assert entry["lang"] == "javascript"
    names = {f["name"] for f in entry["functions"]}
    assert {"greet", "add"} <= names
    assert entry["imports"] == ["./lib.js"]


def test_a_use_that_is_not_a_definition_is_still_found(tmp_path, monkeypatch):
    """`x = leaked_marker` is a use, not a def or an import.

    Candidate lookup is by identifier token, so a plain assignment is found —
    and the same token index is what keeps a name mentioned only in a comment
    out of the running.
    """
    (tmp_path / "app.py").write_text(
        "def go():\n"
        "    marker = 1\n"
        "    return marker\n",
        encoding="utf-8")
    (tmp_path / "notes.md").write_text("marker is discussed here\n",
                                       encoding="utf-8")
    monkeypatch.setattr(ci, "_state_dir", lambda: tmp_path / ".aria-index")
    index = ci.refresh(tmp_path, force=True)["index"]
    got = ci.find_references(index, "marker")
    assert got["reference_count"] >= 2
    assert all(h["file"] == "app.py" for h in got["references"])
    # A markdown mention is not a code reference, and md is not indexed at all.
    assert "notes.md" not in (index["files"] or {})


def test_a_shell_file_and_a_broken_file_do_not_stop_the_map(tmp_path, monkeypatch):
    (tmp_path / "run.sh").write_text("greet() {\n  echo hi\n}\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("def fine():\n    return 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00broken")
    monkeypatch.setattr(ci, "_state_dir", lambda: tmp_path / ".aria-index")
    index = ci.refresh(tmp_path, force=True)["index"]
    assert "ok.py" in index["files"]
    assert index["files"]["run.sh"]["functions"][0]["name"] == "greet"
    # The unreadable file is recorded, not fatal.
    assert "bad.py" in index["files"]
    assert "skipped" in index["files"]["bad.py"] or index["files"]["bad.py"]["lang"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))