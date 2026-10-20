import json

"""The format the user asked for is the format that gets delivered.

The Setup card was removed on purpose - eleven controls before you had even
described the document - so the format travels in the user's own sentence:
"... as a PDF". That only works if the brief is BINDING.

Observed live: a brief ending "As a PDF." produced a .docx. The model chose the
format, nothing compared it against the request, and the receipt did not
mention the mismatch - so the user received the wrong file type with no signal
that anything was wrong.
"""
import skills


def test_an_explicit_format_request_in_the_brief_is_honoured():
    cases = [
        ("A one-page board note on onboarding. As a PDF.", "pdf"),
        ("Create a document.\n\nA board deck on energy. As a PPTX file.", "pptx"),
        ("A formal policy memo. Produce a word document.", "docx"),
        ("A budget spreadsheet for three sites. As a spreadsheet.", "xlsx"),
        ("A 10-slide deck on the four-day week.", "pptx"),
        ("A short deck.", "pptx"),
        ("Put together slides on pricing.", "pptx"),
        ("A briefing on cloud migration. Produce a PDF file.", "pdf"),
        ("Write it as a pdf, not a docx.", "pdf"),
    ]
    for text, want in cases:
        got = skills._format_requested_in(text)
        assert got == want, (
            f"{text!r}: asked for {want}, detected {got or 'nothing'}")


def test_a_passing_mention_does_not_pin_the_format():
    """A technical brief can name formats without asking for one.

    If "compare PDF and DOCX export" pinned a format, the console would
    override a deliberate choice on a technicality - and the Setup card is not
    coming back to express one.
    """
    for text in (
        "Compare PDF and DOCX export paths for the new renderer.",
        "A briefing note about the PDF standard.",
        "Why do DOCX files bloat when embedded fonts are used?",
        "Compare deck and PDF export paths.",
    ):
        assert skills._format_requested_in(text) == "", (
            f"{text!r} was read as a format request")


def test_the_last_mention_wins():
    """The last request is nearest the instruction."""
    assert skills._format_requested_in(
        "Produce a PDF. No - produce a word document instead.") == "docx"


def test_the_detected_format_reaches_a_real_tool_call(monkeypatch):
    """Not just detected: it must land on the render call.

    `_forced` is applied in `_prepare`, which runs BEFORE dispatch. With no
    `doc_setup` from the console any more, this is the only thing standing
    between "as a PPTX" and a PDF.

    The fake model calls render_document and - like the real one - does NOT
    mention a format, so anything that arrives at the renderer came from the
    brief.
    """
    import asyncio
    import mcp_runner
    import mcp_server

    seen = {}

    def _fake(**kw):
        seen.update(kw)
        return {"ok": True, "artifact": "art:0123456789abcdef",
                "filename": "x.pptx", "format": kw.get("format"),
                "bytes": 10, "blocks": 1,
                "stats": {"words": 10, "sections": 1, "format": kw.get("format")}}

    state = {"n": 0}

    async def fake_reply(**kw):
        """One faithful tool hop: run the prepare hook, dispatch the renderer,
        report the outcome, then return final text.

        This used to return a tool_call and rely on the REAL `run_with_tools`
        to dispatch it - but `run_with_tools` itself is what this fake
        replaces, so nothing dispatched. The test passed only because the
        harness fallback then made a live model call and rendered whatever it
        got back; when the model answered with no blocks the assertion failed.
        Emulating the hop keeps the test offline and deterministic."""
        state["n"] += 1
        args = {"title": "T", "blocks": [
            {"type": "heading", "level": 1, "text": "H"},
            {"type": "paragraph", "text": "P"}]}
        prep = kw.get("prepare_fn")
        if prep:
            args = prep("render_document", args) or args
        got = _fake(**args)
        on_outcome = kw.get("on_outcome")
        if on_outcome:
            on_outcome("render_document", args, True, json.dumps(got), 0.0)
        return {"text": json.dumps({"filename": got["filename"],
                                    "sections": ["a"]}),
                "provider": "t", "tool_calls": []}

    skill = skills.SkillRegistry().get("author")
    orig_r, orig_t = mcp_server.render_document, mcp_runner.run_with_tools
    mcp_server.render_document = _fake
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s8-t",
            "A briefing note on energy costs. As a PPTX file.", None))
    finally:
        mcp_server.render_document = orig_r
        mcp_runner.run_with_tools = orig_t

    assert seen, "render_document was never called"
    assert seen.get("format") == "pptx", (
        "the brief's format request did not reach the renderer: "
        f"{seen.get('format')!r}")
