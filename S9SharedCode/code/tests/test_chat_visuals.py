"""The chat stream must report what the turn actually produced, and must not
lose the user's question when the turn is interrupted.

The Chat view showed nothing visual because the `done` frame carried no file
list: the DAG path sends `files` from a `produced_files.json` sidecar, and chat
has no sidecar, so the view was left scraping `art:` handles out of the answer
prose. That works only when the model volunteers a handle, and it can never
show a chart, because a chart lives inside the document rather than in the
reply.
"""

from __future__ import annotations

import asyncio
import json
import time

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


# ── an interrupted turn must not swallow the question ────────────────────────


def _stub_chat(monkeypatch, text="ok"):
    """Neutralise everything the lightweight turn reaches out to."""
    import gateway
    import mcp_runner

    class _LLM:
        def chat(self, **kwargs):
            return {"text": text}

    async def _run_with_tools(**kwargs):
        return {"text": text}

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
    monkeypatch.setattr(agent_server, "_capture_observation_bg", lambda *a, **k: None)

    async def _cost(cid, *a, **k):
        return None

    async def _delta(cid, before, *a, **k):
        return None

    monkeypatch.setattr(agent_server, "_acost_breakdown", _cost)
    monkeypatch.setattr(agent_server, "_session_cost_delta", _delta)


def _messages(cid):
    """Whatever the store holds for this conversation, however it is stored."""
    with agent_server._CHAT_THREADS_LOCK:
        return list((agent_server._chat_threads_load().get(cid, {}) or {}).get("messages")
                    or [])


def test_the_question_is_stored_before_the_answer_is_known(client, monkeypatch):
    """Walk away mid-turn ("Start new chat", a closed tab) and the user's own
    message must still be there.

    The turn used to be written in one block AFTER the answer finished
    streaming, so a client that disconnected lost both halves: the user had
    paid for the tokens and came back to an empty thread with nothing in the UI
    saying the turn had ever existed. The question half is now written up
    front, which is what this asserts - by abandoning the stream after the
    first frame and then reading the store.
    """
    _stub_chat(monkeypatch)
    cid = "ct-interrupted-test"
    with client.stream("POST", "/api/chat/simple/stream", headers=HDR,
                      json={"conversation_id": cid, "query": "will you survive?"}) as r:
        assert r.status_code == 200, r.read().decode("utf-8", "replace")[:200]
        # Consume only the first frame, then hang up mid-answer.
        for _line in r.iter_lines():
            if _line:
                break
    msgs = _messages(cid)
    assert any(m.get("role") == "user" and m.get("content") == "will you survive?"
               for m in msgs), (
        f"the interrupted turn lost the user's message; stored={msgs!r}")


def test_a_disconnected_stream_still_persists_the_answer(monkeypatch):
    """Closing the SSE response must detach a viewer, not cancel the turn.

    The old generator owned the model call, so this exact action - refresh,
    navigate away, close the tab - cancelled it before the answer was stored.
    The run now lives in a server-owned task; only /api/chat/cancel stops it.
    """
    _stub_chat(monkeypatch, "the answer survives the refresh")

    async def _slow_mem(messages, query, *a, **k):
        await asyncio.sleep(0.4)
        return messages

    monkeypatch.setattr(agent_server, "_with_chat_memory", _slow_mem)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown", lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_session_cost_delta", lambda *a, **k: None)

    cid = "ct-disconnect-answer"
    with TestClient(agent_server.app) as tc:
        with tc.stream("POST", "/api/chat/simple/stream", headers=HDR,
                       json={"conversation_id": cid,
                             "query": "keep going after I leave"}) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line:
                    break
        # The response is closed. Give the detached run time to finish and
        # read the store directly, the way a reloaded Chat view would.
        deadline = time.time() + 4
        while time.time() < deadline:
            msgs = _messages(cid)
            if any(m.get("role") == "assistant" and not m.get("pending")
                   for m in msgs):
                break
            time.sleep(0.1)

    msgs = _messages(cid)
    answer = [m for m in msgs
              if m.get("role") == "assistant" and not m.get("pending")]
    assert answer, f"the detached turn never stored an answer; stored={msgs!r}"
    assert answer[-1].get("content") == "the answer survives the refresh"


def test_cancel_route_reaches_the_lightweight_run_registry():
    """Stop must cancel the asyncio task, not only the DAG threading.Event."""
    cancelled = []

    class _Task:
        def done(self):
            return False

        def cancel(self):
            cancelled.append(True)

    cid = "ct-cancel-registry"
    with agent_server._CHAT_RUNS_LOCK:
        old = agent_server._CHAT_RUNS.get(cid)
        agent_server._CHAT_RUNS[cid] = {"task": _Task(), "turn_id": "turn-x"}
    try:
        with TestClient(agent_server.app) as tc:
            r = tc.post("/api/chat/cancel", headers=HDR,
                        json={"conversation_id": cid})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "cancelled", body
        assert body["lightweight_cancelled"] == 1, body
        assert cancelled == [True]
    finally:
        with agent_server._CHAT_RUNS_LOCK:
            if old is None:
                agent_server._CHAT_RUNS.pop(cid, None)
            else:
                agent_server._CHAT_RUNS[cid] = old


def test_a_completed_turn_stores_the_question_exactly_once(client, monkeypatch):
    """The question is written up front and the answer at the end, so the
    question must not also be appended again at the end - or every reopened
    transcript would show it twice."""
    _stub_chat(monkeypatch)
    cid = "ct-once-test"
    r = client.post("/api/chat/simple/stream", headers=HDR,
                    json={"conversation_id": cid, "query": "how many times?"})
    assert r.status_code == 200, r.text
    r.read()
    users = [m for m in _messages(cid) if m.get("role") == "user"]
    assert len(users) == 1, f"the question was stored {len(users)} times"
    assert users[0].get("content") == "how many times?"