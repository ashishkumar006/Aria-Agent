"""The length contract and the delivered-size feedback signal.

Both are prompt-level, which is the whole point: "length follows the request"
with no number resolves toward brevity every time, and a model that cannot see
how much it produced cannot tell a 2,000-word report from a 200-word stub.
"""
import mcp_server
import pytest
import skills


@pytest.mark.parametrize("query,skill,lo,hi", [
    # An explicit number always wins over the tier heuristics.
    ("about 2000 words on X", "author", 2000, 2000),
    ("5 page report on X", "author", 2000, 2600),
    # Depth words are FLOORS now, not targets. They used to be 8000/30000.
    ("an extensive deep-dive", "author", 3000, 3000),
    ("a thorough comprehensive study", "author", 6000, 6000),
    ("a detailed analysis of X", "author", 3000, 3000),
])
def test_length_target_is_quantified_and_not_contradictory(query, skill, lo, hi):
    target, label = skills.length_target(query, skill)
    assert lo <= target <= hi, f"{query!r} -> {target} ({label})"


@pytest.mark.parametrize("query", [
    "A briefing note on NHS ambulance handover delays",
    "A board note on why onboarding takes 9 days",
    "Analyse the trade-offs in our vendor contract",
])
def test_no_length_is_imposed_when_nothing_was_asked_for(query):
    """The 700-word default is gone.

    It was the reason every document came out the same size regardless of
    subject or ambition - including briefs that said "comprehensive". With the
    request silent, nothing is invented; the agent decides.
    """
    floor, label = skills.length_target(query, "author")
    assert floor == 0, f"{query!r} was given a {floor}-word floor"
    assert skills.ceiling_for(query, "author") == 0
    block = skills.length_contract(query, "author")
    assert "No length has been imposed" in block, block


@pytest.mark.parametrize("query,ceiling", [
    ("A one-page summary of the policy", 450),
    ("Give me a TL;DR of this report", 120),
    ("A single paragraph on the outage", 120),
    ("Keep it short: the chair wants the headline", 700),
])
def test_an_explicit_short_request_still_binds(query, ceiling):
    """Removing our invented caps must not remove the USER's instruction.

    Without this, "a one-page summary" - a limit the user set - came back as
    twenty pages, because the only cap left was the one we had deleted.
    """
    assert skills.ceiling_for(query, "author") == ceiling
    block = skills.length_contract(query, "author")
    assert f"{ceiling:,}" in block, block


def test_impossible_word_counts_are_capped_and_said_so():
    """A rendering limit, not a content one: it exists so a 500-page request
    gets an honest error naming the achievable maximum instead of a stub."""
    target, label = skills.length_target("write 500 pages of prose", "author")
    assert target <= skills._MAX_WORDS_PER_RENDER
    assert "page" in label
    # ...and a 500-page ask is still honoured up to that ceiling, not clipped
    # to the old 12,000.
    assert target > 12000, "the old arbitrary 12,000 cap is back"


def test_deck_targets_count_slides_not_pages():
    assert skills.length_target("15 slide deck", "deck")[0] == 525
    # ~1 slide a minute: a 20 minute talk is not 40 slides.
    n, label = skills.length_target("20 minute talk", "deck")
    assert n == 22 * 35 and "22-slide" in label
    # Clamped to something presentable.
    assert skills.length_target("200 slide deck", "deck")[0] == 40 * 35


def test_length_contract_states_the_number_and_a_floor():
    block = skills.length_contract("write a 12 page report", "author")
    assert "LENGTH CONTRACT" in block
    # The achievable number is stated, and so is the failure threshold.
    assert "5,400" in block, block
    assert "4,050" in block, block
    # A deck is measured in slide text, not prose - but "a deck" with no slide
    # count carries no number at all now, so ask for one to get the unit.
    assert "words of slide text" in skills.length_contract("a 10 slide deck", "deck")


def test_doc_stats_reports_what_was_actually_delivered():
    spec = {"blocks": [
        {"type": "heading", "level": 1, "text": "Alpha"},
        {"type": "paragraph", "text": "one two three four five"},
        {"type": "bullet", "items": ["a b", "c d"]},
    ]}
    st = mcp_server._doc_stats(spec, "pdf")
    assert st["format"] == "pdf"
    assert st["words"] >= 10
    assert st["sections"] == 1


def test_doc_stats_survives_garbage_without_raising():
    # A malformed spec must not crash the tool call: the stats are a
    # convenience, not a gate.
    for spec in ({}, {"blocks": None}, {"blocks": [{}]},
                 {"blocks": [{"type": "paragraph", "text": None}]},
                 {"blocks": "nonsense"}):
        st = mcp_server._doc_stats(spec, "docx")
        assert st["words"] >= 0