"""Recall must degrade rather than stall the caller.

`_query_vector` embedded the query with no deadline, so the embedder's own 60s
socket timeout became the caller's latency. Measured on this machine's ledger:
15,917 searches, median 1.4s, p90 9.7s, 1,164 over 20s, worst 78.5s — because
nomic-embed-text is fast while Ollama keeps it resident and costs a 30-40s load
when it does not. One of those slow searches was triggered by the two-character
message "hi" (`calls.id=107140`, 35,550ms).

Keyword recall answers in milliseconds without a vector, so a search that
cannot embed in time now falls back to it instead of holding up a chat turn.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import memory.plane as plane_mod
from memory.plane import MemoryPlane


class _Embedder:
    """Just the two attributes `_query_vector` reads off `self`."""

    def __init__(self, delay: float = 0.0, fail: Exception | None = None):
        self.delay = delay
        self.fail = fail

    async def _embed(self, text, task_type):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"embedding": [0.1] * 768, "model": "nomic-embed-text", "dim": 768}


def test_a_cold_embed_falls_back_to_keyword_recall(monkeypatch):
    monkeypatch.setattr(plane_mod, "QUERY_EMBED_BUDGET_S", 0.2)

    async def go():
        t0 = time.perf_counter()
        got = await MemoryPlane._query_vector(_Embedder(delay=3.0), "hi")
        return got, time.perf_counter() - t0

    vec, waited = asyncio.run(go())
    assert vec is None, "a search that timed out must report no vector, not raise"
    assert waited < 1.5, f"the caller waited {waited:.1f}s for a dead embed"


def test_a_warm_embed_still_returns_the_vector(monkeypatch):
    """The budget must not quietly switch vector recall off when it is fine."""
    async def go():
        return await MemoryPlane._query_vector(_Embedder(), "what units do I use?")
    assert asyncio.run(go()) == [0.1] * 768


def test_a_failed_embed_still_falls_back_without_raising():
    """The old single `except Exception` path must survive the refactor."""
    async def go():
        return await MemoryPlane._query_vector(
            _Embedder(fail=RuntimeError("ollama HTTP 500")), "hi")
    assert asyncio.run(go()) is None


def test_the_budget_is_a_real_deadline_not_a_comment():
    assert plane_mod.QUERY_EMBED_BUDGET_S <= 15, (
        "a recall budget longer than a human will wait for a greeting is not a "
        "budget; the 35.5s row this guards against was inside the old 60s socket "
        "timeout and nobody saw it coming")


# ── the ledger must bill what was actually sent ──────────────────────────
"""The streamed branch logged `input_tokens=len(prompt_text) // 4`, and
`prompt_text` joins the MESSAGES only. `req.tools` — 27 chat tool schemas,
16,928 chars, ~4,232 tokens — never entered the row, so a chat turn logged
5,076 input tokens for a request the provider billed around 9,300. Every
streamed row had `prompt_chars == input_tokens * 4` exactly, which is the
signature of an estimate that forgot half the prompt."""


def test_streamed_calls_log_the_router_estimate():
    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(
        encoding="utf-8")
    assert "input_tokens=est_in," in src, (
        "the streamed branch is back to counting messages only; tool schemas "
        "are then invisible in every cost view built on `calls`")
    assert "input_tokens=len(prompt_text) // 4" not in src


def test_the_router_estimate_counts_tools():
    """`est_in` is only the fix if it actually includes the tool payload."""
    import main as gw

    msgs = [{"role": "user", "content": "hello"}]
    without = gw._est_input_tokens(msgs, None, None)
    with_tools = gw._est_input_tokens(msgs, None, [
        {"type": "function", "function": {
            "name": f"tool_{i}", "description": "x" * 300}} for i in range(20)])
    assert with_tools > without + 1000, (
        f"tools added only {with_tools - without} tokens — the schemas are not "
        f"in the estimate, so the ledger still hides them")


def test_log_call_records_thinking_and_rate():
    """Tokens/s is only honest if thinking tokens are in the numerator:
    `candidatesTokenCount` excludes them, and Gemini 3 spends most of a short
    answer thinking, which is why small calls read as 0.01 tok/s."""
    import db

    sig = inspect.signature(db.log_call)
    assert "thinking_tokens" in sig.parameters
    body = inspect.getsource(db.log_call)
    assert "output_tps" in body, "the rate column is never written"
