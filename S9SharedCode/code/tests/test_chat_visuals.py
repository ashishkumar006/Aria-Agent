"""The chat stream must report what the turn actually produced.

The Chat view showed nothing visual because the `done` frame carried no file
list: the DAG path sends `files` from a `produced_files.json` sidecar, and chat
has no sidecar, so the view was left scraping `art:` handles out of the answer
prose. That works only when the model volunteers a handle, and it can never
show a chart, because a chart lives inside the document rather than in the
reply.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import agent_server

HDR = {"X-Aria-Token": "test-token"}


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


def test_a_rendered_document_in_the_answer_is_reported(monkeypatch, tmp_path):
    import artifacts

    store = tmp_path / "artifacts"
    store.mkdir()
    monkeypatch.setattr(artifacts, "STORE", store)
    monkeypatch.setattr(artifacts, "get_meta",
                        lambda art: type("A", (), {
                            "descriptor": "Revenue chart (pdf)",
                            "content_type": "application/pdf"})())

    got = agent_server._chat_produced_files(
        "ct-abc", "Here it is: art:0123456789abcdef")

    assert len(got) == 1
    assert got[0]["artifact"] == "art:0123456789abcdef"
    assert got[0]["filename"] == "Revenue chart (pdf)"
    assert got[0]["format"] == "pdf"


def test_an_answer_with_no_artifact_reports_nothing(monkeypatch):
    assert agent_server._chat_produced_files("ct-abc", "just prose") == []


def test_a_handle_the_store_cannot_resolve_is_omitted(monkeypatch):
    """An unresolvable handle becomes a download button that fails with an
    opaque 400 — worse than showing nothing, and indistinguishable from the
    genuine files beside it."""
    import artifacts

    def _missing(art):
        raise FileNotFoundError(art)

    monkeypatch.setattr(artifacts, "get_meta", _missing)

    assert agent_server._chat_produced_files("ct-abc", "art:0123456789abcdef") == []


def test_a_traversal_shaped_conversation_id_is_refused(monkeypatch):
    """The id becomes a lookup key. It is not path-joined here, but the guard
    is the boundary and it must not be dropped."""
    assert agent_server._chat_produced_files("../../etc", "art:0123456789abcdef") == []
    assert agent_server._chat_produced_files("", "art:0123456789abcdef") == []


def test_duplicate_handles_are_reported_once(monkeypatch):
    import artifacts

    monkeypatch.setattr(artifacts, "get_meta", lambda art: type("A", (), {
        "descriptor": "d", "content_type": "application/pdf"})())

    got = agent_server._chat_produced_files(
        "ct-abc", "art:0123456789abcdef and again art:0123456789abcdef")

    assert len(got) == 1


def test_the_stream_done_frame_carries_files(client, monkeypatch):
    """End to end through the endpoint: the frame the console reads."""
    import artifacts
    import gateway
    import mcp_runner

    class _LLM:
        def chat(self, **kwargs):
            return {"text": "Here you go: art:0123456789abcdef"}

    async def _run_with_tools(**kwargs):
        return {"text": "Here you go: art:0123456789abcdef"}

    async def _msgs(messages, query, *a, **k):
        return messages, []

    async def _mem(messages, query, *a, **k):
        return messages

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(mcp_runner, "run_with_tools", _run_with_tools)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_with_doc_context", _msgs)
    monkeypatch.setattr(agent_server, "_with_chat_memory", _mem)
    monkeypatch.setattr(agent_server, "_capture_preference_bg", lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(artifacts, "get_meta", lambda art: type("A", (), {
        "descriptor": "Report (pdf)", "content_type": "application/pdf"})())

    r = client.post("/api/chat/simple", headers=HDR,
                    json={"conversation_id": "ct-files-test",
                          "query": "make me a report"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("files"), "the one-shot chat response carries no file list"


def test_the_streaming_done_frame_carries_files(client, monkeypatch):
    import artifacts
    import gateway
    import mcp_runner

    class _LLM:
        def chat(self, **kwargs):
            return {"text": "Here you go: art:0123456789abcdef"}

    async def _run_with_tools(**kwargs):
        return {"text": "Here you go: art:0123456789abcdef"}

    async def _msgs(messages, query, *a, **k):
        return messages, []

    async def _mem(messages, query, *a, **k):
        return messages

    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(mcp_runner, "run_with_tools", _run_with_tools)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_with_doc_context", _msgs)
    monkeypatch.setattr(agent_server, "_with_chat_memory", _mem)
    monkeypatch.setattr(agent_server, "_capture_preference_bg", lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(artifacts, "get_meta", lambda art: type("A", (), {
        "descriptor": "Report (pdf)", "content_type": "application/pdf"})())

    with client.stream("POST", "/api/chat/simple/stream", headers=HDR,
                       json={"conversation_id": "ct-stream-files",
                             "query": "make me a report"}) as resp:
        assert resp.status_code == 200, resp.text
        frames = [json.loads(ln[5:].strip())
                  for ln in resp.iter_lines() if ln.startswith("data:")]

    done = next((f for f in frames if f.get("type") == "done"), None)
    assert done is not None, frames
    assert done.get("files"), (
        "the streaming done frame carries no file list, so the Chat view can "
        "never render a document the model produced")
    assert done["files"][0]["artifact"] == "art:0123456789abcdef"


def test_the_render_document_description_advertises_charts():
    """The schema accepts `chart`, `image` and `cover`; the description used to
    list only the plain block types, so the model was told charts do not
    exist while the renderer could draw them."""
    from skills import _TOOL_CATALOG

    desc = _TOOL_CATALOG["render_document"]["description"]
    for block in ("chart", "image", "cover"):
        assert block in desc, (
            f"render_document accepts a {block!r} block but does not tell the "
            f"model it exists")


def test_every_block_type_in_the_schema_is_documented():
    """The enum is the contract. A type that is only in the enum is invisible."""
    from skills import _TOOL_CATALOG

    schema = _TOOL_CATALOG["render_document"]["input_schema"]
    block_enum = schema["properties"]["blocks"]["items"]["properties"]["type"]["enum"]
    desc = _TOOL_CATALOG["render_document"]["description"]
    undocumented = [b for b in block_enum if b not in desc]
    assert not undocumented, (
        f"block types accepted but not documented: {undocumented}")