"""Consume-then-fold: a tool result the node has already read stops being
re-sent in full.

Why this exists
---------------
The 4 KB artifact design was already in the codebase, and correct as far as it
goes — but it only applied to *cross-node inputs*. A node's own tool results are
appended to `messages` and re-sent on every later hop, and the researcher sets
`max_tool_result_chars: 0` (no clip) because clipping mid-page made a model
re-read the same file until it hit the hop cap. Measured in the gateway ledger:
prompts of 330,468 to 429,333 characters, and median researcher output
collapsing from ~1,070 tokens under 150k input to 266 above it.

The existing answer, `_elide_oldest_tool_output`, DELETES the text and tells the
model to call the tool again — and it only runs when a caller sets
`max_context_chars`, which the researcher does not. So it never fired where it
was most needed, and where it did fire it cost a re-fetch.

Folding keeps the same shape (bulk out of the prompt, pairing intact) but loses
nothing and needs no caller to opt in.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import mcp_runner as mr  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    import artifacts
    monkeypatch.setattr(artifacts, "STORE", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """Folding is the behaviour under test, so pin its switch on. The last test
    turns it off explicitly."""
    import flags as f
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=False: True, raising=False)


def _big(n=None):
    return ("A page of a long article. " * (n or 900))


def _round(idx: int, name: str, body: str) -> list[dict]:
    """One assistant turn asking for a tool, and the tool's answer."""
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"tc{idx}", "name": name, "arguments": {}}]},
        {"role": "tool", "tool_call_id": f"tc{idx}", "content": body},
    ]


def _names(msgs):
    out = {}
    for i, m in enumerate(msgs):
        for tc in (m.get("tool_calls") or []):
            out[str(tc.get("id"))] = tc.get("name")
    return out


def _tool_bodies(msgs):
    return [m["content"] for m in msgs if m.get("role") == "tool"]


# ── the timing rule ──────────────────────────────────────────────────────
def test_a_fresh_result_is_never_folded():
    """The node is entitled to the page it just asked for, whole."""
    msgs = _round(1, "fetch_url", _big())
    n, saved = mr._fold_consumed(msgs, _names(msgs))
    assert (n, saved) == (0, 0)
    assert len(_tool_bodies(msgs)[0]) > 10_000


def test_a_result_is_folded_once_a_later_round_has_been_written():
    """Two rounds: the newest stays whole, the consumed one becomes a handle."""
    first, second = _big(), _big()
    msgs = _round(1, "fetch_url", first) + _round(2, "fetch_url", second)
    names = _names(msgs)

    n, saved = mr._fold_consumed(msgs, names)
    assert n == 1
    assert saved > 0
    bodies = _tool_bodies(msgs)
    old = json.loads(bodies[0])
    assert old["folded"] is True
    assert old["tool"] == "fetch_url"
    assert old["artifact"].startswith("art:")
    # Both rounds are the same foldable size, so this asserts the RULE and not
    # the threshold: the first version of this test used a 1,040-char second
    # round, which was never foldable anyway and so proved nothing when the
    # protection was deleted.
    assert len(bodies[1]) == len(second), "the newest result was folded too"


# ── the lossless guarantee ───────────────────────────────────────────────
def test_the_folded_text_is_intact_in_the_store():
    """The whole point versus elision: nothing is thrown away."""
    import artifacts
    body = _big()
    msgs = _round(1, "fetch_url", body) + _round(2, "fetch_url", _big(40))
    mr._fold_consumed(msgs, _names(msgs))
    rec = json.loads(_tool_bodies(msgs)[0])
    back = artifacts.get_bytes(rec["artifact"]).decode("utf-8")
    assert back == body, "the handle does not return the original bytes"


def test_the_preview_is_a_real_prefix_and_the_note_names_the_way_back():
    body = _big()
    msgs = _round(1, "fetch_url", body) + _round(2, "fetch_url", _big(40))
    mr._fold_consumed(msgs, _names(msgs))
    rec = json.loads(_tool_bodies(msgs)[0])
    assert body.startswith(rec["preview"][:200])
    assert rec["chars"] == len(body)
    assert rec["bytes"] == len(body.encode("utf-8"))
    note = rec["note"]
    assert rec["artifact"] in note and "read_artifact" in note
    assert "Do NOT re-run" in note, (
        "the note must steer away from the old advice, which was to re-fetch")


def test_folding_shrinks_the_measured_prompt():
    msgs = _round(1, "fetch_url", _big()) + _round(2, "fetch_url", _big(40))
    before = mr._context_chars(msgs)
    mr._fold_consumed(msgs, _names(msgs))
    after = mr._context_chars(msgs)
    assert after < before * 0.2, (before, after)


# ── what is left alone ───────────────────────────────────────────────────
def test_a_small_result_is_not_folded():
    """A handle costs characters too; folding a small blob trades a tiny saving
    for a tool call the node now has to make."""
    msgs = _round(1, "fetch_url", "short answer") + _round(2, "fetch_url", "short too")
    assert mr._fold_consumed(msgs, _names(msgs)) == (0, 0)


def test_a_result_carrying_an_id_is_not_folded():
    """`schedule_task` returns the schedule id the user types back to cancel the
    job. Replacing that with a preview is not a saving, it is data loss."""
    body = json.dumps({"ok": True, "schedule_id": "sch-1d2034f0",
                       "pad": "x" * 20_000})
    msgs = _round(1, "schedule_task", body) + _round(2, "fetch_url", "y" * 9_000)
    n, _ = mr._fold_consumed(msgs, _names(msgs))
    assert n == 0
    assert "sch-1d2034f0" in _tool_bodies(msgs)[0]


def test_read_artifact_output_is_never_folded_again():
    """read_artifact IS the un-folding step. Folding its result would put the
    text back behind a handle and invite the node to fetch it again — a loop."""
    msgs = (_round(1, "fetch_url", "z" * 9_000)
            + _round(2, "read_artifact", _big(900))
            + _round(3, "fetch_url", "w" * 9_000))
    mr._fold_consumed(msgs, _names(msgs))
    bodies = _tool_bodies(msgs)
    assert '"folded"' not in bodies[1][:80], "read_artifact output was folded"
    assert len(bodies[1]) > 10_000


def test_an_unknown_tool_is_left_whole():
    """Folding is a judgement about the KIND of payload. When the name is
    missing or unclassified the safe answer is to change nothing."""
    msgs = _round(1, "brand_new_tool", _big()) + _round(2, "fetch_url", "q" * 9_000)
    assert mr._fold_consumed(msgs, _names(msgs)) == (0, 0)
    msgs2 = [{"role": "tool", "tool_call_id": "nope", "content": _big()}]
    assert mr._fold_consumed(msgs2, {}) == (0, 0)


# ── safety ───────────────────────────────────────────────────────────────
def test_folding_is_idempotent_and_does_not_double_store():
    import artifacts
    body = _big()
    msgs = _round(1, "fetch_url", body) + _round(2, "fetch_url", "n" * 9_000)
    names = _names(msgs)
    mr._fold_consumed(msgs, names)
    first = json.loads(_tool_bodies(msgs)[0])
    assert first["folded"] is True
    again = mr._fold_consumed(msgs, names)
    assert again[0] == 0, "a folded message was folded a second time"
    assert _tool_bodies(msgs)[0].count('"folded"') == 1
    assert artifacts.get_bytes(first["artifact"]).decode("utf-8") == body


def test_a_store_failure_keeps_the_text_inline(monkeypatch):
    """Losing a research finding to a disk hiccup is worse than a big prompt."""
    import artifacts

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(artifacts, "put", boom)
    body = _big()
    msgs = _round(1, "fetch_url", body) + _round(2, "fetch_url", "p" * 9_000)
    n, saved = mr._fold_consumed(msgs, _names(msgs))
    assert (n, saved) == (0, 0)
    assert _tool_bodies(msgs)[0] == body


def test_the_switch_restores_the_old_behaviour(monkeypatch, tmp_path):
    """Kill switch: off must mean whole results re-sent, and no residue — no
    artifacts written, no message touched."""
    import artifacts
    import flags as f
    monkeypatch.setattr(artifacts, "STORE", tmp_path / "store")
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=False: False, raising=False)
    body = _big()
    msgs = _round(1, "fetch_url", body) + _round(2, "fetch_url", "r" * 9_000)
    n, saved = mr._fold_consumed(msgs, _names(msgs))
    assert (n, saved) == (0, 0)
    assert _tool_bodies(msgs)[0] == body
    assert not list((tmp_path / "store").glob("*")), (
        "a switch that is off must not write to the store")


def test_elision_never_trades_a_fold_for_a_refetch():
    """The destructive fallback must not clobber a handle.

    `_elide_oldest_tool_output` replaces bulk with 'call the tool again'. If it
    runs over an already-folded message it destroys the only pointer to text
    that is still intact in the store — and then pays for the network again.
    """
    msgs = _round(1, "fetch_url", _big()) + _round(2, "fetch_url", "s" * 9_000)
    mr._fold_consumed(msgs, _names(msgs))
    folded = _tool_bodies(msgs)[0]
    assert mr._elide_oldest_tool_output(msgs) is True  # elides the newest round
    assert _tool_bodies(msgs)[0] == folded, "elision destroyed a fold handle"
    assert "art:" in _tool_bodies(msgs)[0]


def test_the_loop_folds_in_practice_not_just_the_helper():
    """End-to-end wiring, because every test above calls `_fold_consumed`
    directly and a helper nothing invokes fixes nothing.

    Three fetch hops then an answer. Asserted on the message list the model is
    actually handed at the last call: the first page is a handle, the page it
    just fetched is whole, and the prompt is smaller than the raw material.
    """
    import asyncio

    pages = {"u1": _big(900), "u2": _big(900), "u3": _big(900)}
    seen: list[list] = []

    class _Chat:
        def __init__(self):
            self.n = 0

        async def __call__(self, messages):
            seen.append([dict(m) for m in messages])
            self.n += 1
            if self.n <= 3:
                url = f"u{self.n}"
                return {"text": "", "provider": "f", "tool_calls": [
                    {"id": f"call{self.n}", "name": "fetch_url",
                     "arguments": {"url": f"https://example.test/{url}"}}]}
            return {"text": "done", "tool_calls": [], "provider": "f"}

    async def _dispatch(name, args):
        return pages[args["url"].rsplit("/", 1)[-1]]

    out = asyncio.run(mr.run_tool_loop(
        messages=[{"role": "user", "content": "read three pages"}],
        chat_fn=_Chat(), dispatch_fn=_dispatch, max_hops=0))
    assert out.get("text") == "done"

    final = seen[-1]
    tools = [m for m in final if m.get("role") == "tool"]
    assert len(tools) == 3, [t.get("tool_call_id") for t in tools]
    first = tools[0]["content"]
    assert '"folded"' in first[:80], "the first page was never folded"
    assert "art:" in first
    assert pages["u1"].startswith(first.split('"preview": "')[1][:60])
    # The newest is still whole: the node may be writing from it right now.
    assert tools[2]["content"] == pages["u3"]
    # And the prompt really got smaller than the material it replaced.
    assert sum(len(str(m.get("content") or "")) for m in final) < sum(
        len(v) for v in pages.values())


def test_the_loop_folds_a_result_the_model_has_had_a_turn_to_use():
    """The first hop's page is folded by the third call, not the second."""
    import asyncio

    big = _big(900)
    seen: list[list] = []

    class _Chat:
        def __init__(self):
            self.n = 0

        async def __call__(self, messages):
            seen.append([dict(m) for m in messages])
            self.n += 1
            if self.n == 1:
                return {"text": "", "provider": "f", "tool_calls": [
                    {"id": "c1", "name": "fetch_url",
                     "arguments": {"url": "https://example.test/a"}}]}
            if self.n == 2:
                return {"text": "", "provider": "f", "tool_calls": [
                    {"id": "c2", "name": "fetch_url",
                     "arguments": {"url": "https://example.test/b"}}]}
            return {"text": "answered", "tool_calls": [], "provider": "f"}

    async def _same(name, args):
        return big

    asyncio.run(mr.run_tool_loop(
        messages=[{"role": "user", "content": "read two pages"}],
        chat_fn=_Chat(), dispatch_fn=_same, max_hops=0))

    # Call 2 saw call 1's page whole — it had only just been read.
    tools_at_2 = [m for m in seen[1] if m.get("role") == "tool"]
    assert len(tools_at_2) == 1 and '"folded"' not in tools_at_2[0]["content"][:80]
    # Call 3 has consumed it, so it is a handle by then.
    tools_at_3 = [m for m in seen[2] if m.get("role") == "tool"]
    assert '"folded"' in tools_at_3[0]["content"][:80]


def test_both_mechanisms_are_off_by_default_for_a_budgeted_caller():
    """The old guard only ran when a caller set max_context_chars, which is why
    it never fired for the researcher. Folding must not need that."""
    import inspect
    src = inspect.getsource(mr.run_tool_loop)
    fold_at = src.index("_fold_consumed(")
    elide_at = src.index("if max_context_chars > 0 or max_context_tokens > 0:")
    assert fold_at < elide_at, (
        "folding must run before the budgeted elision, not inside it")
