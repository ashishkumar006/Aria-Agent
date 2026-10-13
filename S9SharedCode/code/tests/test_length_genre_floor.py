"""The genre gap and the clause that told the author to stop.

Two failures, both measured against real runs:

* A request that named a genre ("write a report on X") matched no tier, so
  floor=0 and the author was told no length was imposed. Same intent worded
  as "a detailed analysis" got floor=3000 and delivered 2,367 words; the
  report wording delivered 310.
* The floor=0 block ended "and no more", which reads as an instruction to
  stop early rather than as freedom from a target.
"""
import pytest

import skills


@pytest.mark.parametrize("query", [
    "Create a document.\n\nfind out about rag and create a neat pdf explaining it",
    "Create a document.\n\nWrite a report on the rise and fall of Roman aqueducts.",
    "A market analysis comparing solid-state and lithium-ion supply chains",
    "Write an academic-style report on Roman aqueducts for a US audience",
    "Create a document.\n\nproduce a pdf on the history of the Silk Road",
    "research vector databases and write a paper about them",
    "make a study of urban water infrastructure",
])
def test_a_named_genre_gets_a_floor(query):
    floor, label = skills.length_target(query, "author")
    assert floor >= 1000, f"{query!r} -> {floor} ({label})"


@pytest.mark.parametrize("query", [
    "A one-line note about aqueduct gradients.",
    "A single paragraph on the outage",
    "A briefing note on NHS ambulance handover delays",
    "An overview of the incident",
])
def test_short_and_brief_requests_still_get_no_floor(query):
    """The genre tier must not swallow the explicit-short tiers."""
    assert skills.length_target(query, "author")[0] == 0, query


def test_depth_words_still_outrank_the_genre_tier():
    """'detailed analysis' must keep its 3000 floor, not drop to 1200."""
    floor, _ = skills.length_target("a detailed analysis about the history of India",
                                   "author")
    assert floor == 3000, floor


def test_explicit_numbers_still_win():
    assert skills.length_target("about 2000 words on X", "author")[0] == 2000
    assert skills.length_target("5 page report on X", "author")[0] == 2250


def test_the_floor_zero_block_no_longer_says_stop():
    """'and no more' is what produced the stubs."""
    block = skills.length_contract("A briefing note on NHS handover delays",
                                   "author")
    assert "No length has been imposed" in block
    assert "and no more" not in block, block
    # The replacement still says to develop the material.
    assert "develop every point" in block, block


def test_no_prompt_anywhere_tells_the_model_to_stop_early():
    """A stale 'and no more' in any prompt would re-arm the same failure."""
    import pathlib
    bad = []
    for p in pathlib.Path("prompts").glob("*.md"):
        txt = p.read_text(encoding="utf-8")
        for lineno, line in enumerate(txt.splitlines(), 1):
            low = line.lower()
            if "and no more" in low or "no more." in low:
                bad.append(f"{p.name}:{lineno}: {line.strip()[:70]}")
    assert not bad, "\n".join(bad)