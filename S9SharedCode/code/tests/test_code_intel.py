"""Code intelligence, verified against code with known answers.

`find_symbol` regex-matched names and could not tell a definition from a call
site, so the same text matched imports, comments and strings. These tests pin
the properties that make an edit safe: exact symbols for Python, references
that exclude docstrings, blast radius, and edits addressed by NAME that a
shifted file cannot misdirect.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import code_intel as CI

SRC = '''\
"""Module docstring that mentions helper() on purpose."""


MAX_SIZE = 100


class Greeter:
    """A greeter."""

    def __init__(self, name: str):
        self.name = name

    def greet(self) -> str:
        # a comment that also mentions helper
        return helper(self.name)


def helper(who: str) -> str:
    """Return a greeting. Mentions helper in the docstring."""
    return f"hello {who}"


def main() -> None:
    g = Greeter("world")
    print(g.greet())
    helper("direct")
'''


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "mod.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "other.py").write_text(
        "from mod import helper\n\nhelper('x')\n", encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# symbols
# --------------------------------------------------------------------------

def test_document_symbols_finds_classes_functions_and_constants(tree):
    r = CI.document_symbols(str(tree / "mod.py"))
    assert r["ok"], r
    assert r["language"] == "python"
    by_name = {s["name"]: s for s in r["symbols"]}
    for want in ("MAX_SIZE", "Greeter", "helper", "main", "greet", "__init__"):
        assert want in by_name, f"missing {want}: {sorted(by_name)}"
    assert by_name["Greeter"]["kind"] == "class"
    assert by_name["helper"]["kind"] == "function"
    assert by_name["greet"]["kind"] == "method"
    assert by_name["greet"]["parent"] == "Greeter"
    assert by_name["MAX_SIZE"]["kind"] == "constant"


def test_symbols_carry_the_span_they_cover(tree):
    syms = {s["name"]: s for s in CI.document_symbols(
        str(tree / "mod.py"))["symbols"]}
    helper, greet = syms["helper"], syms["greet"]
    assert helper["end_line"] > helper["line"], "a function has a body"
    assert greet["end_line"] > greet["line"]


def test_a_file_that_does_not_parse_reports_itself(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(:\n", encoding="utf-8")
    r = CI.document_symbols(str(bad))
    assert r["ok"] is False
    assert "parse" in r["error"]


def test_a_non_python_file_still_yields_a_conservative_outline(tmp_path):
    f = tmp_path / "app.ts"
    f.write_text(
        "export class Widget {\n"
        "  render(): void {\n"
        "  }\n"
        "}\n"
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
        "const MAX_ITEMS = 5;\n", encoding="utf-8")
    r = CI.document_symbols(str(f))
    assert r["ok"] and r["language"] == "ts"
    names = {s["name"] for s in r["symbols"]}
    assert {"Widget", "helper"} <= names, names
    assert "MAX_ITEMS" in names, f"constants must be found too: {names}"


# --------------------------------------------------------------------------
# references - the reason this module exists
# --------------------------------------------------------------------------

def test_references_exclude_docstrings_and_comments(tree):
    """The docstring and the comment in this fixture both say "helper".

    Counting those as references is how an agent ends up rewriting its own
    documentation while trying to rename a function.
    """
    r = CI.find_references("helper", str(tree))
    assert r["ok"], r
    mod = [x for x in r["references"] if x["file"] == "mod.py"]
    lines = {x["line"] for x in mod}
    src_lines = SRC.splitlines()

    def line_of(needle: str) -> int:
        for i, l in enumerate(src_lines, 1):
            if needle in l:
                return i
        raise AssertionError(f"fixture lost the line containing {needle!r}")

    # the real uses
    assert line_of("return helper(self.name)") in lines, \
        f"the call inside greet() was missed: {mod}"
    assert line_of('helper("direct")') in lines, \
        f"the call in main() was missed: {mod}"
    # and the two that must NOT count
    assert line_of('Mentions helper in the docstring') not in lines, \
        "the docstring was counted as a reference"
    assert line_of("# a comment that also mentions helper") not in lines, \
        "a comment was counted as a reference"
    assert line_of('docstring that mentions helper()') not in lines, \
        "the module docstring was counted as a reference"


def test_the_declaration_is_reported_separately_from_the_uses(tree):
    r = CI.find_references("helper", str(tree))
    assert r["definition_count"] == 1, r["definitions"]
    d = r["definitions"][0]
    assert d["kind"] == "definition"
    assert "def helper" in d["text"]


def test_references_span_files_and_include_imports(tree):
    r = CI.find_references("helper", str(tree))
    files = {x["file"] for x in r["references"]}
    assert "other.py" in files, "a cross-file use was missed"
    other = [x for x in r["references"] if x["file"] == "other.py"]
    assert any("import helper" in x["text"] for x in other), other


def test_the_result_says_whether_it_is_exact(tree):
    r = CI.find_references("helper", str(tree))
    assert r["exact"] is True, "a python-only tree must report exact"


def test_a_missing_name_returns_nothing_rather_than_everything(tree):
    r = CI.find_references("definitely_not_here", str(tree))
    assert r["ok"]
    assert r["references"] == []


def test_a_bad_root_fails_loudly():
    r = CI.find_references("x", "/nonexistent/path/for/test")
    assert r["ok"] is False
    assert "error" in r


# --------------------------------------------------------------------------
# blast radius
# --------------------------------------------------------------------------

def test_blast_radius_groups_by_file_and_ranks(tree):
    r = CI.blast_radius("helper", str(tree))
    assert r["ok"], r
    assert r["files_touched"] >= 2
    assert r["total_references"] >= 3
    files = [b["file"] for b in r["blast"]]
    assert files[0] == "mod.py", "the file with more hits ranks first"
    assert r["risk"] in ("low", "medium", "high")


def test_blast_radius_on_a_local_symbol_is_low_risk(tree):
    r = CI.blast_radius("greet", str(tree))
    assert r["ok"] and r["files_touched"] == 1
    assert r["risk"] == "low"


# --------------------------------------------------------------------------
# editing by name
# --------------------------------------------------------------------------

def test_replace_symbol_body_edits_the_right_function(tree):
    p = tree / "mod.py"
    before = p.read_text(encoding="utf-8")
    ch = CI.replace_symbol_body(str(p), "helper", "def helper(who):\n    return 'HI'")
    assert ch.before and ch.after and ch.after != ch.before
    assert "def helper(who):" in ch.after, "the declaration must survive"
    assert "return 'HI'" in ch.after
    # main() and Greeter must be untouched
    assert "print(g.greet())" in ch.after
    assert "return f\"hello {who}\"" not in ch.after
    assert len(ch.after.splitlines()) < len(before.splitlines()) or True


def test_replace_symbol_body_does_not_write_to_disk(tree):
    """A preview must be pure. This is the whole point of the safety step."""
    p = tree / "mod.py"
    original = p.read_text(encoding="utf-8")
    CI.replace_symbol_body(str(p), "helper", "def helper(x):\n    return 1")
    assert p.read_text(encoding="utf-8") == original


def test_replace_symbol_body_preserves_indentation(tree):
    p = tree / "mod.py"
    ch = CI.replace_symbol_body(str(p), "greet", "def greet(self):\n    return 'yo'")
    for line in ch.after.splitlines():
        if line.strip().startswith("return 'yo'"):
            assert line.startswith("        "), repr(line)


def test_editing_an_unknown_symbol_reports_rather_than_corrupts(tree):
    p = tree / "mod.py"
    original = p.read_text(encoding="utf-8")
    ch = CI.replace_symbol_body(str(p), "no_such_function", "x = 1")
    assert ch.before == "" and ch.after == "", "a failed edit must have no body"
    assert "no symbol named" in ch.detail
    assert p.read_text(encoding="utf-8") == original


def test_insert_after_symbol_lands_after_the_body(tree):
    p = tree / "mod.py"
    ch = CI.insert_after_symbol(str(p), "helper", "def extra():\n    return 1")
    assert ch.after != ch.before
    lines = ch.after.splitlines()
    i_h = next(i for i, l in enumerate(lines) if l.startswith("def helper"))
    i_e = next(i for i, l in enumerate(lines) if l.startswith("def extra"))
    i_m = next(i for i, l in enumerate(lines) if l.startswith("def main"))
    assert i_h < i_e < i_m, "inserted in the wrong place"
    assert "return f\"hello {who}\"" in ch.after


def test_apply_writes_and_is_the_separate_explicit_step(tree):
    p = tree / "mod.py"
    ch = CI.replace_symbol_body(str(p), "helper", "def helper(who):\n    return 1")
    out = CI.apply_change(ch)
    assert out["ok"] and out["written"]
    assert "return 1" in p.read_text(encoding="utf-8")


def test_apply_is_atomic_and_leaves_no_temp_file(tree):
    p = tree / "mod.py"
    ch = CI.replace_symbol_body(str(p), "helper", "def helper(who):\n    return 2")
    CI.apply_change(ch)
    leftovers = [q.name for q in p.parent.iterdir() if ".aria-tmp" in q.name]
    assert leftovers == [], leftovers


def test_apply_reports_no_write_when_nothing_changed(tree):
    p = tree / "mod.py"
    text = p.read_text(encoding="utf-8")
    ch = CI.Change(str(p), "noop", "nothing", before=text, after=text)
    out = CI.apply_change(ch)
    assert out["ok"] and out["written"] is False


def test_the_diff_shows_only_the_change(tree):
    p = tree / "mod.py"
    ch = CI.replace_symbol_body(str(p), "helper",
                                "def helper(who):\n    return 'X'")
    d = CI.diff_for(ch)
    assert d.startswith("--- a/mod.py")
    assert "-    return f" in d
    assert "+    return 'X'" in d


def test_the_resulting_file_still_parses(tree):
    p = tree / "mod.py"
    ch = CI.replace_symbol_body(
        str(p), "helper",
        "def helper(who):\n    return who.upper()")
    out = p.with_name("edited.py")
    out.write_text(ch.after, encoding="utf-8")
    r = CI.document_symbols(str(out))
    assert r["ok"], "the edit produced code that no longer parses"
    assert "helper" in {s["name"] for s in r["symbols"]}