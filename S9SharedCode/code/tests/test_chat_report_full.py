"""A delegated research report reaches the user VERBATIM, and later turns get
only its name and description.

Observed live (chat ct-eb790492): Research returned a full report, and the same
delegation from chat answered "Based on my research..." - the model's summary of
the report it had been fed as tool output. Two things were wrong, and both had
the same cause (the report travelled through the model):

  * the user asked for the report, not a paraphrase of it, and
  * every later turn ("send it to my telegram") re-sent the whole 20k characters
    to the model, which is the opposite of what the lightweight surface is for.

Now the full text goes straight to the CALLER (`answer_full` is popped before
the tool output is serialised), the endpoint shows it as the answer, and the
history the model receives carries a name-plus-opening stub instead of the body.
"""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import agent_server

HDR = {"X-Aria-Token": "test-token"}

_REPORT = ("# EV market report\n\n"
           + "Electric vehicles are taking over the market. " * 900).strip()


@pytest.fixture()
def client():
    return TestClient(agent_server.app)


class _FakeStream:
    """The minimum `_run_handoff` consumes: a body_iterator of SSE bytes."""

    def __init__(self, frames):
        self._frames = frames

    @property
    def body_iterator(self):
        async def _it():
            for f in self._frames:
                yield ("data: " + json.dumps(f) + "\n\n").encode("utf-8")
        return _it()


# ── the dispatcher: full text to the caller, trimmed text to the model ───────


def test_a_report_reaches_the_caller_verbatim_and_never_the_model(monkeypatch):
    """`answer_full` is for the endpoint; the JSON the model reads must not
    contain it, or the model summarises the very text the user asked to see
    unsummarised (and the next turn re-sends it)."""
    monkeypatch.setattr(agent_server, "_stream_run", lambda *a, **k: _FakeStream(
        [{"type": "done", "answer": _REPORT, "files": []}]))
    files_sink: list = []
    report_sink: dict = {}
    d = agent_server._chat_handoff_dispatch("ct-report-1", files_sink,
                                            report_sink)
    out = asyncio.run(d("delegate_research", {"topic": "EV market"}))

    payload = json.loads(out)
    assert payload["ok"] is True
    assert "answer_full" not in payload, (
        "the full report is in the tool output the model reads")
    assert len(payload["answer"]) < len(_REPORT), (
        "the model got the whole report instead of the trimmed answer")
    assert report_sink["text"] == _REPORT.strip(), (
        "the caller did not receive the report verbatim")
    assert report_sink["topic"] == "EV market"


def test_a_failed_run_leaves_the_report_sink_empty(monkeypatch):
    monkeypatch.setattr(agent_server, "_stream_run", lambda *a, **k: _FakeStream(
        [{"type": "error", "text": "planner died"}]))
    report_sink: dict = {}
    d = agent_server._chat_handoff_dispatch("ct-report-2", None, report_sink)
    out = asyncio.run(d("delegate_research", {"topic": "x"}))
    assert json.loads(out)["ok"] is False
    assert not report_sink


def test_the_report_tools_are_offered_to_chat():
    names = {t["name"] for t in agent_server._chat_tool_payload() or []}
    assert "send_channel_message" in names
    assert "save_to_documents" in names


def test_the_dispatcher_claims_the_report_tools():
    """They are chat-local: if the dispatcher returned None the names would go
    down the MCP path, where they do not exist."""
    d = agent_server._chat_handoff_dispatch("ct-report-3")
    for name in ("send_channel_message", "save_to_documents"):
        got = asyncio.run(d(name, {"channel": "pigeon"}))
        assert got is not None, f"{name} was not claimed by the dispatcher"


# ── what later turns see: name and description, never the body ───────────────


def test_later_turns_get_the_report_as_name_and_description():
    history = [
        {"role": "user", "content": "research EVs"},
        {"role": "assistant", "content": _REPORT, "kind": "report",
         "title": "EV market"},
        {"role": "user", "content": "send it to my telegram"},
    ]
    out = agent_server._chat_history_for_model(history)
    body = out[1]["content"]
    assert "EV market" in body
    assert "IN FULL" in body
    assert 'source="EV market"' in body, (
        "the model is not told how to fetch the report by name")
    assert len(body) < 900, "the whole report is still in the model's context"
    assert "taking over the market" in body, "the opening lines are missing"
    assert out[0] == {"role": "user", "content": "research EVs"}
    assert out[2]["content"] == "send it to my telegram"


def test_file_notes_are_names_and_handles_never_bytes():
    history = [{"role": "assistant", "content": "done",
                "files": [{"artifact": "art:" + "a" * 16,
                           "filename": "report.pdf"}]}]
    out = agent_server._chat_history_for_model(history)
    assert 'report.pdf' in out[0]["content"]
    assert "art:" + "a" * 16 in out[0]["content"]


def test_resolve_report_defaults_to_the_newest(monkeypatch):
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": "old text", "kind": "report",
         "title": "Solar"},
        {"role": "assistant", "content": "new text", "kind": "report",
         "title": "EV market"},
    ])
    assert agent_server._chat_resolve_report("ct-x", "") == ("EV market", "new text")
    assert agent_server._chat_resolve_report("ct-x", "it") == ("EV market", "new text")
    assert agent_server._chat_resolve_report("ct-x", "solar") == ("Solar", "old text")
    assert agent_server._chat_resolve_report("ct-x", "nope") == ("", "")


def test_resolve_file_needs_a_name(monkeypatch):
    """Guessing which document the user meant is how the wrong one is saved."""
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": "x",
         "files": [{"artifact": "art:" + "b" * 16, "filename": "memo.pdf"}]},
    ])
    assert agent_server._chat_resolve_file("ct-x", "") is None
    assert agent_server._chat_resolve_file("ct-x", "memo.pdf")["filename"] == "memo.pdf"
    assert agent_server._chat_resolve_file("ct-x", "art:" + "b" * 16)["filename"] == "memo.pdf"
    assert agent_server._chat_resolve_file("ct-x", "memo")["filename"] == "memo.pdf"
    assert agent_server._chat_resolve_file("ct-x", "other") is None


# ── read_report: summarising needs the body, so fetch it on demand ───────────

def test_read_report_returns_the_whole_body(monkeypatch):
    """Live: "can you summarise it" got "the report is already above" four
    times, because the stub left the model with 240 characters and no way to
    read the rest. The tool is that way, and it returns the report verbatim."""
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": _REPORT, "kind": "report",
         "title": "EV market"}])
    out = json.loads(agent_server._chat_read_report_tool(
        {"source": "EV market"}, "ct-read-1"))
    assert out["ok"] is True
    assert out["title"] == "EV market"
    assert out["text"] == _REPORT
    assert out["chars"] == len(_REPORT)


def test_read_report_defaults_to_the_newest_report(monkeypatch):
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": "old", "kind": "report",
         "title": "Solar"},
        {"role": "assistant", "content": _REPORT, "kind": "report",
         "title": "EV market"}])
    out = json.loads(agent_server._chat_read_report_tool({}, "ct-read-2"))
    assert out["ok"] is True
    assert out["title"] == "EV market"


def test_read_report_names_one_it_cannot_find(monkeypatch):
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": _REPORT, "kind": "report",
         "title": "EV market"}])
    out = json.loads(agent_server._chat_read_report_tool(
        {"source": "Solar"}, "ct-read-3"))
    assert out["ok"] is False
    assert "no research report" in out["message"]


def test_the_stub_tells_the_model_to_read_before_summarising(monkeypatch):
    history = [{"role": "assistant", "content": _REPORT, "kind": "report",
                "title": "EV market"}]
    body = agent_server._chat_history_for_model(history)[0]["content"]
    assert "read_report" in body
    assert "summarise" in body.lower(), (
        "a follow-up asking for a summary must not be told to re-read the "
        "transcript it cannot see")


def test_read_report_is_offered_to_chat():
    names = [t.get("name") for t in (agent_server._chat_tool_payload() or [])]
    assert "read_report" in names


# ── chunking: every character survives, nothing lands mid-sentence ───────────


def test_channel_chunks_keep_every_character_and_respect_the_limit():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 400 for i in range(10))
    chunks = agent_server._channel_chunks(text)
    assert all(0 < len(c) <= agent_server._CHAT_CHANNEL_CHUNK for c in chunks)
    assert len(chunks) > 1
    # Each chunk is a separate message, so whitespace at a boundary is dropped;
    # no WORD may be lost (the earlier `"".join` sloppiness in this assertion
    # jammed "word" and "Paragraph" together across a boundary and blamed the
    # splitter).
    assert (" ".join(" ".join(c.split()) for c in chunks)
            == " ".join(text.split())), "chunking lost characters"
    # Every cut is at whitespace: no word is split across two messages.
    pos = 0
    for c in chunks:
        i = text.index(c, pos)
        pos = i + len(c)
        assert pos == len(text) or text[pos].isspace(), (
            f"a cut landed inside a word: {text[pos-20:pos+10]!r}")


def test_an_unbroken_string_is_split_without_loss():
    chunks = agent_server._channel_chunks("x" * 10000)
    assert all(len(c) <= agent_server._CHAT_CHANNEL_CHUNK for c in chunks)
    assert "".join(chunks) == "x" * 10000


def test_a_short_message_is_one_chunk():
    assert agent_server._channel_chunks("hello") == ["hello"]


# ── send_channel_message: now, not in a minute ───────────────────────────────


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._p is None:
            raise ValueError("not json")
        return self._p


class _Recorder:
    """Stands in for httpx.AsyncClient; keeps every post for assertions."""

    posts: list = []

    def __init__(self, *a, **k):
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, files=None):  # noqa: A002
        _Recorder.posts.append({"url": url, "json": json, "files": files})
        if url.endswith("/v1/documents"):
            return _Resp(200, {"ok": True, "document": {"id": "doc-77"}})
        return _Resp(200, {"ok": True})


@pytest.fixture()
def wired(monkeypatch):
    _Recorder.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda: {"x-gateway-token": "t"})
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda ch: "555")
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": _REPORT, "kind": "report",
         "title": "EV market"},
    ])
    return _Recorder


def test_send_channel_message_sends_the_whole_report_now(wired):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "source": "EV market"}, "ct-send-1"))
    payload = json.loads(out)
    assert payload["ok"] is True, out
    assert payload["sent"] == len(_Recorder.posts) > 1, (
        "a 40k-char report must arrive in chunks, not truncated")
    assert payload["chars"] == len(_REPORT)
    assert all(p["url"].endswith("/v1/channels/telegram/send")
               for p in _Recorder.posts)
    assert all(p["json"]["to"] == "555" for p in _Recorder.posts)
    rejoined = " ".join(
        " ".join(p["json"]["text"].split()) for p in _Recorder.posts)
    assert rejoined == " ".join(_REPORT.split())


def test_send_channel_message_says_so_without_a_paired_destination(
        wired, monkeypatch):
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda ch: "")
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "message": "hi"}, "ct-send-2"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no telegram destination is paired" in payload["message"]
    assert _Recorder.posts == []


def test_send_channel_message_refuses_an_unknown_channel(wired):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "pigeon", "message": "hi"}, "ct-send-3"))
    assert json.loads(out)["ok"] is False
    assert _Recorder.posts == []


def test_send_channel_message_names_a_source_it_cannot_find(wired):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "source": "Solar panels"}, "ct-send-3b"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert 'no report matching "Solar panels"' in payload["message"]
    assert _Recorder.posts == []


def test_send_channel_message_reports_a_pending_approval(wired, monkeypatch):
    class _Pending(_Recorder):
        async def post(self, url, json=None, files=None):  # noqa: A002
            return _Resp(200, {"ok": False, "status": "pending"})

    monkeypatch.setattr(httpx, "AsyncClient", _Pending)
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "message": "hi"}, "ct-send-4"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "approval" in payload["message"]


# ── send_channel_message: the file itself, not an apology ────────────────────


@pytest.fixture()
def wired_file(wired, monkeypatch):
    import artifacts

    monkeypatch.setattr(artifacts, "get_bytes", lambda art: b"%PDF-1.4 evs")
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": "the report",
         "files": [{"artifact": "art:" + "e" * 16,
                    "filename": "evs.pdf", "format": "pdf"}]},
    ])
    return wired


def test_send_channel_message_attaches_the_rendered_file(wired_file):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "file": "evs.pdf",
         "message": "Here is the EV report"}, "ct-send-5"))
    payload = json.loads(out)
    assert payload["ok"] is True, out
    assert payload["file"] == "evs.pdf"
    assert payload["bytes"] == len(b"%PDF-1.4 evs")
    assert len(_Recorder.posts) == 1
    post = _Recorder.posts[0]
    assert post["url"].endswith("/v1/channels/telegram/send")
    assert post["json"]["to"] == "555"
    media = post["json"]["media"]
    assert len(media) == 1, "exactly one attachment per file send"
    assert media[0]["filename"] == "evs.pdf"
    assert base64.b64decode(media[0]["data_b64"]) == b"%PDF-1.4 evs"
    assert media[0]["caption"] == "Here is the EV report"


def test_send_channel_message_file_without_message_still_attaches(wired_file):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "file": "evs"}, "ct-send-6"))
    payload = json.loads(out)
    assert payload["ok"] is True, out
    assert _Recorder.posts[0]["json"]["media"][0]["caption"] == ""


def test_send_channel_message_file_needs_an_exact_enough_name(wired_file):
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "file": "nope.pdf"}, "ct-send-7"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no rendered file matching" in payload["message"]
    assert _Recorder.posts == []


def test_send_channel_message_file_says_so_without_a_destination(
        wired_file, monkeypatch):
    monkeypatch.setattr(agent_server, "resolve_notify_chat", lambda ch: "")
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "file": "evs.pdf"}, "ct-send-8"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no telegram destination is paired" in payload["message"]
    assert _Recorder.posts == []


def test_send_channel_message_file_beats_a_stale_source(wired_file):
    """With a file, `message` is the caption; a stale `source` must not turn
    the attachment back into a wall of chunked text."""
    out = asyncio.run(agent_server._chat_send_channel_tool(
        {"channel": "telegram", "file": "evs.pdf", "source": "EV market",
         "message": "here"}, "ct-send-9"))
    assert json.loads(out)["ok"] is True
    posts = _Recorder.posts
    assert len(posts) == 1, "a file send must not chunk the report too"
    assert posts[0]["json"]["media"][0]["filename"] == "evs.pdf"


# ── save_to_documents: same bytes, same pipeline as an upload ────────────────


@pytest.fixture()
def wired_save(monkeypatch):
    import artifacts

    _Recorder.posts = []
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    monkeypatch.setattr(agent_server, "_gw_auth_headers",
                        lambda: {"x-gateway-token": "t"})
    monkeypatch.setattr(artifacts, "get_meta", lambda art: type("A", (), {
        "descriptor": "EV market (pdf)", "content_type": "application/pdf"})())
    monkeypatch.setattr(artifacts, "get_bytes", lambda art: b"%PDF-1.4 fake")
    monkeypatch.setattr(agent_server, "_chat_thread_messages", lambda cid: [
        {"role": "assistant", "content": "here",
         "files": [{"artifact": "art:" + "c" * 16,
                    "filename": "EV-market.pdf", "format": "pdf"}]},
    ])
    return _Recorder


def test_save_to_documents_copies_bytes_into_the_store(wired_save):
    out = asyncio.run(agent_server._chat_save_documents_tool(
        {"file": "EV-market.pdf"}, "ct-save-1"))
    payload = json.loads(out)
    assert payload["ok"] is True, out
    assert payload["doc_id"] == "doc-77"
    assert len(_Recorder.posts) == 1
    form = _Recorder.posts[0]["files"]["file"]
    assert form[0] == "EV-market.pdf"
    assert form[1] == b"%PDF-1.4 fake"
    assert form[2] == "application/pdf"
    assert _Recorder.posts[0]["url"].endswith("/v1/documents")


def test_save_to_documents_needs_an_exact_enough_name(wired_save):
    out = asyncio.run(agent_server._chat_save_documents_tool(
        {"file": "nope.pdf"}, "ct-save-2"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "no rendered file matching" in payload["message"]
    assert _Recorder.posts == []


def test_save_to_documents_reports_a_store_refusal(wired_save, monkeypatch):
    class _Refusing(_Recorder):
        async def post(self, url, json=None, files=None):  # noqa: A002
            return _Resp(400, {"error": "unsupported format"})

    monkeypatch.setattr(httpx, "AsyncClient", _Refusing)
    out = asyncio.run(agent_server._chat_save_documents_tool(
        {"file": "EV-market.pdf"}, "ct-save-3"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "unsupported format" in payload["message"]


# ── end to end through the endpoints ─────────────────────────────────────────


def _stub(monkeypatch, calls: dict):
    """Neutralise the model and the run; record what the tool loop was handed."""
    import gateway
    import mcp_runner

    async def _run_handoff(brief, conversation_id, idem_key, **kwargs):
        return {"ok": True, "status": "done", "answer": _REPORT[:4000],
                "files": [], "say": "done", "truncated": True,
                "answer_full": _REPORT}

    async def _run_with_tools(**kwargs):
        disp = kwargs.get("extra_dispatch")
        if disp:
            calls["tool_out"] = await disp("delegate_research",
                                           {"topic": "EV market"})
        return {"text": "The research finished."}

    class _LLM:
        def chat(self, **kwargs):
            return {"text": "The research finished."}

    async def _doc_context(messages, query, *a, **k):
        calls["doc_context"] = True
        return messages, []

    async def _mem(messages, query, *a, **k):
        return messages

    async def _cost(cid, *a, **k):
        return None

    async def _delta(cid, before, *a, **k):
        return None

    monkeypatch.setattr(agent_server, "_run_handoff", _run_handoff)
    monkeypatch.setattr(agent_server, "_with_doc_context", _doc_context)
    monkeypatch.setattr(agent_server, "_with_chat_memory", _mem)
    monkeypatch.setattr(agent_server, "_capture_preference_bg", lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_capture_observation_bg", lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "_acost_breakdown", _cost)
    monkeypatch.setattr(agent_server, "_session_cost_delta", _delta)
    monkeypatch.setattr(gateway, "LLM", _LLM)
    monkeypatch.setattr(mcp_runner, "run_with_tools", _run_with_tools)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)


def test_one_shot_chat_answers_with_the_report_verbatim(client, monkeypatch):
    calls: dict = {}
    _stub(monkeypatch, calls)
    r = client.post("/api/chat/simple", headers=HDR,
                    json={"conversation_id": "ct-e2e-report-1",
                          "query": "research EVs for me"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == _REPORT, (
        "the one-shot answer is not the research agent's report")
    assert body["kind"] == "report"
    assert body["title"] == "EV market"
    assert "doc_context" not in calls, (
        "the lightweight turn is searching documents up front again")
    assert _REPORT not in (calls.get("tool_out") or ""), (
        "the model was handed the full report as tool output")


def test_streaming_chat_opens_with_the_report_card_not_a_paraphrase(
        client, monkeypatch):
    calls: dict = {}
    _stub(monkeypatch, calls)
    with client.stream("POST", "/api/chat/simple/stream", headers=HDR,
                       json={"conversation_id": "ct-e2e-report-2",
                             "query": "research EVs for me"}) as resp:
        assert resp.status_code == 200
        frames = [json.loads(ln[5:].strip())
                  for ln in resp.iter_lines() if ln.startswith("data:")]
    types = [f.get("type") for f in frames]
    assert "delta" not in types, (
        "a 20k report must not be dribbled out word by word")
    assert any(f.get("type") == "status" and "showing it in full" in f.get("text", "")
               for f in frames)
    done = next((f for f in frames if f.get("type") == "done"), None)
    assert done is not None, frames
    assert done["answer"] == _REPORT
    assert done.get("kind") == "report"
    assert done.get("title") == "EV market"

    with agent_server._CHAT_THREADS_LOCK:
        msgs = (agent_server._chat_threads_load()
                .get("ct-e2e-report-2", {}).get("messages") or [])
    rep = next((m for m in msgs if m.get("kind") == "report"), None)
    assert rep is not None, f"the report was not stored: {msgs!r}"
    assert rep["content"] == _REPORT
    assert rep["title"] == "EV market"
