"""The whole Authoring chain, end to end, against the REAL gateway.

Everything here is real except the model's decision to pick the tool: the
render goes through the gateway's reportlab/pptx/docx generators, the bytes
come back, they are stored as a content-addressed artifact, the receipt is
built from that record, and the console serves the file back. The four things
this caught that unit tests could not:

  1. an `author` node that invented an artifact handle and produced a
     "Created report.pdf" receipt for a file that did not exist,
  2. a stall guard that threw away a document the renderer had already
     stored,
  3. a tool-less skill dying on the new bookkeeping,
  4. the model refusing the task outright instead of delivering a document.
"""
import asyncio
import json
import re

import pytest

# Drives the real gateway, so it carries the repo's `live` marker: skipped
# unless the user asks with `-m live`. Calling auth.configure() instead would
# replace the per-launch token for the WHOLE test session - that broke 32
# unrelated tests the first time this was written.
pytestmark = pytest.mark.live

ART = re.compile(r"art:[0-9a-fA-F]{16}$")

SPEC_BLOCKS = [
    {"type": "cover", "title": "Placement Rules", "subtitle": "A live test",
     "meta": ["Prepared for the console"]},
    {"type": "heading", "level": 1, "text": "Purpose"},
    {"type": "paragraph", "text": ("Legal writing places authorities by "
                                   "preference. " * 60)},
    {"type": "heading", "level": 1, "text": "Formats"},
    {"type": "chart", "kind": "bar",
     "categories": ["Cases", "Statutes", "Rules", "Treatises"],
     "series": [{"name": "Citations", "data": [42, 31, 18, 9]}],
     "title": "Authority mix"},
    {"type": "table", "header": ["Format", "Use"],
     "rows": [["Case", "Citing a case"], ["Rule", "Citing a rule"]]},
]


def _run_author(blocks, toc=True):
    """Drive the author skill with a model that calls render_document once."""
    import mcp_runner
    import skills
    import mcp_server

    skill = skills.SkillRegistry().get("author")
    state = {"n": 0}

    async def fake_reply(**kw):
        state["n"] += 1
        cb = kw.get("on_outcome")
        if cb and state["n"] == 1:
            # The REAL tool, against the REAL gateway.
            result_text = mcp_server.render_document(
                format="pdf", title="Placement Rules", blocks=blocks, toc=toc)
            cb("render_document", {"format": "pdf"}, True, result_text, 0.9)
        return {"text": json.dumps({
            "filename": "placement-rules.pdf", "format": "pdf",
            "artifact": "art:i-made-this-up",
            "summary": "done"}),
            "provider": "test", "tool_calls": []}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        return asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s8-live", "write a report", None))[0]
    finally:
        mcp_runner.run_with_tools = original


@pytest.mark.timeout(300)
def test_a_real_render_becomes_a_real_downloadable_receipt():
    result = _run_author(SPEC_BLOCKS)
    assert result.success is True, result.error

    art = result.output["artifact"]
    assert ART.match(art), f"not a content-addressed id: {art!r}"
    assert art != "art:i-made-this-up", "the model's invented id survived"
    assert result.output["produced"][0]["artifact"] == art
    assert result.output["filename"] == "placement-rules.pdf"

    # The delivered stats ride along, so the receipt can state the size.
    stats = result.output["produced"][0]["stats"]
    assert stats["words"] > 100, stats
    assert stats["format"] == "pdf"


@pytest.mark.timeout(300)
def _token(monkeypatch):
    """The agent's per-launch token, set only for this test."""
    import auth
    monkeypatch.setattr(auth, "_token", "test-token", raising=False)
    return auth.token()


def test_the_console_serves_that_file_and_it_contains_what_was_asked_for(monkeypatch):
    from fastapi.testclient import TestClient
    import agent_server

    result = _run_author(SPEC_BLOCKS)
    art = result.output["artifact"]

    client = TestClient(agent_server.app)
    hdr = {"X-Aria-Token": _token(monkeypatch)}
    r = client.get(f"/api/artifact/{art}?download=1", headers=hdr)
    assert r.status_code == 200, r.text[:200]
    assert r.content.startswith(b"%PDF-")

    import pymupdf
    d = pymupdf.open(stream=r.content, filetype="pdf")
    text = "\n".join(p.get_text() for p in d)
    assert d.page_count >= 2, d.page_count
    assert "Placement Rules" in d[0].get_text(), "cover is not page one"
    assert "Purpose" in text and "Formats" in text
    # The chart is drawn, not photographed.
    assert sum(len(p.get_drawings()) for p in d) > 20, "no vector output"
    assert "attachment; filename=" in r.headers.get("content-disposition", "")
    assert ".pdf" in r.headers.get("content-disposition", "")


@pytest.mark.timeout(300)
def test_the_artifact_id_in_a_receipt_is_always_downloadable(monkeypatch):
    """Every id the receipt can name must be servable, because the receipt is
    what the user acts on."""
    from fastapi.testclient import TestClient
    import agent_server

    result = _run_author(SPEC_BLOCKS)
    art = result.output["artifact"]
    client = TestClient(agent_server.app)
    hdr = {"X-Aria-Token": _token(monkeypatch)}
    assert client.get(f"/api/artifact/{art}?download=0",
                      headers=hdr).status_code == 200
    # And the hallucinated shape really is refused, which is why the receipt
    # must never be built from the model's JSON.
    assert client.get("/api/artifact/art:doc-gen-pipeline?download=1",
                      headers=hdr).status_code == 400