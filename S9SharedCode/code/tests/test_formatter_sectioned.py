import asyncio
import json as _json

import skills
def test_section_envelope_is_unwrapped_before_assembly():
    """Regression: the sectioned Formatter shipped raw JSON to the user.

    A section call answers with the envelope its own prompt asks for
    (`{"final_answer": "## Storage..."}`), and the assembled answer
    was the concatenation of those envelopes: the user read
    `{"final_answer": "...4,000 words..."}` instead of the report. Worse,
    the model emits LITERAL newlines inside the value rather than
    escaped ones, so a strict json.loads raises "Invalid control
    character" on exactly the reply that needs unwrapping.
    """
    envelope = ('{\n  "final_answer": "## Storage\n\nDurability details '
                'here."\n}')
    assert skills._strip_answer_envelope(envelope).startswith("## Storage")

    # Plain prose passes through untouched.
    assert skills._strip_answer_envelope("## Plain\n\ntext") == "## Plain\n\ntext"
    # Braces and quotes inside prose are NOT treated as an envelope.
    assert skills._strip_answer_envelope('Use {x} and "q" here.') == \
        'Use {x} and "q" here.'
    # Unparseable input is returned as-is (stripped) rather than swallowed.
    assert skills._strip_answer_envelope('{"a": ') == '{"a":'
    assert skills._strip_answer_envelope("") == ""
    # Alternate envelope keys are unwrapped too.
    assert skills._strip_answer_envelope('{"answer": "## Alt\\n\\nb"}') == \
        "## Alt\n\nb"
    # Unterminated envelope (observed live: 1 open brace, 0 close, so no
    # JSON parse can recover it). The shape is fixed, so take the body.
    unterminated = '{"final_answer": "## Storage\n\nDurability here.'
    assert skills._strip_answer_envelope(unterminated) == \
        "## Storage\n\nDurability here."
    # ...and with a trailing quote/brace/comma the model may have added.
    assert skills._strip_answer_envelope(
        '{"final_answer": "## S\n\nb",}') == "## S\n\nb"


def test_sectioned_formatter_assembles_one_section_per_input():
    """A multi-source run must produce one section per upstream result,
    joined into one answer: the property the whole sectioned path exists
    for (the keyed models cap a single call at ~300 words)."""
    import asyncio

    class _SecLLM:
        def __init__(self):
            self.prompts = []

        def chat(self, prompt=None, **kw):
            self.prompts.append(prompt or "")
            if "WRITE THE OPENING ONLY" in (prompt or ""):
                return {"text": "Orientation paragraph."}
            # Each section answers in the envelope shape.
            n = len(self.prompts)
            return {"text": _json.dumps(
                {"final_answer": f"## Section {n}\n\nBody {n}."})}

    reg = skills.SkillRegistry()
    fm = reg.get("formatter")
    assert fm.sectioned is True, "formatter must be configured sectioned"

    fake = _SecLLM()
    orig = skills.LLM
    skills.LLM = lambda *a, **k: fake
    try:
        out = asyncio.run(skills._sectioned_final_answer(
            fm, "prompt", [{"question": "a"}, {"question": "b"},
                           {"question": "c"}],
            "q", "sess"))
    finally:
        skills.LLM = orig

    assert out, "assembly must return text"
    assert "Orientation paragraph." in out
    # Sections are written first (3 calls), the lead last (4th).
    for n in (1, 2, 3):
        assert f"## Section {n}" in out, out[:200]
    # No envelope may survive into the answer.
    assert "final_answer" not in out
    assert not out.strip().startswith("{")


def test_sectioned_formatter_falls_back_to_single_call():
    """With fewer than two upstream results there is nothing to expand,
    so the helper must decline and let the normal single-call path run."""
    import asyncio

    reg = skills.SkillRegistry()
    fm = reg.get("formatter")

    class _NeverCalled:
        def chat(self, *a, **k):
            raise AssertionError("must not call the model")

    orig = skills.LLM
    skills.LLM = lambda *a, **k: _NeverCalled()
    try:
        out = asyncio.run(skills._sectioned_final_answer(
            fm, "prompt", [{"question": "only one"}], "q", "sess"))
    finally:
        skills.LLM = orig
    assert out is None

