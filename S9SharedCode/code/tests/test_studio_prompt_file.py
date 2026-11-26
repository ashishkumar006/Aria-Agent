"""The studio coding prompt is a file, not a string in a function.

Moving it out is only useful if three things stay true: the file renders with no
placeholder left behind, a missing file degrades instead of killing a run, and an
edit to the file takes effect without a restart.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_server as A  # noqa: E402

PROMPT_PATH = Path(A.__file__).with_name("prompts") / "studio_coder.md"

# The tokens the renderer knows how to fill. A new one in the file that is not
# here is a placeholder the model will read as literal text.
KNOWN_TOKENS = {"LAYOUT", "SEARCH_EXAMPLE", "APPROVAL_MODE", "MODE_BEHAVIOUR",
                "BROWSER_RULE", "HEAD", "WORKSPACE_BRIEF"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    A._STUDIO_CODER_CACHE["mtime"] = -1.0
    A._STUDIO_CODER_CACHE["text"] = ""
    yield
    A._STUDIO_CODER_CACHE["mtime"] = -1.0
    A._STUDIO_CODER_CACHE["text"] = ""


def test_the_prompt_file_exists_and_is_prose():
    assert PROMPT_PATH.exists(), f"{PROMPT_PATH} is missing"
    text = PROMPT_PATH.read_text(encoding="utf-8")
    assert len(text) > 2000, "the prompt shrank to a stub"
    # The sections the run depends on, in order.
    for head in ("== 0. PLAN ==", "== 1. INSPECT", "== 2. EDIT",
                 "== 3. EXECUTE", "== 4. WORKING"):
        assert head in text, head


def test_every_placeholder_is_filled():
    """A literal `{{TOKEN}}` reaching the model reads as a template bug."""
    rendered = A.render_studio_coder_prompt(
        LAYOUT="LAYOUT", SEARCH_EXAMPLE="search_files('x', '.')",
        APPROVAL_MODE="ASK", MODE_BEHAVIOUR="behaviour",
        BROWSER_RULE="- browser\n", HEAD="HEAD", WORKSPACE_BRIEF="BRIEF")
    assert "{{" not in rendered and "}}" not in rendered
    for part in ("LAYOUT", "search_files('x', '.')", "ASK", "behaviour",
                 "- browser", "HEAD", "BRIEF"):
        assert part in rendered


def test_the_file_only_uses_tokens_the_renderer_knows():
    """A placeholder added to the file without a caller would be silently
    dropped by the renderer, so the file and the renderer are checked against
    each other here rather than trusted."""
    import re

    used = set(re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}",
                          PROMPT_PATH.read_text(encoding="utf-8")))
    assert used <= KNOWN_TOKENS, sorted(used - KNOWN_TOKENS)
    assert used, "the file uses no placeholders — did the format change?"


def test_optional_blocks_default_to_nothing():
    """No browser rule when the flag is off."""
    rendered = A.render_studio_coder_prompt()
    assert "YOU HAVE A BROWSER" not in rendered
    # The git rules are in the prompt's EXECUTE section, not a per-request block.
    assert "git_action" in rendered


def test_an_edit_to_the_file_applies_without_a_restart(tmp_path, monkeypatch):
    fake = tmp_path / "studio_coder.md"
    fake.write_text("FIRST VERSION {{LAYOUT}}", encoding="utf-8")
    monkeypatch.setattr(A, "_STUDIO_CODER_PROMPT_PATH", fake)
    A._STUDIO_CODER_CACHE["mtime"] = -1.0

    first = A.render_studio_coder_prompt(LAYOUT="X")
    assert "FIRST VERSION" in first

    time.sleep(0.01)
    fake.write_text("SECOND VERSION {{LAYOUT}}", encoding="utf-8")
    second = A.render_studio_coder_prompt(LAYOUT="X")
    assert "SECOND VERSION" in second, "the mtime cache is not noticing edits"


def test_a_missing_prompt_file_falls_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "_STUDIO_CODER_PROMPT_PATH", tmp_path / "nope.md")
    A._STUDIO_CODER_CACHE["mtime"] = -1.0
    rendered = A.render_studio_coder_prompt(LAYOUT="X")
    assert "coding agent" in rendered
    assert "X" in rendered
    assert "{{" not in rendered


def test_an_empty_prompt_file_falls_back(tmp_path, monkeypatch):
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    monkeypatch.setattr(A, "_STUDIO_CODER_PROMPT_PATH", empty)
    A._STUDIO_CODER_CACHE["mtime"] = -1.0
    assert "coding agent" in A.render_studio_coder_prompt()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))