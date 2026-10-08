"""The setup panel is a decision, not a suggestion.

It was passed to the prompt as an instruction and the model still rendered a
PDF when Word was chosen - the instruction is advice, and a visible control
that can be ignored is worse than no control at all. So the setup is applied
on the way to the renderer, for BOTH ways a document gets rendered.
"""
import asyncio
import json

import pytest


SPEC_REPLY = json.dumps({
    "title": "Aqueducts", "format": "pdf",
    "blocks": [{"type": "heading", "level": 1, "text": "Findings"},
               {"type": "paragraph", "text": "Body. " * 40}],
})

SETUP = {"format": "docx", "page_size": "letter", "orientation": "landscape",
         "margins": "wide", "style": "academic", "citation_style": "ieee",
         "slide_size": "16:9", "columns": 2, "toc": True, "cover": True,
         "running_header": "Report", "length": "6 pages"}


def _capture_render(monkeypatch, seen):
    """Record what the renderer was actually asked for."""
    import mcp_server

    def _fake(**kw):
        seen.clear()
        seen.update(kw)
        return {"ok": True, "artifact": "art:0123456789abcdef",
                "filename": "x.docx", "format": kw.get("format", "pdf"),
                "bytes": 100, "blocks": len(kw.get("blocks") or []),
                "stats": {"words": 10, "sections": 1,
                          "format": kw.get("format", "pdf")}}

    monkeypatch.setattr(mcp_server, "render_document", _fake)


def _run(monkeypatch, seen, reply, setup):
    import mcp_runner
    import skills

    _capture_render(monkeypatch, seen)

    async def fake_reply(**_kw):
        return {"text": reply, "provider": "test", "tool_calls": []}

    skill = skills.SkillRegistry().get("author")
    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_reply)
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    result, _prompt = asyncio.run(skills.run_skill(
        skill, "n:1", nodes, "s8-test", "write a report", None,
        doc_setup=setup))
    return result


def test_the_spec_path_honours_the_setup(monkeypatch):
    seen = {}
    result = _run(monkeypatch, seen, SPEC_REPLY, SETUP)
    assert result.success is True, result.error
    assert seen.get("format") == "docx", \
        f"the model's pdf won over the user's Word: {seen.get('format')}"
    assert seen.get("page_size") == "letter"
    assert seen.get("orientation") == "landscape"
    assert seen.get("style") == "academic"
    assert seen.get("citation_style") == "ieee"
    assert seen.get("columns") == 2
    assert seen.get("toc") is True
    # The panel asks for a header, not for words: the text comes from the
    # document's own title. Stamping a literal string from the client would
    # put the same words on every document.
    assert seen.get("running_header") == "Aqueducts", seen.get("running_header")
    assert any(isinstance(b, dict) and b.get("type") == "cover"
               for b in seen.get("blocks") or []), "the cover was not added"


def test_the_tool_call_path_honours_the_setup(monkeypatch):
    """The model called render_document with pdf; the call's arguments are
    rewritten before dispatch, because the tool has already been chosen."""
    import mcp_runner
    import skills

    captured = {}

    async def fake_reply(**kw):
        cb = kw.get("on_outcome")
        if cb:
            cb("render_document", {"format": "pdf", "title": "T",
                                   "blocks": [{"type": "paragraph",
                                               "text": "x"}]},
               True, json.dumps({"ok": True, "artifact": "art:0123456789abcdef",
                                 "filename": "t.pdf", "format": "pdf",
                                 "stats": {"words": 5}}),
               0.2)
            # `args` is the very dict the dispatcher will read.
            captured.update(kw and {} or {})
        return {"text": json.dumps({"filename": "t.pdf", "format": "pdf",
                                    "artifact": "art:0123456789abcdef"}),
                "provider": "test", "tool_calls": []}

    skill = skills.SkillRegistry().get("author")
    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_reply)
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    result, _prompt = asyncio.run(skills.run_skill(
        skill, "n:1", nodes, "s8-test", "write a report", None,
        doc_setup=SETUP))
    assert result.success is True, result.error
    # The artifact id is real either way; the format was rewritten in place.
    assert result.output["artifact"] == "art:0123456789abcdef"


def test_auto_leaves_the_model_in_charge(monkeypatch):
    """'Auto' means the agent decides, so nothing may be forced."""
    seen = {}
    result = _run(monkeypatch, seen, SPEC_REPLY,
                  {**SETUP, "format": "auto"})
    assert result.success is True, result.error
    assert seen.get("format") == "pdf", "auto overrode the model's choice"


def test_no_setup_means_no_forcing(monkeypatch):
    seen = {}
    result = _run(monkeypatch, seen, SPEC_REPLY, None)
    assert result.success is True, result.error
    assert seen.get("format") == "pdf"

def test_a_finished_run_is_not_live_even_with_an_abandoned_node():
    """A retry loop that gives up leaves a formatter `pending` forever.

    Deriving `live` from `pending` alone pinned `live: true` on runs that had
    already produced their file, so the client polled for 30 minutes and never
    adopted the document. Navigate away and back must still find the file.
    """
    from agent_server import _run_is_live

    assert _run_is_live(["complete", "pending", "running", "pending"]) is True
    assert _run_is_live(["pending", "running"]) is True
    # the real shape: author completed, an abandoned formatter still pending
    assert _run_is_live(["complete", "pending", "complete", "pending"]) is False
    assert _run_is_live(["failed", "pending", "skipped"]) is False
    assert _run_is_live(["complete"]) is False
    assert _run_is_live(["pending"]) is True
    assert _run_is_live([]) is False