"""Chat must reach the user's uploaded documents without the model asking.

The bug this pins: chat passed the documents to the model only as an
AVAILABLE TOOL (`search_knowledge`). That is a suggestion, not access. A model
that skipped the tool answered from its own weights, and with the `chat.tools`
prefab flag off it could not call the tool at all - so the console read as
though chat had no documents, which was true.

The fix is deterministic retrieval on the request path. These tests cover the
retrieval, the prompt block, the injection point, and - the part that actually
broke first - that the function has the names it needs at call time.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_server  # noqa: E402


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **_kw):
        self.calls.append((url, json or {}))
        return self.responder(url, json or {})


def _patch_gateway(monkeypatch, responder):
    """Route the module's httpx.AsyncClient at the single call site."""
    import httpx

    holder: dict[str, _Client] = {}

    def factory(*_a, **_k):
        client = _Client(responder)
        holder["c"] = client
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return holder


def _hit(chunk: str, filename: str = "handbook.pdf", heads=None, page=None):
    return {"id": "mem:doc-1-0", "doc_id": "doc-1", "filename": filename,
            "chunk_index": 0, "page": page, "heading_path": heads or [],
            "descriptor": chunk[:200], "chunk": chunk,
            "embed_model": "nomic-embed-text"}


# ── the names the function needs at call time ─────────────────────────────────
# `_doc_context` referenced `httpx` and `_GW_BASE`, neither of which was in
# scope where it was defined. Both raised at call time and were swallowed by
# the broad `except`, so retrieval silently returned nothing and the feature
# looked like it worked while doing nothing.


def test_retrieval_actually_calls_the_gateway(monkeypatch):
    holder = _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [_hit("X")]}))

    async def go():
        return await agent_server._doc_context("anything", {"doc-1"})

    text, n = asyncio.run(go())
    assert holder.get("c") is not None, "the gateway was never called: a name is missing"
    assert n == 1, (n, text)


def test_gateway_base_constant_exists_at_module_scope():
    assert isinstance(agent_server._GW_BASE, str)
    assert agent_server._GW_BASE.startswith("http")


# ── the retrieval itself ─────────────────────────────────────────────────────


def test_hits_become_a_cited_block(monkeypatch):
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [
        _hit("Refunds are processed within 5 business days.",
             filename="policy.pdf", heads=["Refunds", "Timing"], page=3),
        _hit("Support is available 09:00-17:00 CET."),
    ]}))

    async def go():
        return await agent_server._doc_context("how long do refunds take", {"doc-1"})

    text, n = asyncio.run(go())
    assert n == 2
    assert "REFERENCE MATERIAL" in text
    # Provenance must name the file and where in it the chunk came from, or the
    # model cannot cite anything a user can check.
    assert "policy.pdf" in text
    assert "Refunds > Timing" in text
    assert "p. 3" in text
    assert "[1]" in text and "[2]" in text
    assert "5 business days" in text


def test_empty_result_says_retrieval_matched_nothing_and_names_the_documents(monkeypatch):
    """A successful search that matched nothing must NOT be silent.

    Silence was the worst failure observed in the wild: with no block in the
    prompt there was no evidence the user had uploaded anything, so the model
    read "I have nothing" as "it must be in their inbox", called Gmail and
    Calendar, and reported THOSE credentials' expiry as the reason it could not
    answer a question whose answer was sitting in an enabled document.

    The wording is still careful, and the original objection still holds: this
    must claim "your search matched nothing", never "you have no documents".
    So the test asserts the absence of the empty-corpus claim as well.
    """
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents",
                        lambda: [{"id": "doc-1", "filename": "Menu.pdf"}])
    text, n = asyncio.run(agent_server._doc_context("q", {"doc-1"}))
    assert n == 0
    assert "NO MATCH" in text
    assert "Menu.pdf" in text, "the user must be told what they actually have"
    for false_claim in ("you have no documents", "no documents found",
                        "you have not uploaded"):
        assert false_claim.lower() not in text.lower(), false_claim
    assert "authentication" not in text.lower() or "not" in text.lower()


def test_a_broken_or_empty_gateway_still_says_nothing(monkeypatch):
    """The original objection, kept: a FAILED lookup must not claim anything.

    `test_empty_result_*` fires only after a 200. If the gateway is down, or
    returns 500, the honest statement is silence - we do not know what the user
    has, so we must not assert an empty corpus either way.
    """
    _patch_gateway(monkeypatch, lambda u, b: _Resp(500, {"error": "boom"}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents",
                        lambda: [{"id": "doc-1", "filename": "Menu.pdf"}])
    text, n = asyncio.run(agent_server._doc_context("q", {"doc-1"}))
    assert (text, n) == ("", 0)


def test_no_note_when_the_user_actually_has_no_documents(monkeypatch):
    """Registry genuinely empty -> "you have not uploaded any documents".

    This is the one case where an empty-corpus claim is TRUE, so stating it is
    safe - and it is still where the substitution rule is needed most, because
    a user asking about "the menu" with nothing uploaded is exactly who goes
    hunting through their inbox.
    """
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents", lambda: [])
    monkeypatch.setattr(agent_server, "_gw_all_documents", lambda: [])
    text, n = asyncio.run(agent_server._doc_context("q", {"doc-1"}))
    assert n == 0
    assert "NOT UPLOADED ANY DOCUMENTS" in text
    assert "email" in text and "Never report" in text


def test_documents_that_are_all_disabled_are_named_not_hidden(monkeypatch):
    """The gap that produced the reported failure.

    A user switched their menu off, so no document was ENABLED, so the old
    guard stayed silent - and the model read "I have nothing" as "it must be
    in their inbox" and reported their expired Gmail token as the reason it
    could not answer a question the menu contained. The document exists; the
    prompt has to say so and say why it cannot answer.
    """
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents", lambda: [])
    monkeypatch.setattr(agent_server, "_gw_all_documents", lambda: [
        {"id": "d1", "filename": "Menu.pdf", "ready": True,
         "enabled": False, "status": "ready"},
        {"id": "d2", "filename": "Big.pdf", "ready": False,
         "enabled": True, "status": "parsing"},
    ])
    text, n = asyncio.run(agent_server._doc_context("menu for monday", {"d1"}))
    assert n == 0
    assert "NONE OF THEM CAN ANSWER" in text
    assert "Menu.pdf (disabled)" in text
    assert "Big.pdf (parsing)" in text
    # Still must not claim the corpus is empty.
    assert "have not uploaded" not in text.lower()


def test_documents_off_stays_silent(monkeypatch):
    """An empty allow-list is a deliberate user choice, not a retrieval miss."""
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents",
                        lambda: [{"id": "doc-1", "filename": "Menu.pdf"}])
    monkeypatch.setattr(agent_server, "_gw_all_documents", lambda: [])
    text, n = asyncio.run(agent_server._doc_context("q", set()))
    assert (text, n) == ("", 0)


def test_gateway_failure_never_fails_the_turn(monkeypatch):
    _patch_gateway(monkeypatch, lambda u, b: _Resp(500, {"error": "boom"}))
    text, n = asyncio.run(agent_server._doc_context("q", {"doc-1"}))
    assert (text, n) == ("", 0)


def test_transport_exception_never_fails_the_turn(monkeypatch):
    import httpx

    def boom(*_a, **_k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(httpx, "AsyncClient", boom)
    text, n = asyncio.run(agent_server._doc_context("q", {"doc-1"}))
    assert (text, n) == ("", 0)


def test_docs_off_short_circuits_without_calling_the_gateway(monkeypatch):
    holder = _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [_hit("X")]}))
    text, n = asyncio.run(agent_server._doc_context("q", set()))
    assert (text, n) == ("", 0)
    assert not holder.get("c"), "docs-off must not reach the gateway at all"


def test_enabled_ids_are_sent_so_the_gateway_cannot_widen_the_scope(monkeypatch):
    holder = _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    asyncio.run(agent_server._doc_context("q", {"doc-1", "doc-2"}))
    _url, body = holder["c"].calls[0]
    assert sorted(body["doc_ids"]) == ["doc-1", "doc-2"], body


def test_character_budget_is_enforced(monkeypatch):
    big = "x" * 40_000
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [
        _hit(big, filename="a.txt"), _hit(big, filename="b.txt"),
        _hit(big, filename="c.txt"),
    ]}))

    async def go():
        return await agent_server._doc_context("q", {"doc-1"})

    text, n = asyncio.run(go())
    assert len(text) < agent_server._DOC_CTX_MAX_CHARS + 500, len(text)
    assert n < 3, "the budget must stop it before including every chunk"


def test_the_cap_bounds_the_whole_block_not_just_the_chunks(monkeypatch):
    """The header is part of what enters the prompt, so it comes out of the
    budget. It used to be prepended after the chunks were summed, so the real
    total overshot the cap by the header's length - a bound that only held
    while the wording stayed short."""
    big = "x" * 40_000
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [
        _hit(big, filename="a.txt"), _hit(big, filename="b.txt"),
    ]}))

    async def go():
        return await agent_server._doc_context("q", {"doc-1"})

    text, n = asyncio.run(go())
    assert n >= 1, text
    assert len(text) <= agent_server._DOC_CTX_MAX_CHARS, len(text)
    # The budget must not be spent entirely on overhead.
    assert len(text) > agent_server._DOC_CTX_MAX_CHARS - 500, len(text)


def test_blank_chunks_are_skipped_rather_than_rendered_empty(monkeypatch):
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [
        {"doc_id": "d", "filename": "a.txt", "chunk": "   ", "heading_path": []},
        _hit("real content"),
    ]}))
    text, n = asyncio.run(agent_server._doc_context("q", {"d"}))
    assert n == 1
    assert "real content" in text


def test_filename_is_never_missing_from_a_citation(monkeypatch):
    """A hit with no filename must still be attributable."""
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [
        {"doc_id": "d", "filename": "", "chunk": "body", "heading_path": []},
    ]}))
    text, _n = asyncio.run(agent_server._doc_context("q", {"d"}))
    assert "uploaded document" in text


# ── injection into the turn ──────────────────────────────────────────────────


def test_block_is_inserted_before_the_question(monkeypatch):
    """It belongs next to the question it is evidence for, not appended to the
    system prompt, or a long thread accumulates every earlier lookup."""
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": [_hit("evidence")]}))

    async def go():
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "older"},
                {"role": "user", "content": "the question"}]
        return await agent_server._with_doc_context(msgs, "the question", {"doc-1"})

    out, n = asyncio.run(go())
    assert n == 1
    assert out[-1]["content"] == "the question", out[-1]
    assert out[-2]["role"] == "system"
    assert "evidence" in out[-2]["content"]
    assert len(out) == 4


def test_the_no_match_turn_is_inserted_before_the_question(monkeypatch):
    """The note is a system turn adjacent to the question, like the real block.

    Anything else and it is either invisible (buried in the system prompt, where
    the model treats it as boilerplate) or repeated on every later turn.
    """
    _patch_gateway(monkeypatch, lambda u, b: _Resp(200, {"hits": []}))
    monkeypatch.setattr(agent_server, "_gw_enabled_documents",
                        lambda: [{"id": "doc-1", "filename": "Menu.pdf"}])

    async def go():
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "q"}]
        return await agent_server._with_doc_context(msgs, "q", {"doc-1"})

    out, n = asyncio.run(go())
    assert n == 0
    assert len(out) == 3
    assert out[1]["role"] == "system" and "NO MATCH" in out[1]["content"]
    assert out[-1]["content"] == "q"


def test_system_prompt_tells_the_model_what_the_block_is():
    """Without this the block is an unexplained wall of text and gets ignored
    in favour of a confident answer from the model's own weights."""
    s = agent_server._CHAT_SYSTEM
    assert "REFERENCE MATERIAL" in s
    assert "search_knowledge" in s
    assert "do NOT claim the user has no documents" in s
