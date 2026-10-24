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
           "chart", "image", "table", "note", "pagebreak"}
PDF_ALLOWED = ALLOWED - {"note"}  # note is pptx speaker-notes only


def test_depth_defines_three_distinct_commitments():
    low = PL.lower()
    for lvl in ("`quick`", "`normal`", "`deep`"):
        assert lvl in low, f"{lvl} is no longer defined"
    # the old bug: deep and normal were the same commitment described twice
    assert "one researcher per facet" in low
    deep_line = next(l for l in PL.splitlines() if l.strip().startswith("- `deep`"))
    normal_line = next(l for l in PL.splitlines()
                       if l.strip().startswith("- `normal`"))
    assert deep_line.strip() != normal_line.strip(), (
        "deep and normal are the same commitment; the dimension that changes "
        "cost (how many researchers) must be distinct")
    assert "researcher" in deep_line.lower()
    assert "researcher" in normal_line.lower()


def test_depth_says_it_is_read_at_runtime():
    """The field was documented as load-bearing while nothing consumed it."""
    low = PL.lower()
    assert "read by the executor" in low or "executor" in low, (
        "depth is described as decorative; if it is actually read, say so, "
        "and if it is not, do not call it a commitment")


def test_facet_count_is_derived_from_the_target_not_a_fixed_band():
    """6 x 4,000 = 24,000 < the 27,000-word floor for 60 pages."""
    assert re.search(r"\b(60|100)\b.*(facet|words)", PL, re.I), (
        "no worked sizing: the facet count must be derived from the document "
        "the user asked for")
    assert "x 450" in PL or "x450" in PL or "450 words" in PL, (
        "the words-per-page figure the author contract uses is not in the "
        "planner, so its facet arithmetic cannot be checked")


def test_the_worked_example_sizing_fits_its_own_document():
    body = PL[PL.index("Worked example"):] if "Worked example" in PL else ""
    m = re.search(r"(\d+)\s*pages?\s*[x×]\s*450\s*words?\s*=\s*\*{0,2}"
                  r"([\d,]+)", body)
    assert m, ("the worked example no longer derives its word target with the "
               "pages x 450 arithmetic")
    pages, target = int(m.group(1)), int(m.group(2).replace(",", ""))
    assert pages * 450 == target, (pages, target)
    assert target >= 27_000, target
    n = re.search(r"\*\*(\d+)\s+distinct subject areas", body) \
        or re.search(r"(\d+)\s+facets", body) \
        or re.search(r"=\s*\*\*(\d+)\s+facets", body)
    assert n, "the worked example states no facet count"
    facets = int(n.group(1))
    assert facets * 1500 >= target, (
        f"{facets} facets at the 1,500-word floor = {facets*1500} words, "
        f"under the {target} the document needs")


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


def test_no_two_prompts_contradict_each_other_on_facet_shape():
    """researcher.md used to carry a workaround for behaviour the planner
    prompt already forbade."""
    assert "never the request itself" in PL.lower(), (
        "the planner no longer forbids echoing the request as a facet")
    # the researcher must not re-litigate the failure in its own opener
    opener = RS[:RS.index("##")]
    assert "may look like a whole brief" not in opener, (
        "researcher.md still defends against a planner failure the planner's "
        "contract already prohibits; the two prompts assume different things")

def test_the_worked_example_lists_the_facets_it_claims():
    """A worked example that says 18 and lists 10 teaches nothing."""
    body = PL[PL.index("Worked example"):]
    n = re.search(r"\*\*(\d+)\s+distinct subject areas", body) \
        or re.search(r"(\d+)\s+facets", body)
    assert n, "the worked example states no facet count"
    claimed = int(n.group(1))
    listed = body.count("\u2713")
    assert listed >= claimed, (
        f"the example claims {claimed} facets and lists {listed}; the model "
        f"reads the list, not the number")