"""Authoring prompt truth — the prompts the pipeline sends, checked against the
plumbing that sends them.

This is the same idea as `test_studio_prompt_truth.py`, aimed at the other half
of the product, and it exists because of what the audit found rather than as an
exercise. Every one of these was a live defect:

- planner.md sized the whole research fleet on "the 1,500-4,000-word floor the
  Researcher prompt sets". No prompt and no cap ever set that range. The
  measured median was ~1,000 words, so fleets were planned against material
  that does not arrive.
- planner.md told the model its `source_hints` "steer tool choice". They were
  only ever STORED on the run record for the console to display, and reached no
  worker at all.
- researcher.md named 9 of the 13 tools it is actually granted, so the four
  specialised search verbs were paid for in schema and invisible to the model.
- author.md advertised `read_artifact` for spilled findings while the code
  deliberately never spills for the author (and the recorded tool uses show
  `read_artifact` called zero times in 1,910 author/deck calls).
- the worker-side effort cap keyed on the literal string "create a document",
  while the orchestrator's own classifier accepted four other shapes — so
  "produce a pdf about X" ran the full evidence fleet for a three-page brief.
- the planner was promised "no ceiling you are being trimmed to" next to a real
  400-node stop, 8-wide waves and a 160-section writing ceiling.

The rule these encode, in the user's words: the planner knows SKILLS and should
know nothing else. Tool names belong to the worker that owns the tools.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import skills                     # noqa: E402
import flow                       # noqa: E402

PROMPTS = CODE / "prompts"

# The planner's own output vocabulary. These four strings are VALUES of the
# `metadata.action` field it must emit for a `computer` node, not tools it can
# call — which is why the no-tools rule below carves them out instead of being
# bent. planner.md says the same thing in prose.
GRAPH_VOCAB = frozenset({"run_command", "read_file", "write_file", "open_app",
                         "drive_app"})

# Names a worker prompt may mention that are not in ITS OWN grant, each because
# the text is about another skill's gated host rather than a tool this node can
# reach. Anything not listed here fails the test on purpose.
MENTIONS_OF_OTHERS = {
    "researcher": GRAPH_VOCAB,   # inside computer_action's action list
    # formatter.md's A2UI `Action` component names these as the event verbs a
    # surface may emit — output vocabulary the formatter writes into JSON, not
    # tools the formatter itself can call.
    "formatter": GRAPH_VOCAB,
}


def _tool_names() -> set[str]:
    return set(skills._TOOL_CATALOG)


def _read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _registry():
    return skills.SkillRegistry()


def _mentions(text: str, names: set[str]) -> set[str]:
    return {n for n in names if re.search(r"\b" + re.escape(n) + r"\b", text)}


# ── the planner: skills only ────────────────────────────────────────────────
def test_planner_never_names_a_tool():
    """The rule the user stated, made mechanical.

    A goal containing a tool name is a coin-flip the planner has no business
    flipping: the worker may have it, may have had it removed, may have it
    withheld by a switch. Say what must be true; the skill that owns the tools
    decides how."""
    hits = _mentions(_read("planner.md"), _tool_names()) - GRAPH_VOCAB
    assert not hits, (
        f"planner.md names tools the planner cannot see or dispatch: "
        f"{sorted(hits)}. Describe the outcome instead.")


def test_planner_skill_table_covers_every_skill_the_registry_can_run():
    """A skill added to agent_config.yaml that planner.md never mentions is a
    capability nobody can use — `vision_file` and `app_builder` were exactly
    that: executable nodes the registry happily runs, reachable only if the
    planner happens to guess the name."""
    reg = _registry()
    missing = sorted(n for n in reg.names() if n != "planner"
                     and not re.search(r"`" + re.escape(n) + r"`",
                                       _read("planner.md")))
    assert not missing, f"planner.md has no row for: {missing}"


def test_planner_arithmetic_uses_the_measured_figure_not_an_invented_one():
    text = _read("planner.md")
    ev = f"{skills.RESEARCHER_EVIDENCE_WORDS:,}"
    assert ev in text, f"planner.md should size the fleet on {ev} words/researcher"
    assert str(skills.WORDS_PER_PAGE) in text, \
        "the pages->words figure must be the shared WORDS_PER_PAGE"
    # The false attribution, not the post-mortem that names it. The file is
    # allowed to say what used to be wrong; it is not allowed to size a fleet
    # on a floor no prompt or cap sets.
    assert "floor the Researcher prompt sets" not in text.replace("\n", " "), \
        "planner.md is justifying a number by a prompt that does not contain it"
    assert "18 facets" not in text, "the worked example must follow the measured figure"


def test_planner_states_the_real_limits():
    """Not "no ceiling": the run stops scheduling at MAX_NODES, waves are
    MAX_CONCURRENCY wide, and one document is written at up to 160 sections."""
    text = _read("planner.md")
    assert str(flow.MAX_NODES) in text, "the node stop is a planning fact"
    assert str(flow.MAX_CONCURRENCY) in text, "wave width is a planning fact"
    assert "160 section" in text, "the section ceiling is a planning fact"


# ── the workers: name what you are given ────────────────────────────────────
@pytest.mark.parametrize("persona", ["researcher", "author", "deck",
                                     "retriever", "distiller", "critic",
                                     "formatter", "summariser"])
def test_a_prompt_may_only_name_tools_that_persona_has(persona):
    reg = _registry()
    skill = reg.get(persona)
    granted = set(skill.tools_allowed or [])
    found = _mentions(_read(f"{persona}.md"), _tool_names()) - granted
    found -= MENTIONS_OF_OTHERS.get(persona, set())
    assert not found, (
        f"{persona}.md tells the model to use {sorted(found)}, which is not in "
        f"that skill's tools_allowed {sorted(granted)}. Either grant it or "
        f"stop promising it — a prompt that names a withheld tool produces an "
        f"apology or an invented result.")


def test_researcher_is_told_about_every_tool_it_is_granted():
    """The mirror image of the test above, because under-naming is the defect
    that actually shipped: 13 granted, 9 described, and the four invisible ones
    are the specialised searches the planner's hints are about."""
    granted = set(_registry().get("researcher").tools_allowed or [])
    assert granted, "researcher must have tools"
    silent = sorted(granted - _mentions(_read("researcher.md"), granted))
    assert not silent, f"researcher.md never mentions: {silent}"


def test_author_is_not_promised_a_spill_it_never_gets():
    """`_inputs_block` exempts the author from spilling on purpose, and the
    recorded tool uses show why. A prompt that says "open the handle" for a
    handle that never arrives sends the writer hunting."""
    text = _read("author.md")
    assert "never\nto need it" in text or "never to need it" in text.replace("\n", " "), \
        "author.md must not present read_artifact as the normal path"


# ── the plumbing the prompts now depend on ──────────────────────────────────
def test_one_intent_predicate_for_both_sides_of_the_file_question():
    """The cap and the orchestrator used to keep separate copies, and the
    worker-side one keyed on a literal prefix."""
    assert flow._DOCUMENT_INTENT_RE is skills.DOCUMENT_INTENT_RE
    for q in ("produce a pdf about roman aqueducts",
              "make a deck of solar trends",
              "create a document about siphons, 3 pages"):
        assert skills._research_effort_note(q), f"{q!r} is a file request"
    assert skills._research_effort_note("what is a siphon") == "", \
        "a question is not a brief and must not be capped"


def test_source_hints_reach_the_node_that_picks_sources():
    """They used to be stored and nothing more, which made planner.md's promise
    about steering a description of a dead channel."""
    src = (CODE / "flow.py").read_text(encoding="utf-8")
    assert '"source_hints": hints' in src or '["source_hints"] = hints' in src \
        or '_meta["source_hints"]' in src, "the fan-out must pass the hints on"
    note = skills._source_hints_note(["papers", "news"])
    assert "arxiv_search" in note and "news_search" in note, \
        "a hint must resolve to the tools that serve it, named for the worker"
    assert "papers -> arxiv" in note.replace("  ", " ")


def test_image_results_are_untrusted_guarded():
    """An image search returns third-party titles and creator strings, and the
    author is told to write them into a caption. Both tools were missing from
    the list that puts the untrusted-content contract in a prompt."""
    for name in ("search_images", "fetch_image"):
        assert name in skills.UNTRUSTED_TOOLS, f"{name} reaches the model raw"


def test_formatter_gets_the_json_repair_and_author_deliberately_does_not():
    """The formatter's `final_answer` IS the user's answer, so a prose reply
    used to throw it away. The author is left out on purpose: with nothing
    rendered, a repair call asking for "the receipt JSON" is asking for a
    receipt for a file that does not exist."""
    assert "formatter" in skills._JSON_CONTRACT_SKILLS
    assert "author" not in skills._JSON_CONTRACT_SKILLS
    assert "deck" not in skills._JSON_CONTRACT_SKILLS


def test_critic_is_not_the_one_persona_left_on_the_old_cap():
    """Every sibling was raised after measured truncation; the critic judged
    whole upstream outputs on 500 tokens, and a JSON rationale cut mid-token
    reads as a broken node and triggers a replan for nothing."""
    assert (_registry().get("critic").max_tokens or 0) >= 1500
