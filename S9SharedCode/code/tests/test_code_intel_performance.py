"""Reference finding must not walk the whole machine.

A measured `find_references` over the repo root took 389s: it descended into
`.venv`, `node_modules` and `dist` - 30,714 files - and parsed `.py` with `ast`
before checking whether a file even mentioned the symbol. One such request
stalled the agent for six minutes, which is why this is pinned.

The numbers are about ORDER, not cleverness:
  - prune vendored/build directories during the walk, not after
  - substring-test each file BEFORE parsing it
Neither changes an answer; both are required for the endpoint to be usable.
"""
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import code_intel as CI

VENDORED = {".venv", "node_modules", "dist", "__pycache__", ".git", "build"}


@pytest.fixture()
def big_tree(tmp_path):
    """A tree that mimics the shape that caused the stall."""
    src = "def helper(x):\n    return x\n"
    (tmp_path / "app.py").write_text(src, encoding="utf-8")
    (tmp_path / "other.py").write_text("from app import helper\n", encoding="utf-8")
    for d in VENDORED:
        sub = tmp_path / d
        sub.mkdir()
        # each vendored dir holds files that would be found without pruning
        for i in range(40):
            (sub / f"f{i}.py").write_text(
                "def helper(x):\n    return x\n", encoding="utf-8")
    return tmp_path


def test_vendored_directories_are_not_walked(big_tree):
    files = CI.iter_files(str(big_tree))
    offenders = [
        f for f in files
        if any(part in VENDORED for part in f.relative_to(big_tree).parts[:-1])
    ]
    assert offenders == [], (
        f"{len(offenders)} files came from vendored directories; they cannot "
        f"contain a definition the caller cares about and dominate the scan")


def test_a_tree_full_of_decoys_still_finds_the_real_reference(big_tree):
    """Pruning must not hide the answer - only the noise.

    The vendored copies all define a `helper` too, so an un-pruned scan
    reports 8+ definitions and the caller cannot tell which is real.
    """
    r = CI.find_references("helper", str(big_tree))
    assert r["ok"], r
    assert r["definition_count"] == 1, (
        f"expected only the real definition, got {r['definitions']}")
    assert r["definitions"][0]["file"] == "app.py"
    assert any(x["file"] == "other.py" for x in r["references"])


def test_references_stay_fast_on_a_decoy_heavy_tree(big_tree):
    """The regression that stalled the agent. Generous ceiling: this is about
    orders of magnitude, not microseconds."""
    t0 = time.perf_counter()
    CI.find_references("helper", str(big_tree))
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, (
        f"took {elapsed:.1f}s on {len(CI.iter_files(str(big_tree)))} files; "
        f"the un-pruned scan of this shape took minutes")


def test_files_parsed_is_reported_and_counts_only_relevant_files(big_tree):
    """A cheap substring test runs BEFORE the expensive parse, so the parse
    count is bounded by how many files actually mention the name.

    After pruning this tree has exactly two real source files and BOTH
    mention `helper`, so parsed == scanned is the correct answer here. What
    must not happen is the vendored copies being counted, which is what the
    pruning test above pins.
    """
    r = CI.find_references("helper", str(big_tree))
    real = {f.name for f in CI.iter_files(str(big_tree))}
    assert r["files_scanned"] == len(real), (r["files_scanned"], len(real))
    assert r["files_parsed"] <= r["files_scanned"]
    # 2 real files, both relevant -> at most 2 parses, and never 8+ which is
    # what an un-pruned scan would report.
    assert r["files_parsed"] <= 2, r
