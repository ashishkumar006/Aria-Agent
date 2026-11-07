"""Per-conversation document access must actually gate retrieval.

The console has a `docs on` / `docs off` toggle per chat thread. It writes
`use_documents` into the conversation preferences and renders the resulting
state, so it looked wired. It was not: `_conversation_doc_ids()` accepted a
`conversation_id` and then ignored it, returning every enabled document in the
registry regardless of the toggle. Turning docs off therefore changed the label
on the button and nothing else — the run still recalled from all documents.

These tests pin the three states the toggle has to distinguish:

  * unset / docs on  -> the enabled documents
  * docs off        -> no documents at all (an empty set, not "no filtering")
  * registry down   -> no documents (fail closed)

`None` is deliberately NOT acceptable for "docs off": the consumer treats
`None` as "do not filter", which is exactly the bug.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_server  # noqa: E402


CONV = "conv-docs-toggle-probe"
DOCS = {"documents": [
    {"id": "doc-a", "enabled": True, "status": "ready"},
    {"id": "doc-b", "enabled": True, "status": "ready"},
    {"id": "doc-off", "enabled": False, "status": "ready"},
    {"id": "doc-busy", "enabled": True, "status": "embedding"},
]}


def _stub_registry(monkeypatch, payload=None, boom=False):
    """Stub at the gateway HTTP seam so the real `_gw_enabled_doc_ids` filter
    runs. Patching `_gw_enabled_doc_ids` itself would bypass the filter and
    make these tests pass for the wrong reason.

    The seam is `gateway.get_json`, not `memory._get`. The registry read moved
    because it sits on a run's critical path and `memory._get` carries a 30s
    timeout with an `ensure_gateway()` probe in front of it — a loaded gateway
    could hold a turn's start, and every later turn in that conversation behind
    its session lock, for half a minute. `get_json` is fail-soft, so an
    unreachable registry answers None rather than raising; the scope function
    fails closed either way, which is what `boom=True` pins here.
    """
    import gateway as gw
    if boom:
        monkeypatch.setattr(gw, "get_json", lambda *a, **k: None)
        return
    body = payload if payload is not None else DOCS
    monkeypatch.setattr(gw, "get_json", lambda *a, **k: body)


def _reset_prefs(conversation_id: str) -> None:
    """Drop a stored preference so a test starts from the untouched default."""
    agent_server._chat_prefs(conversation_id, use_documents=True)


def test_enabled_registry_excludes_disabled_and_unfinished_documents(monkeypatch):
    _stub_registry(monkeypatch)
    assert agent_server._gw_enabled_doc_ids() == {"doc-a", "doc-b"}


def test_docs_on_exposes_only_enabled_ready_documents(monkeypatch):
    _stub_registry(monkeypatch)
    _reset_prefs(CONV)
    assert agent_server._conversation_doc_ids(CONV) == {"doc-a", "doc-b"}


def test_default_preference_is_docs_on(monkeypatch):
    """A thread that never touched the toggle must still get documents."""
    _stub_registry(monkeypatch)
    fresh = "conv-docs-never-toggled"
    with agent_server._CHAT_THREADS_LOCK:
        data = agent_server._chat_threads_load()
        if fresh in data:
            data[fresh].pop("prefs", None)
            agent_server._chat_threads_save(data)
    assert agent_server._conversation_doc_ids(fresh) == {"doc-a", "doc-b"}


def test_docs_off_exposes_nothing(monkeypatch):
    """The regression: docs off used to return every enabled document."""
    _stub_registry(monkeypatch)
    agent_server._chat_prefs(CONV, use_documents=False)
    got = agent_server._conversation_doc_ids(CONV)
    assert got == set(), f"docs off still exposes documents: {sorted(got or [])}"
    assert got is not None, (
        "docs off must return an empty set, not None: None means "
        "'do not filter', which re-exposes every document"
    )


def test_docs_off_survives_an_unreachable_registry(monkeypatch):
    """Docs off must not need the registry at all - fail closed either way."""
    _stub_registry(monkeypatch, boom=True)
    agent_server._chat_prefs(CONV, use_documents=False)
    assert agent_server._conversation_doc_ids(CONV) == set()


def test_docs_on_fails_closed_when_registry_is_unreachable(monkeypatch):
    _stub_registry(monkeypatch, boom=True)
    agent_server._chat_prefs(CONV, use_documents=True)
    assert agent_server._conversation_doc_ids(CONV) == set()


def test_toggle_is_scoped_to_its_own_conversation(monkeypatch):
    """One thread's docs-off must not silence another thread."""
    _stub_registry(monkeypatch)
    agent_server._chat_prefs("conv-a", use_documents=False)
    agent_server._chat_prefs("conv-b", use_documents=True)
    assert agent_server._conversation_doc_ids("conv-a") == set()
    assert agent_server._conversation_doc_ids("conv-b") == {"doc-a", "doc-b"}
    _reset_prefs(CONV)
    _reset_prefs("conv-a")
    _reset_prefs("conv-b")


def test_memory_block_honours_docs_off(monkeypatch):
    """The uploaded-document chunk reached MEMORY HITS in a thread with
    documents switched OFF: `_chat_memory_block` read the GLOBAL enabled set,
    so the toggle governed retrieval tools but not the one block that is
    injected into every chat prompt."""
    import memory as mem

    seen = {}

    def _read(query, **kw):
        seen.update(kw)
        return [{"descriptor": "chunk from the uploaded document",
                 "kind": "document"}]

    monkeypatch.setattr(mem, "read", _read)
    block = agent_server._chat_memory_block("what does the doc say?", "s1",
                                            set())
    # Nothing is read from the document drawer at all - filtering after the
    # ranking would still spend a slot and a record without doc_id would pass.
    assert "document" not in (seen.get("drawers") or []), seen
    assert seen.get("doc_ids") == set(), seen
    assert "uploaded document" not in block


def test_memory_block_passes_the_conversations_own_document_set(monkeypatch):
    """Docs on: exactly this conversation's documents may answer, not every
    enabled document in the registry."""
    import memory as mem

    seen = {}

    def _read(query, **kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(mem, "read", _read)
    agent_server._chat_memory_block("q", "s1", {"doc-a"})
    assert seen.get("doc_ids") == {"doc-a"}, seen
    assert "document" in (seen.get("drawers") or []), seen


def test_preference_round_trips_through_the_http_endpoint():
    from fastapi.testclient import TestClient

    _reset_prefs(CONV)
    with TestClient(agent_server.app) as client:
        r = client.post(f"/api/chat/threads/{CONV}/prefs", json={"use_documents": False})
        assert r.status_code == 200, r.text
        assert r.json()["prefs"]["use_documents"] is False
        r = client.post(f"/api/chat/threads/{CONV}/prefs", json={"use_documents": True})
        assert r.json()["prefs"]["use_documents"] is True
        # A malformed toggle must be rejected rather than coerced.
        bad = client.post(f"/api/chat/threads/{CONV}/prefs", json={"use_documents": "maybe"})
        assert bad.status_code == 400, bad.text
    _reset_prefs(CONV)


# ── the toggle must reach the DAG/Research worker, not just the light chat ──
#
# `_stream_run` is a SEPARATE function from the /api/chat handler that owns the
# request body, so reading `body` inside it is a NameError - and it was:
# every research run died with "name 'body' is not defined" before the first
# node. The suite missed it because the route-level tests replace `_stream_run`
# with a fake. These drive the real one with the orchestrator stubbed out.

def _stream_once(monkeypatch, conversation_id: str, body):
    """Run `_stream_run` to completion, returning (frames, captured kwargs)."""
    import asyncio

    import gateway

    monkeypatch.setattr(agent_server, "_GATEWAY_WARMUP_THREAD", None)
    monkeypatch.setattr(agent_server, "_EMBEDDER_WARMUP_THREAD", None)
    monkeypatch.setattr(gateway, "ensure_gateway", lambda *a, **k: True)
    monkeypatch.setattr(agent_server, "_session_cost_breakdown",
                        lambda sid: {})
    monkeypatch.setattr(agent_server, "_session_cost_delta",
                        lambda sid, before: {})
    monkeypatch.setattr(agent_server, "_record_turn_cost",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent_server, "resolve_session",
                        lambda _cid: "s8-docs-stream")
    seen = {}

    def _run(*_a, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(agent_server, "_run_orchestrator", _run)

    resp = agent_server._stream_run("what does the doc say?", conversation_id,
                                    body=body)

    async def _go():
        return [f async for f in resp.body_iterator]

    return asyncio.run(_go()), seen


def test_docs_off_preference_reaches_the_orchestrator(monkeypatch):
    agent_server._chat_prefs("ct-docs-stream", use_documents=False)
    try:
        frames, seen = _stream_once(monkeypatch, "ct-docs-stream", None)
    finally:
        _reset_prefs("ct-docs-stream")
    assert seen.get("doc_ids") == set(), (
        "the stored docs-off toggle never reached the run: "
        f"{seen.get('doc_ids')!r}")
    assert not any('"error"' in str(f) for f in frames), frames


def test_per_turn_use_documents_false_reaches_the_orchestrator(monkeypatch):
    """A body-level OFF wins for THIS turn even when the stored pref is on."""
    agent_server._chat_prefs("ct-docs-stream-body", use_documents=True)
    try:
        frames, seen = _stream_once(monkeypatch, "ct-docs-stream-body",
                                    {"use_documents": False})
    finally:
        _reset_prefs("ct-docs-stream-body")
    assert seen.get("doc_ids") == set(), seen.get("doc_ids")
    assert not any('"error"' in str(f) for f in frames), frames


# ── single-document binding ("discuss this document") ───────────────────────
#
# A thread bound to one document must retrieve from THAT document only. Without
# a binding the only scope the console could express was a folder, so discussing
# a generated report dragged in every other enabled document.

def _clear_binding(conversation_id: str) -> None:
    agent_server._chat_prefs(conversation_id, doc_ids=[])


def test_bound_document_narrows_retrieval_to_itself(monkeypatch):
    _stub_registry(monkeypatch)
    agent_server._chat_prefs("conv-bound", use_documents=True, doc_ids=["doc-a"])
    try:
        assert agent_server._conversation_doc_ids("conv-bound") == {"doc-a"}
    finally:
        _clear_binding("conv-bound")


def test_per_turn_doc_ids_override_the_stored_binding(monkeypatch):
    """Same rule as `use_documents`: a body value is authoritative for this
    turn, so a client can bind a thread before it has an id to write prefs to."""
    _stub_registry(monkeypatch)
    agent_server._chat_prefs("conv-bound-turn", use_documents=True,
                             doc_ids=["doc-a"])
    try:
        got = agent_server._conversation_doc_ids(
            "conv-bound-turn", {"doc_ids": ["doc-b"]})
        assert got == {"doc-b"}, got
    finally:
        _clear_binding("conv-bound-turn")


def test_binding_an_off_or_indexing_document_yields_nothing(monkeypatch):
    """Binding is a scope narrowing, not an authorization: a document that is
    switched off or still indexing has no complete vectors and must not answer
    just because a client named it."""
    _stub_registry(monkeypatch)
    assert agent_server._gw_bound_doc_ids(["doc-off", "doc-busy"]) == set()
    assert agent_server._gw_bound_doc_ids(["doc-a", "doc-off"]) == {"doc-a"}


def test_docs_off_still_wins_over_a_binding(monkeypatch):
    """The conversation toggle is the outer switch. Turning documents off for a
    thread must silence it even when the thread carries a document binding."""
    _stub_registry(monkeypatch)
    agent_server._chat_prefs("conv-off-bound", use_documents=False,
                             doc_ids=["doc-a"])
    try:
        assert agent_server._conversation_doc_ids("conv-off-bound") == set()
    finally:
        agent_server._chat_prefs("conv-off-bound", use_documents=True)
        _clear_binding("conv-off-bound")


def test_binding_and_folder_scope_union(monkeypatch):
    """Both are explicit user choices about what may answer; naming a document
    must not silently delete the folder scope the thread already carries."""
    _stub_registry(monkeypatch, {"documents": [
        {"id": "doc-a", "enabled": True, "status": "ready", "folder_id": "f1"},
        {"id": "doc-b", "enabled": True, "status": "ready", "folder_id": "f2"},
    ], "folders": [{"id": "f1", "parent_id": ""}, {"id": "f2", "parent_id": ""}]})
    agent_server._chat_prefs("conv-both", use_documents=True,
                             doc_ids=["doc-b"], folder_ids=["f1"])
    try:
        assert agent_server._conversation_doc_ids("conv-both") == {"doc-a", "doc-b"}
    finally:
        agent_server._chat_prefs("conv-both", doc_ids=[], folder_ids=[])


def test_binding_round_trips_through_the_prefs_endpoint(monkeypatch):
    """The thread has to exist before preferences can be written to it."""
    from fastapi.testclient import TestClient

    cid = "conv-prefs-doc-binding"
    with agent_server._CHAT_THREADS_LOCK:
        data = agent_server._chat_threads_load()
        data.setdefault(cid, {"id": cid, "messages": []})
        agent_server._chat_threads_save(data)
    _clear_binding(cid)
    try:
        with TestClient(agent_server.app) as client:
            r = client.post(f"/api/chat/threads/{cid}/prefs",
                            json={"doc_ids": ["doc-a"]})
            assert r.status_code == 200, r.text
            assert r.json()["prefs"]["doc_ids"] == ["doc-a"]
            r = client.get(f"/api/chat/threads/{cid}/prefs")
            assert r.json()["prefs"]["doc_ids"] == ["doc-a"]
            bad = client.post(f"/api/chat/threads/{cid}/prefs",
                              json={"doc_ids": "doc-a"})
            assert bad.status_code == 400, bad.text
    finally:
        _clear_binding(cid)
        with agent_server._CHAT_THREADS_LOCK:
            data = agent_server._chat_threads_load()
            data.pop(cid, None)
            agent_server._chat_threads_save(data)


# ── the prompt note that names the bound document ───────────────────────────
#
# Light chat stopped retrieving up front and leaves `search_knowledge` to the
# model. For "discuss this document" that is wrong twice over: the model does
# not know WHICH document is on the user's screen, and it may answer from its
# own weights about a file it could read.

NAMED = {"documents": [
    {"id": "doc-a", "filename": "Q3 review.pdf", "enabled": True,
     "status": "ready"},
    {"id": "doc-b", "filename": "Bluebook.md", "enabled": True,
     "status": "ready"},
]}


def test_binding_note_names_the_document_and_orders_a_search(monkeypatch):
    _stub_registry(monkeypatch, NAMED)
    note = agent_server._doc_binding_note(["doc-a"])
    assert "Q3 review.pdf" in note, note
    assert "doc-a" in note, "the id is what lets the model cite the right file"
    assert "search_knowledge" in note
    assert "Do not substitute" in note


def test_no_binding_means_no_note():
    """An ordinary conversation must not gain a document instruction it never
    asked for — the note also narrows what the model believes it may answer."""
    assert agent_server._doc_binding_note([]) == ""
    assert agent_server._doc_binding_note(None) == ""


def test_the_scope_reports_which_documents_are_bound(monkeypatch):
    """One read answers both questions the light chat needs: what may be
    searched, and what to name in the prompt."""
    _stub_registry(monkeypatch, NAMED)
    agent_server._chat_prefs("conv-scope", use_documents=True,
                             doc_ids=["doc-a"])
    try:
        ids, bound = agent_server._conversation_doc_scope("conv-scope", {})
        assert ids == {"doc-a"}
        assert bound == ["doc-a"]
        assert "Q3 review.pdf" in agent_server._doc_binding_note(bound)
        # an unbound conversation gets no note
        ids2, bound2 = agent_server._conversation_doc_scope(
            "conv-scope", {"doc_ids": []})
        assert bound2 == [], bound2
    finally:
        _clear_binding("conv-scope")


def test_a_bound_document_that_cannot_answer_is_not_named(monkeypatch):
    """Naming a document the tool cannot read would tell the model "this is the
    subject" and leave it guessing at the contents."""
    _stub_registry(monkeypatch, {"documents": [
        {"id": "doc-off", "filename": "Private.pdf", "enabled": False,
         "status": "ready"},
        {"id": "doc-a", "filename": "Q3 review.pdf", "enabled": True,
         "status": "ready"},
    ]})
    ids, bound = agent_server._conversation_doc_scope(
        "conv-unreadable", {"doc_ids": ["doc-off", "doc-a"]})
    assert ids == {"doc-a"}
    assert bound == ["doc-a"], "a switched-off document must not be named as the subject"


def test_binding_note_still_says_so_when_titles_cannot_be_read(monkeypatch):
    """The registry being away must not turn a bound turn into an unbound one:
    the tool allowlist is already narrow, and the model has to be told to search
    rather than left to infer nothing is bound."""
    _stub_registry(monkeypatch, boom=True)
    note = agent_server._doc_binding_note(["doc-a"])
    assert note and "could not be read" in note, note




def test_the_citation_contract_is_opt_in_per_turn(monkeypatch):
    """`[[p:3|words]]` is only worth asking for when a viewer can light it up.

    Plain Chat has no page on screen, so a marker there is punctuation the user
    would have to read around. The document pane sends `cite_markers`; nothing
    else does, and the note has to keep the two apart.
    """
    _stub_registry(monkeypatch, NAMED)
    plain = agent_server._doc_binding_note(["doc-a"])
    assert "[[p:" not in plain, plain
    cited = agent_server._doc_binding_note(["doc-a"], cite=True)
    assert "[[p:PAGE|WORDS]]" in cited, cited
    assert "CHARACTER FOR CHARACTER" in cited, cited
    # The page number has to come from the excerpt header, not from the model's
    # memory of pagination: a wrong page highlights the wrong page.
    assert "page number that" in cited, cited


def test_citation_markers_do_not_replay_into_the_next_turn():
    """A marker is a display contract with one surface, not part of the prose.

    Left in the history, the model learns the format and starts using it in a
    later ordinary chat turn that has no viewer at all — the reader then sees
    bracketed fragments in an answer about something else."""
    history = [
        {"role": "assistant",
         "content": "Line 7 retired in August [[p:2|Line 7 retired in August]], "
                    "and the deck stayed open [[p:3|the deck stayed open]]."},
        {"role": "user", "content": "and the forecast?"},
    ]
    out = agent_server._chat_history_for_model(history)
    joined = "\n".join(m["content"] for m in out)
    assert "[[p:" not in joined, joined
    assert "Line 7 retired in August" in joined, "the prose survives; only the marker goes"


def test_a_plain_double_bracket_survives_the_strip():
    """Only the `p:` form is a citation. Wiki-style links in a document quote
    are the user's text and must not be eaten by the marker regex."""
    assert agent_server._strip_citation_markers(
        "see [[Chapter 2]] and [[p:1|see this]]") == "see [[Chapter 2]] and "


def test_a_dropped_closing_bracket_still_does_not_reach_the_model():
    """Live shape: `[[p:7|Supervised Fine-Tuning (SFT)]` — one bracket, not two.

    The strict reader missed it, so the marker stayed in the stored answer and
    went back into the next turn's history, where the model learns to write
    citations at a surface that cannot draw them."""
    assert agent_server._strip_citation_markers(
        "phases [[p:7|Supervised Fine-Tuning (SFT)]" + "\n\n" + "more"
    ) == "phases " + "\n\n" + "more"
