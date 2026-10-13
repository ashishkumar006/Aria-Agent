"""A research node's output must not be lost to a stray brace.

`n_004` in the 60-page run returned output beginning
`{"question": "gradients and hydraulics in Roman water con...`, was rejected
by `parse_skill_json`, retried ZERO times, and took with it a whole research
node, the recovery re-run it triggered, and the duplicate document that
followed. The old fallback was a blind `find("{") .. rfind("}")` slice.
"""
import json

import pytest

import skills

# The real shape that broke it: a findings string containing a brace, plus a
# trailing comma - both common, neither invalid-looking to a human.
GOOD = {
    "question": "gradients and hydraulics in Roman water supply",
    "findings": "Flow relied on a gradient near {1:2000}. Roman builders "
                "measured fall across long runs.",
    "evidence": [{"claim": "gradients were near 1:2000",
                  "quote": "the water fell gently, perhaps {1} in 2000"}],
    "caveats": [],
}


def test_valid_researcher_json_round_trips():
    assert skills.parse_skill_json(json.dumps(GOOD))["question"] == \
        GOOD["question"]


def test_braces_inside_strings_do_not_break_extraction():
    """The old blind slice started/ended in the wrong place here."""
    t = ("Sure, here is the research:\n" + json.dumps(GOOD)
         + "\nHope that helps!")
    out = skills.parse_skill_json(t)
    assert out.get("question"), out
    assert "1:2000" in out["findings"]


def test_a_literal_newline_inside_a_string_is_repaired():
    """Models emit raw newlines in findings; that is invalid JSON but not a
    reason to discard the node."""
    broken = ('{"question": "q", "findings": "line one\nline two", '
              '"caveats": []}')
    out = skills.parse_skill_json(broken)
    assert out.get("question") == "q", out
    assert "line two" in out["findings"], out


def test_trailing_commas_are_repaired():
    t = '{"question": "q", "findings": "f", "caveats": [],}'
    assert skills.parse_skill_json(t).get("question") == "q"


def test_markdown_fences_still_stripped():
    t = "```json\n" + json.dumps(GOOD) + "\n```"
    assert skills.parse_skill_json(t).get("question")


def test_escaped_quotes_are_not_mistaken_for_the_end_of_a_string():
    t = json.dumps({"findings": 'he said "stop" and then {continued}'})
    assert skills.parse_skill_json(t)["findings"].endswith("{continued}")


def test_genuinely_unparseable_still_returns_empty():
    assert skills.parse_skill_json("not json at all") == {}
    assert skills.parse_skill_json("") == {}
    assert skills.parse_skill_json("{}") == {}


def test_valid_json_is_never_mutated():
    """The repair pass must not touch a payload that already parses."""
    t = json.dumps(GOOD)
    assert skills.parse_skill_json(t) == GOOD


def test_the_real_failure_shape_from_the_run():
    """Reproduces n_004's prefix: an object opening with `question`."""
    t = ('{"question": "gradients and hydraulics in Roman water '
         'conduction", "findings": "Pressure varied {shallow to deep}.", '
         '"sources": [], "evidence": [], "conflicts": [], "caveats": []}')
    out = skills.parse_skill_json(t)
    assert out.get("findings"), out
    assert "shallow to deep" in out["findings"]