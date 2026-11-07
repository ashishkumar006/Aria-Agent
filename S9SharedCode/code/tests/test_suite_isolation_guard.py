"""The suite must be unable to touch the live Documents library, in either verb.

Why this file exists
--------------------
`conftest._no_live_document_uploads` neutralises the gateway write helpers, and
everybody assumed that meant a test could not change the user's library. It
could. `_persist_persona_documents` *replaces* two documents: it lists the
library, DELETEs the previous copies of "Persona — current.md" and
"Persona — evolution.md", then uploads the new ones. Only the upload was
neutralised. So a chat-turn persona worker running inside the suite deleted both
live copies — one of them 322 chunks — and filed nothing in their place, because
the upload half had been told the gateway was away.

The general shape of the bug: a guard on the write verb of a two-verb operation
turns an atomic replace into a destructive delete.

Note on how that is proved here. Every `gateway.*_json` helper is deliberately
fail-soft — a connection error returns `None`, because callers treat "the
gateway is away" as a normal answer. So a tripwire that merely raises cannot
tell the guard from the real function: both end up returning `None`. The
tripwire therefore *records the attempt* before raising, and the assertion is
about whether a request was ever tried.
"""
from __future__ import annotations

import pytest


class _Cut:
    """Records every attempted request, then behaves like an unreachable gateway."""

    def __init__(self, sink):
        self._sink = sink

    def _boom(self, method, url, **kw):
        self._sink.append(f"{method} {url}")
        raise ConnectionError("network cut for this test")

    def get(self, url, **kw):
        return self._boom("GET", url, **kw)

    def post(self, url, **kw):
        return self._boom("POST", url, **kw)

    def delete(self, url, **kw):
        return self._boom("DELETE", url, **kw)

    def request(self, method, url, **kw):
        return self._boom(method.upper(), url, **kw)


@pytest.fixture()
def cut_network(monkeypatch):
    import gateway
    attempts: list[str] = []
    monkeypatch.setattr(gateway, "_client", lambda: _Cut(attempts), raising=False)
    monkeypatch.setattr(gateway, "sync_pool", lambda *a, **k: _Cut(attempts), raising=False)
    return attempts


def test_the_guard_neutralises_the_delete_verb_too(cut_network):
    """The three helpers the fixture holds shut must not even try a socket.

    Deleting is the verb that does the damage, and it is the one a "writes are
    blocked" guard forgets: the persona publisher reads the library with a
    helper nobody patches, deletes with `delete_json`, and uploads with
    `post_multipart`.
    """
    import gateway
    assert gateway.post_multipart("/v1/documents", files={"file": ("a.txt", b"x")}) is None
    assert gateway.delete_json("/v1/documents/doc-whatever") is None
    assert gateway.document_exists("doc-whatever") is None
    assert cut_network == [], f"the suite reached the live gateway: {cut_network}"


def test_the_persona_publisher_leaves_the_live_library_untouched():
    """The publisher's listing call is a real read against :8109, so the honest
    assertion is the one that matters: the user's document ids before and after.

    The publisher is allowed to report failure — with uploads held shut it must,
    or a test run would claim a persona was published when nothing was filed.
    What it must never do is delete first and fail second.
    """
    import agent_server
    import gateway

    _seed_persona(agent_server)
    before = _live_doc_ids(gateway)
    if before is None:
        pytest.skip("no live gateway to compare against")
    out = agent_server._persist_persona_documents()
    assert isinstance(out, dict)
    assert out.get("ok") is not True, (
        f"a suite run published persona documents into the live library: {out}")
    after = _live_doc_ids(gateway)
    assert after == before, (
        f"the suite changed the live library: removed={sorted(before - after)} "
        f"added={sorted(after - before)}")


def _seed_persona(agent_server):
    """Give the publisher a persona to publish.

    Without one the store reports "no persona" and returns before reaching the
    library, so the test above would pass on the broken guard too. The state dir
    is the temp one `S9_STATE_DIR` points at, so seeding it files nothing.
    """
    d = agent_server.STATE_DIR / "persona"
    d.mkdir(parents=True, exist_ok=True)
    (d / "current.md").write_text(
        "# Persona\n\nSeeded by the isolation guard; never published.\n",
        encoding="utf-8")
    (d / "evolution.md").write_text(
        "# Persona evolution\n\nVersion 1 — seeded, not published.\n",
        encoding="utf-8")


def _live_doc_ids(gateway):
    """The ids currently in the user's library, or None when the gateway is away."""
    try:
        listing = gateway.get_json("/v1/documents") or {}
    except Exception:
        return None
    rows = listing.get("documents") or listing.get("items")
    if rows is None:
        return None
    return {str(d.get("id")) for d in rows if isinstance(d, dict)}
