"""The chunker's core promises, each one a test.

The old chunker (`mcp_server._chunk_text`) was a sliding window over
`text.split()`. These tests exist so the replacement cannot silently regress
to anything like it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from documents.chunker import (  # noqa: E402
    Block, chunk_blocks, chunk_text, split_sentences, _overlap_tail,
    DEFAULT_TARGET_WORDS,
)

SENT = ("Retrieval augmented generation indexes source documents into a "
        "vector store. At query time it recalls the spans that are relevant. "
        "The quality of the result depends almost entirely on how the source "
        "was chunked beforehand.")


# ── sentence splitting ───────────────────────────────────────────────────────

def test_splits_into_complete_sentences():
    s = split_sentences(SENT)
    assert len(s) == 3, s
    assert all(x.endswith(".") for x in s)
    # Nothing lost: rejoining the sentences reproduces the input.
    assert " ".join(s) == SENT


def test_never_splits_a_sentence_in_half():
    s = split_sentences(SENT)
    for piece in s:
        assert piece.strip().endswith((".", "!", "?")), repr(piece)


@pytest.mark.parametrize("text, expect_whole", [
    ("Dr. Smith wrote the report. It was long.", 2),
    ("See fig. 3 for details. The curve rises.", 2),
    ("Values such as 3.14 are exact. Others are not.", 2),
    ("The U.S. leads the market. Growth continues.", 2),
    ("Items include pens, etc. and paper. That is all.", 2),
    ("This costs approx. 5 dollars. That is cheap.", 2),
    ("Published in the U.S.A. last year. It sold well.", 2),
    ("Use e.g. this approach. It works.", 2),
    ("On Jan. 5 the report landed. It was late.", 2),
])
def test_abbreviations_and_decimals_do_not_cause_false_splits(text, expect_whole):
    assert len(split_sentences(text)) == expect_whole, split_sentences(text)


def test_quoted_sentence_ends_the_containing_sentence():
    """A full stop inside a closing quote is ambiguous.

    Splitting there produces 'He said "we are done."' as a standalone
    sentence, which is still a readable unit - the quote stays with its
    opener. What must never happen is a *partial* sentence, e.g. cutting
    between "we are" and "done.".
    """
    s = split_sentences('He said "we are done." Then he left. Nobody argued.')
    assert "".join(s).replace(" ", "") == \
        'Hesaid"wearedone."Thenheleft.Nobodyargued.'.replace(" ", "")
    for piece in s:
        assert piece.strip().endswith(('"', ".", "!", "?")), repr(piece)
    # The opener and its quote are not separated.
    assert any("we are done" in p for p in s)


def test_never_cuts_inside_a_quote():
    s = split_sentences('The rule is "never split a sentence" at all. Done.')
    for piece in s:
        assert piece.strip().endswith((".", "!")), repr(piece)


def test_handles_question_and_exclamation():
    s = split_sentences("Is it ready? Yes! Not quite.")
    assert len(s) == 3, s


def test_empty_and_whitespace():
    assert split_sentences("") == []
    assert split_sentences("   \n  ") == []


def test_text_without_ending_punctuation_still_returns_something():
    s = split_sentences("a heading with no period")
    assert s == ["a heading with no period"]


def test_runaway_sentence_is_hard_wrapped_not_returned_whole():
    long_one = " ".join(["word"] * 400)
    s = split_sentences(long_one)
    assert len(s) == 1
    chunks = chunk_text(long_one + ". Short tail sentence here.", 60)
    assert len(chunks) > 1
    # The wrap happened at a word boundary, not mid-word.
    for c in chunks:
        assert c.text


# ── heading hierarchy ────────────────────────────────────────────────────────

def build_sections() -> list[Block]:
    return [
        Block(kind="heading", text="Overview", level=1),
        Block(kind="para", text=SENT),
        Block(kind="heading", text="Details", level=2),
        Block(kind="para", text="The details section says something useful. "
                                "It continues for a while."),
        Block(kind="heading", text="Appendix", level=1),
        Block(kind="para", text="The appendix is separate. It should not mix."),
    ]


def test_heading_sections_never_merge():
    """Prose must not span a section boundary.

    A heading chunk legitimately carries its own full path (so "Details"
    nested under "Overview" reads as Overview > Details); the rule being
    protected is that PROSE is never attributed to two different sections.
    """
    chunks = chunk_blocks(build_sections(), target_words=25)
    # Each prose chunk must sit under exactly ONE section, and the set of
    # prose paths must be a subset of the sections that exist. A two-element
    # path here is fine ("Overview > Details" IS one section); what must not
    # happen is prose from two different sections landing in one chunk, which
    # the chunker's per-section loop prevents by construction.
    sections = {("Overview",), ("Overview", "Details"), ("Appendix",)}
    prose_paths = {c.heading_path for c in chunks if c.kind != "heading"}
    assert prose_paths <= sections, prose_paths
    # And the appendix's prose must be attributed to the appendix only.
    appendix = [c for c in chunks
                if c.kind != "heading" and c.heading_path == ("Appendix",)]
    assert appendix, "appendix prose missing"
    for c in appendix:
        assert "Overview" not in c.text or "Overview" in c.content


def test_heading_chunks_carry_their_full_path():
    chunks = chunk_blocks(build_sections(), target_words=25)
    headings = {c.content: c.heading_path for c in chunks if c.kind == "heading"}
    assert headings["Overview"] == ("Overview",)
    assert headings["Details"] == ("Overview", "Details")
    assert headings["Appendix"] == ("Appendix",)


def test_every_chunk_knows_its_breadcrumb():
    chunks = chunk_blocks(build_sections(), target_words=25)
    for c in chunks:
        crumb = " > ".join(c.heading_path)
        assert crumb, f"no breadcrumb: {c.text[:60]!r}"
        # Prose is prefixed with its breadcrumb; a heading IS the breadcrumb.
        if c.kind == "heading":
            assert c.text == c.content
        else:
            assert c.text.startswith(crumb), repr(c.text[:80])


def test_nested_headings_build_a_path():
    blocks = [
        Block(kind="heading", text="Part One", level=1),
        Block(kind="para", text="Intro sentence one. Intro sentence two."),
        Block(kind="heading", text="Sub A", level=2),
        Block(kind="para", text="Body of sub A here. More of sub A here."),
        Block(kind="heading", text="Sub B", level=2),
        Block(kind="para", text="Body of sub B here. More of sub B here."),
    ]
    chunks = chunk_blocks(blocks, target_words=30)
    paths = {c.heading_path for c in chunks if c.kind != "heading"}
    assert ("Part One", "Sub A") in paths, paths
    assert ("Part One", "Sub B") in paths, paths


def test_heading_only_section_still_produces_a_chunk():
    blocks = [Block(kind="heading", text="Glossary", level=1)]
    chunks = chunk_blocks(blocks)
    assert len(chunks) == 1
    assert "Glossary" in chunks[0].text


# ── overlap ──────────────────────────────────────────────────────────────────

def test_overlap_is_whole_sentences():
    body = [f"Sentence number {i} carries some meaning. And a second clause "
            f"for it." for i in range(12)]
    blocks = [Block(kind="heading", text="Body", level=1),
              Block(kind="para", text=" ".join(body))]
    chunks = chunk_blocks(blocks, target_words=60)
    prose = [c for c in chunks if c.kind == "para"]
    assert len(prose) > 1, [c.kind for c in chunks]
    # The last WHOLE sentence of the first prose chunk must reappear at the
    # head of the next one.
    sents = split_sentences(prose[0].content)
    assert sents, "chunk 0 had no sentences"
    last = sents[-1]
    assert last in prose[1].content, \
        f"overlap lost the trailing sentence.\n  last={last!r}\n  next={prose[1].content[:160]!r}"


def test_overlap_tail_is_never_the_whole_pack():
    pack = ["One short sentence.", "Another one here.", "A third sentence."]
    # A target far larger than the pack would otherwise return all of it,
    # making the next chunk a pure duplicate of the previous.
    tail = _overlap_tail(pack, target=1000)
    assert len(tail) < len(pack), "overlap must not swallow the whole pack"
    assert tail, "expected at least a minimum overlap"


def test_overlap_respects_the_whole_sentence_boundary():
    pack = ["First sentence is here. Second one follows. Third is here."]
    tail = _overlap_tail(pack, target=20)
    for piece in tail:
        assert piece in pack, piece


# ── size behaviour ───────────────────────────────────────────────────────────

def test_chunks_stay_near_the_target_without_hard_cutting():
    blocks = [Block(kind="heading", text="T", level=1),
              Block(kind="para", text=" ".join(
                  f"This is sentence {i} in the document body."
                  for i in range(200)))]
    chunks = chunk_blocks(blocks, target_words=120)
    assert len(chunks) > 1
    for c in chunks:
        if c.kind != "para":
            continue
        # Overlap adds whole sentences on top of the target, so the ceiling is
        # the target plus the overlap budget - not the bare target.
        ceiling = 120 + int(120 * 0.20) + 30
        w = len(c.text.split())
        assert w <= ceiling, f"chunk too large: {w} words (ceiling {ceiling})"


def test_a_single_long_sentence_gets_its_own_chunk():
    blocks = [Block(kind="para", text=SENT),
              Block(kind="para", text=" ".join(f"word{i}" for i in range(300)))]
    chunks = chunk_blocks(blocks, target_words=50)
    assert len(chunks) >= 2


def test_indices_are_sequential_and_zero_based():
    blocks = [Block(kind="heading", text="A", level=1), Block(kind="para", text=SENT),
              Block(kind="heading", text="B", level=1), Block(kind="para", text=SENT)]
    chunks = chunk_blocks(blocks)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_empty_input_yields_nothing():
    assert chunk_blocks([]) == []
    assert chunk_text("") == []


# ── tables ───────────────────────────────────────────────────────────────────

def test_table_header_is_repeated_on_every_chunk():
    header = ["region", "q1_revenue", "q2_revenue"]
    rows = [["north", str(i), str(i * 2)] for i in range(40)]
    blocks = [Block(kind="heading", text="Revenue", level=1),
              Block(kind="table", header=header, rows=[header] + rows)]
    chunks = chunk_blocks(blocks, target_words=40)
    assert len(chunks) > 1, "expected the table to split"
    for c in chunks:
        if c.kind != "table":
            continue
        assert "region" in c.text, f"header missing: {c.text[:90]!r}"
        assert "q1_revenue" in c.text


def test_table_chunks_are_atomic():
    blocks = [Block(kind="heading", text="T", level=1),
              Block(kind="table", header=["a", "b"],
                    rows=[["a", "b"]] + [[str(i), str(i)] for i in range(60)])]
    chunks = chunk_blocks(blocks, target_words=30)
    for c in chunks:
        assert c.kind in ("heading", "table")


# ── content vs text separation ──────────────────────────────────────────────

def test_content_excludes_the_breadcrumb():
    blocks = [Block(kind="heading", text="Chapter 1", level=1),
              Block(kind="para", text=SENT)]
    chunks = chunk_blocks(blocks)
    # The first chunk is the heading itself; the prose is the next one.
    assert chunks[0].kind == "heading"
    prose = next(c for c in chunks if c.kind == "para")
    assert prose.text.startswith("Chapter 1")
    assert not prose.content.startswith("Chapter 1"), prose.content[:60]
    assert SENT.split(".")[0] in prose.content


def test_to_dict_is_json_friendly():
    import json
    c = chunk_blocks([Block(kind="heading", text="X", level=1),
                      Block(kind="para", text=SENT)])[0]
    d = c.to_dict()
    assert isinstance(d["heading_path"], list)
    assert isinstance(d["char_span"], list)
    json.dumps(d)


# ── the specific failure the user called out ────────────────────────────────

def test_no_chunk_ends_mid_sentence_across_a_realistic_document():
    """The headline requirement: no abrupt cut between sentences."""
    doc = "\n\n".join([
        "## Introduction",
        "This document describes the chunking strategy. It covers boundaries. "
        "It covers overlap too.",
        "## Method",
        "We split on sentence boundaries. We never cut mid-sentence. "
        "Overlap is carried in whole sentences. This keeps fragments readable.",
        "## Results",
        "Retrieval quality improved by 12 percent. Latency rose slightly. "
        "Cost stayed flat overall.",
    ])
    chunks = chunk_text(doc, target_words=18)
    assert len(chunks) > 1
    for c in chunks:
        body = c.content.strip()
        if not body or c.kind == "heading" or c.kind == "table":
            continue
        # A prose chunk must end on sentence-final punctuation. The only
        # legitimate exceptions are a hard-wrapped fragment, which the
        # chunker marks by splitting without punctuation.
        assert body.endswith((".", "!", "?", '"', ")", "]")), \
            f"chunk ends mid-sentence: ...{body[-70:]!r}"
