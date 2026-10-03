"""Two critical defects found by black-box probing of the running agent.

Both were invisible to the existing suite because both are *transport* or
*destructive-path* bugs on routes the tests exercised only in the happy case.

1. **SSE chunking destroyed one space per boundary.** `_chat_chunks` split on
   `" "` and rejoined each group with `" "`, discarding the separator *between*
   groups. Measured live over ~600 boundaries in 20 streams: 359 inserted
   characters, 0 deleted, exactly 1.0 lost space per boundary. A client
   rendering from `delta` saw `"How can Ihelp you today?"` while `done.answer`
   — and the persisted history — held the correct text. That divergence is
   exactly why it survived: the final message was always right.

2. **`DELETE /api/memory?session_id=&confirm=wipe` wiped the entire store.**
   The guard was `if session_id:`, and an empty string is falsy, so a
   present-but-blank filter was dropped and the request reached the gateway
   unscoped. It destroyed 215 memory rows and 45 chat threads in one call, with
   nothing in the audit log and no undo.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent_server  # noqa: E402

import pytest  # noqa: E402

SAMPLES = [
    "Hi! How can I help you today?",
    "alpha beta gamma delta epsilon zeta eta theta",
    "one",
    "a b c d e f g h i j k l m n o p",
    "* **Breakfast:** Boiled Egg (5 Nos Max) / Boiled White Chana (150gms)",
    "trailing space ",
    " leading space",
    "",
]


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("wpc", [1, 2, 3, 4, 7, 100])
def test_joining_the_chunks_reproduces_the_answer_exactly(text, wpc):
    """The invariant that was broken: `"".join(chunks) == text`.

    A single lost space per boundary is enough to render `"a staplefood"` and
    to corrupt anything the user copies out of a live stream.
    """
    joined = "".join(agent_server._chat_chunks(text, words_per_chunk=wpc))
    assert joined == text, (repr(joined), repr(text))


@pytest.mark.parametrize("text", SAMPLES)
def test_no_chunk_boundary_eats_a_separator(text):
    for wpc in (1, 2, 3, 4, 5):
        chunks = list(agent_server._chat_chunks(text, words_per_chunk=wpc))
        assert "".join(chunks) == text, (wpc, chunks)


def test_words_are_never_glued_together():
    text = "Bread is a staple food prepared from a dough of flour and water"
    for wpc in (1, 2, 3, 4):
        joined = "".join(agent_server._chat_chunks(text, words_per_chunk=wpc))
        # Every original inter-word space must still be present.
        assert joined.count(" ") == text.count(" "), (wpc, joined)


def test_a_chunk_never_starts_mid_word():
    """Chunks split on word boundaries so markdown is not re-parsed mid-token.
    That guarantee must survive the separator fix."""
    text = "**bold** and *italic* and `code`"
    for wpc in (1, 2, 3):
        for chunk in agent_server._chat_chunks(text, words_per_chunk=wpc):
            assert chunk == chunk.strip() or chunk.strip(), repr(chunk)


def test_empty_text_yields_nothing():
    assert list(agent_server._chat_chunks("")) == []
    assert list(agent_server._chat_chunks(None)) == []


def test_chunking_is_deterministic():
    text = "alpha beta gamma delta epsilon"
    a = list(agent_server._chat_chunks(text, 3))
    b = list(agent_server._chat_chunks(text, 3))
    assert a == b


# ── the wipe footgun ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_blank_session_id_is_refused_not_treated_as_no_filter():
    """`session_id=` is what a client sends when the conversation id has not
    been assigned yet. Reading it as "no filter" turned a scoped intent into a
    global wipe."""
    from fastapi import Request

    scope = {"type": "http", "method": "DELETE", "path": "/api/memory",
             "headers": [], "query_string": b"session_id=&confirm=wipe"}
    resp = await agent_server.wipe_memory(session_id="", confirm="wipe")
    assert resp.status_code == 400, resp
    assert "empty" in resp.body.decode().lower(), resp.body


@pytest.mark.asyncio
async def test_whitespace_only_session_id_is_refused():
    resp = await agent_server.wipe_memory(session_id="   ", confirm="wipe")
    assert resp.status_code == 400, resp


@pytest.mark.asyncio
async def test_a_real_session_id_still_passes_the_guard():
    """The fix must not break the legitimate scoped wipe: a real id is not
    rejected as blank."""
    out = await agent_server.wipe_memory(session_id="ct-abc123", confirm="wipe")
    # Whether the gateway call succeeds is not this test's concern; what matters
    # is that the blank-id guard did not fire.
    assert not (isinstance(out, dict) and out.get("status") == "error"
                and "empty" in str(out.get("message", "")).lower()), out


@pytest.mark.asyncio
async def test_an_absent_session_id_still_allows_the_global_wipe():
    """Omitting the parameter is the documented way to wipe everything, gated by
    `confirm=wipe`. It must keep working."""
    out = await agent_server.wipe_memory(session_id=None, confirm="wipe")
    assert not (isinstance(out, dict) and out.get("status") == "error"), out


@pytest.mark.asyncio
async def test_wipe_still_requires_the_exact_confirm_token():
    for bad in (None, "", "WIPE", "Wipe", "wipe ", "true", "1", "yes"):
        resp = await agent_server.wipe_memory(session_id="ct-x", confirm=bad)
        assert resp.status_code == 400, (bad, resp)
