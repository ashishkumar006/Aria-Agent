"""Reading a stored document — the four things that made it unreliable.

1. A fetched page and a search list carried no `session_id`, because the tool
   process was never told which run spawned it, so the library could say a row
   came "from the web" but not from which run.
2. `read_artifact` decoded a PDF's BYTES as UTF-8. The agent could not read the
   document it had just written — only whatever similarity search returned.
3. `search_knowledge` ranks chunks, so "summarise this document" or "what does
   section 4 say" had no deterministic path at all. `read_document` walks the
   stored chunks in order.
4. A turn bound to one document got the same six-chunk slice as an open question
   about everything, which is a guess with citations rather than a reading.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import agent_server  # noqa: E402
import artifacts  # noqa: E402
import gateway  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture(autouse=True)
def _private_store(tmp_path, monkeypatch):
    real = artifacts.STORE
    artifacts.STORE = tmp_path / "artifacts"
    artifacts.STORE.mkdir(parents=True, exist_ok=True)
    yield artifacts.STORE
    artifacts.STORE = real


# ── 1. the run that fetched a page ──────────────────────────────────────────

def test_a_fetched_page_carries_the_run_that_fetched_it(monkeypatch):
    """`mcp_runner` exports ARIA_SESSION when it spawns the tool child; the
    filing helpers read it. Without it every web row has session_id="" and no
    later turn can group a run's sources back together."""
    posts: list[dict] = []
    monkeypatch.setattr(gateway, "post_multipart",
                        lambda path, *, files, data=None, timeout=120.0:
                        posts.append(data or {}) or
                        {"document": {"id": "doc-web-sess-001"}})
    monkeypatch.setenv("ARIA_SESSION", "s8-live-1")
    mcp_server._store_page_as_document(
        "https://example.org/report",
        {"status": 200, "text": "# Report\n\nReal content.", "length_bytes": 20})
    assert posts[-1]["session_id"] == "s8-live-1"
    mcp_server._store_hits_as_document(
        "web_search", "battery prices", [{"title": "T", "url": "https://t.dev",
                                          "content": "c"}])
    assert posts[-1]["session_id"] == "s8-live-1"


def test_no_session_is_still_a_valid_fetch(monkeypatch):
    """A tool run outside a session (a CLI call, a chat turn with no id yet)
    must not fail because the variable is absent."""
    posts: list[dict] = []
    monkeypatch.delenv("ARIA_SESSION", raising=False)
    monkeypatch.setattr(gateway, "post_multipart",
                        lambda path, *, files, data=None, timeout=120.0:
                        posts.append(data or {}) or
                        {"document": {"id": "doc-web-sess-002"}})
    mcp_server._store_page_as_document(
        "https://example.org/x", {"status": 200, "text": "# X\n\ntext",
                                  "length_bytes": 4})
    # `register_with_documents` omits an empty session rather than sending "",
    # so the row keeps its default and the fetch still succeeds.
    assert posts[-1].get("session_id", "") == ""


# ── 2. reading a PDF back ───────────────────────────────────────────────────

def test_a_pdf_artifact_is_not_dumped_as_decoded_bytes():
    """The old behaviour: `raw.decode("utf-8", errors="replace")` on a PDF.
    The model received a wall of replacement characters and called it the
    document's contents."""
    art = artifacts.put(b"%PDF-1.4 not a real pdf \x00\x01\x02 body",
                        content_type="application/pdf", source="author:pdf",
                        descriptor="Broken fixture")
    out = mcp_server.read_artifact(art)
    assert out["ok"] is False
    # Either it could not be parsed or it has no text layer — both are honest
    # answers. What must never happen is `ok: True` with binary mojibake.
    assert "text" in out["error"] or "extract" in out["error"], out["error"]
    assert "\ufffd\ufffd\ufffd" not in str(out.get("content") or "")


def test_a_text_artifact_still_reads_as_text():
    art = artifacts.put("Solar additions reached 511 GW.".encode("utf-8"),
                        content_type="text/plain", source="researcher",
                        descriptor="findings")
    out = mcp_server.read_artifact(art)
    assert out["ok"] is True
    assert out["content"].startswith("Solar additions reached 511 GW")
    assert out["size_bytes"] == len("Solar additions reached 511 GW.".encode())


# ── 3. reading a stored document in order ───────────────────────────────────

CHUNKS = {"document_id": "doc-a", "filename": "Report.pdf", "status": "ready",
          "total_chunks": 3, "start": 0, "next_start": 2,
          "chunks": [
              {"index": 0, "kind": "heading", "page": 1, "heading_path": [],
               "words": 3, "text": "Section One Title"},
              {"index": 1, "kind": "para", "page": 1,
               "heading_path": ["Section One"], "words": 9,
               "text": "Body text of section one with the figures in it."}]}


def test_read_document_walks_the_chunks_and_keeps_the_full_text(monkeypatch):
    seen: dict = {}

    def _get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return CHUNKS

    monkeypatch.setattr(mcp_server._memory, "_get", _get)
    monkeypatch.setenv("ARIA_DOC_IDS", "doc-a,doc-b")
    out = mcp_server.read_document("doc-a", start_chunk=0, count=2)
    assert out["ok"] is True
    assert out["next_start"] == 2
    assert "Section One Title" in out["text"]
    # Full text, not the detail view's 400-character preview.
    assert "Body text of section one with the figures in it." in out["text"]
    assert seen["path"] == "/v1/documents/doc-a/text"
    assert seen["params"] == {"start": 0, "count": 2}


def test_read_document_refuses_a_document_the_thread_cannot_read(monkeypatch):
    """The allowlist is the security boundary: a document switched off for
    search, or a thread with documents off, must be refused rather than read."""
    called: list = []
    monkeypatch.setattr(mcp_server._memory, "_get",
                        lambda *a, **k: called.append(a) or {})
    monkeypatch.setenv("ARIA_DOC_IDS", "doc-allowed")
    out = mcp_server.read_document("doc-secret")
    assert out["ok"] is False
    assert "not available" in out["error"]
    assert called == [], "the gateway was asked anyway"


def test_read_document_allows_everything_when_the_thread_is_unfiltered(
        monkeypatch):
    """ARIA_DOC_IDS unset means "not filtering" — the ordinary chat case."""
    monkeypatch.delenv("ARIA_DOC_IDS", raising=False)
    monkeypatch.setattr(mcp_server._memory, "_get", lambda *a, **k: CHUNKS)
    assert mcp_server.read_document("doc-a")["ok"] is True


def test_the_new_tool_is_advertised_where_tools_are_resolved():
    """A tool the model is not given is a tool that does not exist.

    `skills.tool_payload` warns and SKIPS a name that is in neither the catalog
    nor the local schemas, so listing `read_document` in a skill's
    `tools_allowed` without a catalog entry would silently drop it."""
    import skills
    assert "read_document" in skills._TOOL_CATALOG
    assert "read_document" in agent_server._CHAT_TOOLS
    payload = skills.tool_payload(["read_document"])
    assert payload and payload[0]["name"] == "read_document"
    # Its content can be third-party text (a stored web page), so the
    # untrusted-content contract has to be injected for it.
    assert "read_document" in skills.UNTRUSTED_TOOLS


# ── 4. the bound turn's budget ──────────────────────────────────────────────

def _async_client_factory(resp):
    """`_doc_context` awaits `c.post(...)` on an async client, so the fake's
    `post` has to be a coroutine. A sync fake fails inside the function's own
    `except`, which returns no documents — the test would then pass by proving
    nothing."""

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return resp

    return lambda *a: _Client()


def test_a_bound_turn_asks_for_far_more_of_the_document(monkeypatch):
    import asyncio

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"hits": [{"chunk": "x " * 500, "filename": "Report.pdf",
                              "page": 1} for _ in range(12)]}

    resp = _Resp()

    def _factory(*_a):
        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured["payload"] = json
                return resp

        return _C()

    monkeypatch.setattr(agent_server, "async_gateway_session", _factory)
    monkeypatch.setattr(agent_server, "_gw_auth_headers", lambda *a: {})
    block, n = asyncio.run(agent_server._doc_context(
        "summarise the whole document", {"doc-a"}, bound=True))
    assert n > 0, "the block came back empty, so the assertions below are vacuous"
    assert captured["payload"]["top_k"] == agent_server._DOC_CTX_BOUND_TOP_K
    assert captured["payload"]["top_k"] > agent_server._DOC_CTX_TOP_K
    asyncio.run(agent_server._doc_context("what about X", {"doc-a"}))
    assert captured["payload"]["top_k"] == agent_server._DOC_CTX_TOP_K


def test_a_bound_turn_keeps_more_text_in_the_prompt(monkeypatch):
    """The budget is what actually limits the block; the header's own length is
    reserved out of it, so both numbers have to move together."""
    import asyncio

    hits = [{"chunk": ("word " * 400).strip(), "filename": "Report.pdf",
             "page": 1} for _ in range(30)]

    class _Resp:
        status_code = 200

        def json(self):
            return {"hits": hits}

    monkeypatch.setattr(agent_server, "async_gateway_session",
                        _async_client_factory(_Resp()))
    monkeypatch.setattr(agent_server, "_gw_auth_headers", lambda *a: {})
    plain, n_plain = asyncio.run(
        agent_server._doc_context("q", {"doc-a"}))
    bound, n_bound = asyncio.run(
        agent_server._doc_context("q", {"doc-a"}, bound=True))
    assert n_plain > 0 and n_bound > 0, (n_plain, n_bound)
    assert n_bound > n_plain, (n_plain, n_bound)
    assert len(bound) > len(plain)
    assert len(bound) <= agent_server._DOC_CTX_BOUND_MAX_CHARS + 1200


def test_with_doc_context_infers_the_binding_from_a_small_allowlist(monkeypatch):
    import asyncio

    seen: dict = {}

    async def _fake(query, doc_ids, *, bound=False):
        seen["bound"] = bound
        return "block", 3

    monkeypatch.setattr(agent_server, "_doc_context", _fake)
    asyncio.run(agent_server._with_doc_context(
        [{"role": "system", "content": "s"},
         {"role": "user", "content": "q"}], "q", {"doc-a"}))
    assert seen["bound"] is True
    asyncio.run(agent_server._with_doc_context(
        [{"role": "system", "content": "s"},
         {"role": "user", "content": "q"}], "q",
        {f"doc-{i}" for i in range(20)}))
    assert seen["bound"] is False
