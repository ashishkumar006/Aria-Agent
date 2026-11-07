"""A facet that produced nothing must not be able to succeed.

Companion to `test_fold_tool_results.py`: folding stops the researcher's prompt
from growing without bound, and this stops the failure mode that growth produced
from being invisible. Both came out of the same ledger query — researcher calls
at 245k-283k input tokens answering with 29-144 output tokens, all logged
`status = ok`.
"""
from __future__ import annotations

import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import pytest

import skills


@pytest.mark.parametrize("parsed", [
    {"findings": ""},
    {"findings": "   \n  "},
    {"findings": None},
    {"findings": []},
    {"findings": {}},
    {},
])
def test_an_empty_facet_is_a_failure_not_a_thin_result(parsed):
    err = skills.empty_facet_error("researcher", parsed, {})
    assert err, "an empty findings field passed as success"
    assert "replan" in err.lower(), (
        "the error exists to trigger recovery, so it has to say what to do")


def test_json_serialising_an_empty_finding_does_not_make_it_non_empty():
    """The trap this file caught in its own first draft.

    `json.dumps(finding or "")` reads like a reasonable normaliser and is wrong:
    `None`, `[]` and `{}` become `'\"\"'`, `'[]'`, `'{}'` — text with
    punctuation in it, which any `.strip()` check calls non-empty. So a facet
    that produced nothing passed as a facet that produced something, and the
    guard that exists to catch exactly that would have missed it.
    """
    assert skills._findings_text({}) == ""
    assert skills._findings_text({"findings": None}) == ""
    assert skills._findings_text({"findings": []}) == ""
    assert skills._findings_text({"findings": ["", "  "]}) == ""
    assert skills._findings_text({"findings": {"a": None, "b": []}}) == ""
    assert skills._findings_text(
        {"findings": [{"point": "gradient 1 in 4800"}]}) == "gradient 1 in 4800"


def test_a_structured_facet_with_its_own_field_names_counts():
    """`findings` is often a list of objects the model named itself.

    Recognising only `findings`/`answer`/`text` inside those objects would call
    `[{"claim": …, "evidence": …}]` empty — the same false positive, one level
    down, and the one that would fire on the most well-structured output.
    """
    parsed = {"findings": [{"claim": "lead pipes were sized by the quinaria",
                            "evidence": "Frontinus, De aquaeductu 24"}]}
    assert skills._findings_text(parsed) != ""
    assert skills.empty_facet_error("researcher", parsed, {}) == ""


def test_the_final_answer_envelope_is_a_valid_facet():
    """The false positive that reached a running pipeline.

    The researcher prompt asks for `{"final_answer": "..."}`, so a facet with no
    `findings` key at all is normal, valid output. Judging only `findings` failed
    every one of them: `test_fanout_three_researchers` and its bounded-fan-out
    sibling went red, and the live shape was a node failing, recovery replanning
    all three siblings, and the same error printed twenty-seven times before the
    cap cut it off. A guard that fires on valid output does not make the run
    safer, it converts a working run into a retry storm.
    """
    parsed = {"final_answer": "The Pont du Gard carries a gradient of ~1 in 3000."}
    assert skills.empty_facet_error("researcher", parsed, {}) == ""
    assert skills._findings_text(parsed) != ""


def test_unparseable_prose_from_the_model_is_not_called_an_empty_facet():
    """A model that wrote prose instead of JSON still said something. That is a
    format failure worth a different message; it is not the saturation sign."""
    reply = {"text": "I researched the question and found that the aqueduct..."}
    assert skills.empty_facet_error("researcher", {}, reply) == ""


def test_a_facet_that_found_nothing_still_says_something():
    """The distinction the whole guard rests on.

    "I searched six sources and the claim is not supported anywhere" is a real
    research result and must not be retried forever. A blank is not.
    """
    real = {"findings": "Searched six sources; none support the claim that "
                       "Roman aqueducts used steam pressure.",
            "sources": [{"url": "https://example.test/a"}]}
    assert skills.empty_facet_error("researcher", real, {}) == ""


def test_findings_as_a_structured_list_still_count():
    assert skills.empty_facet_error(
        "researcher", {"findings": [{"point": "gradient averaged 1 in 4800"}]},
        {}) == ""


def test_only_the_researcher_is_judged_this_way():
    """An author returning no blocks, or a formatter returning no text, is a
    different contract with different existing guards. Applying this check by
    name would fail nodes for the wrong reason."""
    assert skills.empty_facet_error("author", {}, {}) == ""
    assert skills.empty_facet_error("formatter", {}, {}) == ""
    assert skills.empty_facet_error("summariser", {}, {}) == ""


def test_a_transport_error_is_not_reported_as_an_empty_facet():
    """If the call already failed, saying "the researcher returned nothing"
    points at the model when the network is the culprit."""
    assert skills.empty_facet_error(
        "researcher", {}, {"error": "connection reset"}) == ""


def test_a_thin_facet_with_sources_is_noted_quietly():
    """Sources present means it read something. That is worth knowing about but
    not worth failing the node for."""
    parsed = {"findings": "Only one paragraph available.",
              "sources": [{"url": "https://x.test/1"}]}
    assert skills.thin_facet_notice("researcher", parsed, "q") == ""


def test_a_thin_facet_with_no_sources_is_named():
    """Not a failure — some subjects are genuinely thin — but the reason a
    document reads as hollow, so it has to be attributable to a facet."""
    notice = skills.thin_facet_notice(
        "researcher", {"findings": "brief", "sources": []},
        "Aqueducts in the provinces")
    assert notice and "near-empty" in notice
    assert "Aqueducts in the provinces" in notice
    assert "0 sources" in notice


def test_the_notice_has_a_floor_that_is_not_the_empty_check():
    """Two different lines: below the floor it is notable, above it it is not.
    If they collapsed into one threshold the guard would either fire constantly
    or never."""
    short = {"findings": "x" * 599, "sources": []}
    long = {"findings": "x" * 700, "sources": []}
    assert skills.thin_facet_notice("researcher", short, "q") != ""
    assert skills.thin_facet_notice("researcher", long, "q") == ""
    # Both are far from empty, so neither fails the node.
    assert skills.empty_facet_error("researcher", short, {}) == ""
    assert skills.empty_facet_error("researcher", long, {}) == ""


def test_the_guard_is_wired_into_the_node_path():
    """A predicate nothing calls is a test file, not a fix.

    Checked as a pair — the call, and a `success=False` result inside the block
    that follows it — because "the name appears in the function" would still
    pass if someone logged it and carried on.
    """
    import inspect
    src = inspect.getsource(skills.run_skill)
    at = src.index("empty_facet_error(")
    block = src[at:at + 400]
    assert "success=False" in block, (
        "the empty-facet check runs but does not fail the node")
    assert "return AgentResult" in block
    assert "thin_facet_notice(" in src, "the thin-facet notice is not wired"
