r"""A section whose prose contained LaTeX used to print its own JSON on the page.

Live document, 23 pages, "how do LLMs work". Sections 4 and 6 came back as one
giant paragraph reading:

    ```json { "blocks": [ { "type": "heading", "level": 1, "text": "Inference
    mechanics, optimization, and quantization" }, { "type": "paragraph", ...

The cause is a single backslash. The author wrote `$\mathcal{O}(N^2)$` inside a
JSON string, `\m` is not a legal JSON escape, so `json.loads` rejected the whole
reply at every escalation level. The fallback that "keeps the words rather than
discarding the call" then tested `raw.startswith("{")` — and the reply was
fenced in ```json, so it started with a backtick, the guard missed, and the
scaffolding became the content.
"""
import json

import pytest

skills = pytest.importorskip("skills")

LATEX_REPLY = (
    '```json\n'
    '{"blocks": ['
    '{"type": "heading", "level": 1, "text": "Inference mechanics"},'
    '{"type": "paragraph", "text": "The operational phase of LLMs, known as '
    '**inference**, presents distinct challenges."},'
    '{"type": "paragraph", "text": "resulting in a catastrophic '
    '$\\mathcal{O}(N^2)$ computational complexity."}'
    ']}\n```'
)


def test_latex_inside_a_string_no_longer_loses_the_section():
    parsed = skills.parse_skill_json(LATEX_REPLY)
    blocks = parsed.get("blocks")
    assert isinstance(blocks, list) and len(blocks) == 3, parsed
    assert blocks[2]["text"].count("$") == 2, blocks[2]["text"]
    # The repair doubles the backslash, so the author's text survives; the
    # renderer is what turns it into O(N²).
    assert "\\mathcal{O}(N^2)" in blocks[2]["text"], blocks[2]["text"]


def test_the_repair_leaves_legal_escapes_alone():
    """The pass must not damage valid JSON on its way past one bad escape."""
    for raw in ('{"a": "line\\nbreak"}', '{"a": "quote\\" inside"}',
                '{"a": "back\\\\slash"}', '{"a": "caf\\u00e9"}',
                '{"a": "tab\\there"}', '{"a": "slash/n not an escape"}'):
        got = skills.parse_skill_json(raw)
        assert got, raw
        assert json.dumps(got, ensure_ascii=False) is not None
    assert skills.parse_skill_json('{"a": "line\\nbreak"}')["a"] == "line\nbreak"
    assert skills.parse_skill_json('{"a": "caf\\u00e9"}')["a"] == "café"
    assert skills.parse_skill_json('{"a": "back\\\\slash"}')["a"] == "back\\slash"


def test_a_latex_command_is_not_read_as_a_json_control_escape():
    """The other half of the same bug.

    `\t`, `\f`, `\r`, `\n`, `\b` ARE valid JSON escapes, so doubling only the
    *invalid* ones let `\theta` parse as a TAB followed by "heta" and `\frac`
    as a form feed plus "rac". The section then rendered, which looks like
    success, and the reader got "Ieta D_{KL}" and "rac{alpha}{r}" — mangled
    maths from a live document's page 15.
    """
    for raw, want in (
            (r'{"a": "\theta"}', "\\theta"),
            (r'{"a": "\frac{1}{2}"}', "\\frac{1}{2}"),
            (r'{"a": "cost \times two"}', "cost \\times two"),
            (r'{"a": "\left( x \right)"}', "\\left( x \\right)"),
            (r'{"a": "\mathbb{E}[x]"}', "\\mathbb{E}[x]"),
            (r'{"a": "\alpha and \beta"}', "\\alpha and \\beta")):
        got = skills.parse_skill_json(raw)
        assert got.get("a") == want, (raw, got)


def test_a_real_json_escape_is_still_a_real_json_escape():
    """The LaTeX rule must not swallow the ordinary ones it overlaps with."""
    assert skills.parse_skill_json(r'{"a": "line\nnext"}')["a"] == "line\nnext"
    assert skills.parse_skill_json(r'{"a": "tab\there"}')["a"] == "tab\there"
    assert skills.parse_skill_json(r'{"a": "carriage\rreturn"}')["a"] == \
        "carriage\rreturn"
    assert skills.parse_skill_json(r'{"a": "caf\u00e9"}')["a"] == "café"
    assert skills.parse_skill_json(r'{"a": "quote\" inside"}')["a"] == \
        'quote" inside'
    assert skills.parse_skill_json(r'{"a": "back\\slash"}')["a"] == "back\\slash"


def test_a_fenced_spec_is_recognised_as_a_spec():
    assert skills._looks_like_block_spec(LATEX_REPLY)
    assert skills._looks_like_block_spec('{"blocks": []}')
    assert not skills._looks_like_block_spec(
        "Inference is bound by memory bandwidth, which is a fact.")


def test_the_salvage_keeps_sentences_and_drops_scaffolding():
    """When a spec still cannot be parsed, the reader gets the prose, not the
    braces — and never a stray `"type":`."""
    broken = ('```json\n{"blocks": [{"type": "paragraph", "text": "First point."},'
              ' {"type": "paragraph", "text": "Second point."}]}\n```')
    blocks = skills._salvage_spec_prose("Section nine", broken)
    texts = [b.get("text", "") for b in blocks]
    assert "First point." in texts and "Second point." in texts, texts
    for b in blocks:
        assert b["type"] in ("heading", "paragraph"), b
        assert '"type"' not in b["text"] and "{" not in b["text"], b


def test_the_prose_fallback_cannot_print_a_block_spec():
    """Structural guard on the branch itself: the naive `startswith("{")` test
    is what let a fenced spec through."""
    import inspect
    src = inspect.getsource(skills._sectioned_document_blocks)
    assert 'raw.startswith("{")' not in src, (
        "the JSON detector is back to testing the first character only; a "
        "reply fenced in ```json starts with a backtick and its whole spec "
        "will be rendered as a paragraph")
    assert "_looks_like_block_spec" in src
