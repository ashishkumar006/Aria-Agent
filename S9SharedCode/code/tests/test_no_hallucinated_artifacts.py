"""A run must never report a document that does not exist.

Observed for real, not theorised: an `author` node refused the task, the
retry returned well-formed JSON with
`{"artifact": "art:doc-gen-pipeline", "filename": "...pdf"}` WITHOUT calling
render_document, and the receipt announced a created PDF. Nothing was written,
nothing was stored, and the download button answered 400 "malformed artifact
id". The model's own report is untrusted input.
"""
import json
import re

import pytest

ART = re.compile(r"art:[0-9a-fA-F]{16}")


def test_a_model_cannot_invent_a_downloadable_artifact_id():
    """The only ids the console will serve are content-addressed."""
    assert ART.fullmatch("art:ed877e63519185d9")
    for fake in ("art:doc-gen-pipeline", "art:my-report", "art:", "art:zz",
                 "art:0123456789abcdefEXTRA", "../etc/passwd"):
        assert not ART.fullmatch(fake), f"{fake} must not be served"


def test_produced_entries_come_from_the_tool_record_not_the_model():
    """flow.py's own guard: a produced entry needs the tool's `produced`
    record, and an id that matches the store's format."""
    import flow

    src = open("flow.py", encoding="utf-8").read()
    assert 'out.get("produced")' in src, "flow.py must read the tool record"
    assert "re.fullmatch(r\"art:[0-9a-fA-F]{16}\", art)" in src, \
        "flow.py must reject an artifact id the store could not have minted"


def test_the_receipt_names_only_what_the_renderer_returned():
    import inspect

    import flow
    src = inspect.getsource(flow)
    # The model's handle must never be the source of a download link.
    assert 'out["artifact"] = _authoritative[0]["artifact"]' in open(
        "skills.py", encoding="utf-8").read()


def test_claiming_a_document_without_rendering_fails_the_node():
    """The real behaviour, with the model mocked: it returns well-formed JSON
    naming a file it never rendered."""
    import asyncio
    import mcp_runner
    import skills

    skill = skills.SkillRegistry().get("author")
    assert skill is not None

    async def fake_reply(**_kw):
        # Exactly what the live run returned: valid JSON, invented handle.
        return {"text": '{"artifact": "art:doc-gen-pipeline", '
                       '"filename": "report.pdf", "format": "pdf", '
                       '"summary": "done"}',
                "provider": "test", "tool_calls": []}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(skill, "n:1", nodes, "s8-test", "make a pdf",
                             None))
    finally:
        mcp_runner.run_with_tools = original

    assert result.success is False, \
        "a claimed document with no render_document call was accepted"
    assert "no file was produced" in (result.error or ""), result.error
    assert "report.pdf" in (result.error or "") or "without calling" in \
        (result.error or ""), result.error


def test_writing_the_render_as_data_is_recovered_not_trusted():
    """`{"render_document": {...}}` parses as a clean object, so the node
    reported success while nothing was rendered - a silent false success."""
    import asyncio
    import mcp_server
    import mcp_runner
    import skills

    seen = {}

    def _fake(**kw):
        seen.update(kw)
        return {"ok": True, "artifact": "art:0123456789abcdef",
                "filename": "r.pdf", "format": "pdf", "bytes": 10,
                "blocks": 2, "stats": {"words": 20, "sections": 1,
                                       "format": "pdf"}}

    skill = skills.SkillRegistry().get("author")

    async def fake_reply(**_kw):
        return {"text": json.dumps({"render_document": {
            "format": "pdf", "title": "T",
            "blocks": [{"type": "heading", "level": 1, "text": "H"},
                       {"type": "paragraph", "text": "P"}]}}),
            "provider": "test", "tool_calls": []}

    orig_render, orig_run = mcp_server.render_document, mcp_runner.run_with_tools
    mcp_server.render_document = _fake
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _p = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s8-test", "make a pdf", None))
    finally:
        mcp_server.render_document = orig_render
        mcp_runner.run_with_tools = orig_run

    assert result.success is True, result.error
    assert result.output["artifact"] == "art:0123456789abcdef"
    assert seen.get("title") == "T", "the spec inside the data was not used"


def test_an_author_node_that_renders_nothing_fails():
    """Not "did it claim a document" but "did it MAKE one"."""
    import asyncio
    import mcp_runner
    import skills

    skill = skills.SkillRegistry().get("author")

    async def fake_reply(**_kw):
        # Valid JSON, no tool call, no document.
        return {"text": json.dumps({"sections": ["A"], "summary": "done"}),
                "provider": "test", "tool_calls": []}

    orig = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _p = asyncio.run(skills.run_skill(
            skill, "n:1", nodes, "s8-test", "make a pdf", None))
    finally:
        mcp_runner.run_with_tools = orig
    assert result.success is False, \
        "an author node that rendered nothing reported success"


def test_a_document_spec_is_rendered_by_the_harness_not_the_model():
    """The model writes the CONTENT; the code guarantees the FILE.

    Observed live: with the tool offered and the prompt telling it to call
    that tool, the author called it on some runs and on others just emitted
    the receipt JSON describing a document it never rendered. When the reply
    IS a document spec, the harness renders it - so the step cannot be
    forgotten and the artifact id can never be invented.
    """
    import asyncio
    import mcp_runner
    import skills

    skill = skills.SkillRegistry().get("author")

    async def fake_reply(**_kw):
        # No tool call at all: just the document, as blocks.
        return {"text": json.dumps({
            "title": "Aqueducts", "format": "pdf", "toc": True,
            "blocks": [
                {"type": "heading", "level": 1, "text": "Design"},
                {"type": "paragraph", "text": "A gradient is chosen. " * 40},
            ]}), "provider": "test", "tool_calls": []}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(skill, "n:1", nodes, "s8-test", "make a pdf",
                             None))
    finally:
        mcp_runner.run_with_tools = original

    if not result.success:
        # No gateway in this environment: the node must still fail LOUDLY
        # rather than claim a document.
        assert "no file was rendered" in (result.error or ""), result.error
        return
    art = result.output["artifact"]
    assert re.fullmatch(r"art:[0-9a-fA-F]{16}", art), \
        f"not a real artifact id: {art!r}"
    assert result.output["produced"][0]["stats"]["words"] > 10


def test_a_repeated_successful_call_does_not_discard_the_document():
    """Observed live: render_document returned 200 and stored the PDF, the
    model called it again unchanged, the stall guard aborted the loop, and the
    node reported failure - so a finished document was never offered."""
    import asyncio
    import json
    import mcp_runner
    import skills

    skill = skills.SkillRegistry().get("author")
    real = "art:0123456789abcdef"
    payload = json.dumps({"ok": True, "artifact": real,
                          "filename": "real.pdf", "format": "pdf",
                          "stats": {"words": 2000, "sections": 4,
                                    "format": "pdf"}})
    calls = {"n": 0}

    async def fake_reply(**kw):
        cb = kw.get("on_outcome")
        calls["n"] += 1
        if cb:
            cb("render_document", {"format": "pdf"}, True, payload, 0.4)
        # The model loops on the same call and never writes its summary.
        return {"text": "", "tool_calls": [{
            "id": f"c{calls['n']}", "name": "render_document",
            "arguments": {"format": "pdf"}}], "provider": "test"}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(skill, "n:1", nodes, "s8-test", "make a pdf",
                             None))
    finally:
        mcp_runner.run_with_tools = original

    assert result.success is True, \
        f"a rendered document was thrown away: {result.error}"
    assert result.output["artifact"] == real
    assert result.output["produced"][0]["filename"] == "real.pdf"


def test_a_skill_with_no_tools_does_not_crash_on_the_new_bookkeeping():
    """Regression: the tool's record was bound inside the tools branch, but
    every branch reaches the final return - so every tool-less skill died with
    UnboundLocalError and the whole run was skipped."""
    import asyncio
    import mcp_runner
    import skills

    planner = skills.SkillRegistry().get("planner")
    assert planner is not None
    assert not planner.tools_allowed, "planner should not use tools"

    async def fake_reply(**_kw):
        return {"text": '{"rationale": "plan", "nodes": []}',
                "provider": "test", "tool_calls": []}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "planner", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(planner, "n:1", nodes, "s8-test", "hi", None))
    finally:
        mcp_runner.run_with_tools = original

    assert result.success is True, result.error
    # No tool call, so no `produced` key is invented for it.
    assert "produced" not in result.output


def test_a_real_tool_call_overwrites_the_models_handle():
    """When the tool DID run, its id wins over whatever the model typed."""
    import asyncio
    import json
    import mcp_runner
    import skills

    skill = skills.SkillRegistry().get("author")
    real = "art:0123456789abcdef"

    async def fake_reply(**kw):
        cb = kw.get("on_outcome")
        if cb:
            # What mcp_runner passes: name, args, ok, result text, latency.
            cb("render_document", {"format": "pdf"}, True,
               json.dumps({"ok": True, "artifact": real,
                           "filename": "real.pdf", "format": "pdf",
                           "bytes": 1234,
                           "stats": {"words": 2700, "sections": 5,
                                     "format": "pdf"}}),
               0.5)
        return {"text": '{"artifact": "art:i-made-this-up", '
                       '"filename": "made-up.pdf"}',
                "provider": "test", "tool_calls": []}

    original = mcp_runner.run_with_tools
    mcp_runner.run_with_tools = fake_reply
    nodes = {"n:1": {"skill": "author", "inputs": {}, "status": "running"}}
    try:
        result, _rendered = asyncio.run(
            skills.run_skill(skill, "n:1", nodes, "s8-test", "make a pdf",
                             None))
    finally:
        mcp_runner.run_with_tools = original

    assert result.success is True, result.error
    assert result.output["artifact"] == real, \
        "the model's invented handle survived"
    assert result.output["produced"][0]["artifact"] == real
    # And the delivered stats ride along for the receipt.
    assert result.output["produced"][0]["stats"]["words"] == 2700