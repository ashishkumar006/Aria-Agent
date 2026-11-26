"""The two authoring prompts describe the machine that actually runs.

Found by cross-reading planner.md / researcher.md against mcp_runner.py, flow.py
and the gateway's docgen.py, each of these was a promise the code could not keep.

  1. depth was a no-op with a degenerate definition: deep and normal were the
     same commitment, nothing read the field, and the only fan-out lever
     (facet count) was pinned at 6-10 with no path below it.
  2. the fleet arithmetic did not close: 6 facets x 4,000 words = 24,000 for a
     document whose floor is 27,000 words.
  3. researcher.md demanded what the loop cannot supply: "write up every page"
     with no fetch-batching instruction, against a 6-turn budget and ~1 tool
     call per turn.
  4. the two prompts assumed different things about each other.
  5. author.md taught a block type the PDF renderer does not have.
"""
import pathlib
import re

import pytest

skills = pytest.importorskip("skills")
ROOT = pathlib.Path(skills.ROOT)
PL = (ROOT / "prompts" / "planner.md").read_text(encoding="utf-8")
RS = (ROOT / "prompts" / "researcher.md").read_text(encoding="utf-8")
AU = (ROOT / "prompts" / "author.md").read_text(encoding="utf-8")

# The block types docgen.py actually accepts (BLOCK_SCHEMA plus aliases).
ALLOWED = {"cover", "heading", "paragraph", "bullets", "numbers", "quote",
           "chart", "image", "table", "stats", "columns", "callout", "note",
           "pagebreak"}
PDF_ALLOWED = ALLOWED - {"note"}  # note is pptx speaker-notes only


# The lean planner no longer defines quick/normal/deep commitments or sizes the
# research fleet — that strategy was removed on purpose (capability + output
# contract only). The researcher/author invariants below still hold.


# Facet sizing / the worked example were removed with the rest of the planner's
# prescriptive strategy; the planner now decides the fleet from the request.


def test_researcher_tells_the_model_to_batch_its_fetches():
    """~1 tool call per turn against a 6-turn budget is the material gap."""
    low = RS.lower()
    assert "same turn" in low or "one turn" in low, (
        "no fetch-batching instruction: the model emits one tool call per "
        "turn and the budget is spent on two pages")
    assert "never fetch one url per turn" in low or "one per turn" in low, (
        "the sequential-fetch anti-pattern is not named")


def test_researcher_states_the_turn_budget_honestly():
    """The prompt must not describe a machine the loop does not have."""
    assert re.search(r"\b6\b|six", RS.lower()), (
        "the tool-turn cap is not stated, so 'write up every page' is "
        "unfalsifiable when only 2.8 pages arrive")
    assert "8,000" not in RS and "8_000" not in RS, (
        "the prompt still claims the old 8k per-page clip")
    assert "complete" in RS.lower() or "whole" in RS.lower(), (
        "the prompt does not say fetch_url returns the whole page")
    assert "300,000" in RS, (
        "the real runaway guard is not stated, so a TRUNCATED body would be "
        "treated as the whole page")
    assert "TRUNCATED" in RS, (
        "the page-body truncation marker is not explained")


def test_researcher_disables_the_tool_result_clip():
    """The yaml, not a code default, is what lets a researcher read pages
    whole. 32k in mcp_runner used to cut long articles mid-sentence."""
    skill = skills.SkillRegistry().get("researcher")
    assert skill.max_tool_result_chars == 0


def test_the_word_floor_is_falsifiable():
    """A floor with no check cannot be failed."""
    assert re.search(r"1,200|1,500", RS), "no word floor"
    assert "count the words" in RS.lower(), (
        "no self-check: the model emits without comparing to the floor")
    assert "200 words" in RS, (
        "the floor is not tied to pages fetched, so a digest satisfies it")


def test_author_teaches_only_block_types_the_pdf_renderer_has():
    taught = set(re.findall(r'"type"\s*:\s*"([a-z_]+)"', AU))
    dead = taught - PDF_ALLOWED
    assert not dead, (
        f"author.md teaches block types the PDF renderer does not have, and "
        f"they are silently dropped: {sorted(dead)}")
    assert taught, "no block types taught at all"


# The facet-shape and worked-example checks went with the worked example itself;
# researcher.md no longer needs a workaround for a planner rule that is gone.