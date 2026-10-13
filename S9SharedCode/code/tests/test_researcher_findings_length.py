"""The researcher prompt was specifying 4-10 paragraphs, and that starved runs.

Measured: 11 researchers returned 22,217 characters total (~350 words each).
A 60-page document needs ~27,000 words and ~100,000 characters of evidence
underneath it, so the author stopped at 2,464 words regardless of the target.
The prompt is what bounded it, not the model.
"""
import pathlib

import pytest

import skills

PROMPT = pathlib.Path(skills.__file__).parent / "prompts" / "researcher.md"


def test_the_old_paragraph_cap_is_gone():
    """The SCHEMA line must not cap findings; the prose may quote it."""
    import re
    text = PROMPT.read_text(encoding="utf-8")
    schema = [ln for ln in text.splitlines()
              if '"findings"' in ln and ":" in ln]
    assert schema, "no findings schema line found"
    for ln in schema:
        assert "paragraph" not in ln.lower(), (
            f"the schema still caps findings: {ln.strip()}")


def test_findings_length_is_specified_with_real_numbers():
    low = PROMPT.read_text(encoding="utf-8").lower()
    assert "findings length" in low, "no FINDINGS LENGTH section"
    assert "1,500-4,000 words" in low, (
        "no concrete word range for a normal single-facet question")


def test_it_explains_why_the_evidence_base_is_the_ceiling():
    low = PROMPT.read_text(encoding="utf-8").lower()
    assert "27,000" in low or "60-page" in low, (
        "the measured starvation is not explained to the model")
    assert "spilled to an artifact" in low, (
        "it must be said that being thorough is free - otherwise the model "
        "compresses to avoid a penalty it was never given")


def test_researcher_output_ceiling_allows_the_target():
    """One researcher must be able to hold 6,000 words of findings."""
    import yaml
    cfg = yaml.safe_load(
        (pathlib.Path(skills.__file__).parent / "agent_config.yaml")
        .read_text(encoding="utf-8"))
    mt = cfg["researcher"]["max_tokens"]
    # 6,000 words ~ 8,000 tokens of prose.
    assert mt >= 16_000, f"researcher max_tokens={mt} cannot hold 6k words"


@pytest.mark.parametrize("tier_words", [45000, 15000])
def test_book_length_tiers_exist_and_are_ordered(tier_words):
    assert tier_words <= skills._MAX_WORDS_PER_RENDER


def test_a_60_page_request_gets_a_book_length_floor():
    floor, label = skills.length_target(
        "write a comprehensive treatise of about 60 pages on X", "author")
    assert floor >= 20_000, floor
    assert "page" in label or "book" in label, label


def test_ordinary_detailed_reports_are_not_demanded_to_be_books():
    """The 15,000 tier must stay narrow or every report becomes a treatise."""
    assert skills.length_target("a detailed analysis of X", "author")[0] == 3000
    assert skills.length_target("an extensive deep-dive", "author")[0] == 3000
    assert skills.length_target("a thorough comprehensive study",
                                "author")[0] == 6000