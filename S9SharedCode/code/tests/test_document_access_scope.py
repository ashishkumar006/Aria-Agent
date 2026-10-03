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
import memory as memory_mod  # noqa: E402


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
    make these tests pass for the wrong reason."""
    if boom:
        def _raise(*_a, **_k):
            raise RuntimeError("gateway unreachable")
        monkeypatch.setattr(memory_mod, "_get", _raise)
        return
    body = payload if payload is not None else DOCS
    monkeypatch.setattr(memory_mod, "_get", lambda *_a, **_k: body)


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
