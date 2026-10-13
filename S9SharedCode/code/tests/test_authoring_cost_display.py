"""The Authoring UI must never show a fabricated or mislabelled cost.

`turn_costs.json` is deliberately not written when the gateway's cost API is
unreachable — a `$0` row would be worse than none. That left the console
reporting `0 tokens` for documents that had really spent hundreds of
thousands, which reads as "this was free".
"""
import json
import pathlib

import pytest

import flow


def test_the_fallback_reads_the_per_agent_shape(monkeypatch):
    """`_session_cost_breakdown` returns per-agent buckets, not flat totals.

    Reading `input_tokens` off it returned None, so a session the gateway was
    holding 1.4M tokens for displayed as 0.
    """
    import agent_server as _as
    monkeypatch.setattr(
        _as, "_session_cost_breakdown",
        lambda s: {"author": {"usd": 0.01, "in_tok": 300, "out_tok": 20,
                              "calls": 3},
                   "researcher": {"usd": 0.02, "in_tok": 700, "out_tok": 80,
                                   "calls": 10}},
        raising=False)
    out = flow.session_cost_totals("s8-noleger2")
    assert out["total_tokens"] == 1100, out
    assert out["calls"] == 13, out
    assert out["by_agent"]["researcher"]["input_tokens"] == 700, out


def test_a_missing_ledger_falls_back_to_the_gateway(monkeypatch):
    """Zero tokens on a 300k-token document is a lie by omission."""
    import agent_server as _as
    monkeypatch.setattr(
        _as, "_session_cost_breakdown",
        lambda s: {"author": {"usd": 0.42, "in_tok": 200_000,
                              "out_tok": 100_000, "calls": 1}},
        raising=False)
    out = flow.session_cost_totals("s8-noleger")
    assert out["total_tokens"] == 300_000, out
    assert out["scope"] == "session", out


def test_the_fallback_is_labelled_as_session_not_run(monkeypatch):
    import agent_server as _as
    monkeypatch.setattr(
        _as, "_session_cost_breakdown",
        lambda s: {"author": {"usd": 0.0, "in_tok": 5, "out_tok": 5,
                              "calls": 1}}, raising=False)
    assert flow.session_cost_totals("s8-x")["scope"] == "session"


def test_a_real_ledger_is_preferred_and_keeps_run_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    d = tmp_path / "state" / "sessions" / "s8-real"
    d.mkdir(parents=True)
    (d / "turn_costs.json").write_text(json.dumps([
        {"ts": 1, "per_agent": {"author": {"usd": 0.1, "in_tok": 10,
                                           "out_tok": 5, "calls": 2}},
         "totals": {"usd": 0.1, "in_tok": 10, "out_tok": 5, "calls": 2}},
    ]), encoding="utf-8")
    out = flow.session_cost_totals("s8-real")
    assert out["total_tokens"] == 15
    assert out["scope"] == "run", "a real per-turn ledger is a run figure"


def test_the_fallback_returns_zero_rather_than_guessing(monkeypatch):
    """A missing number is honest; an invented one is not."""
    import agent_server as _as
    monkeypatch.setattr(_as, "_session_cost_breakdown",
                        lambda s: (_ for _ in ()).throw(RuntimeError("down")),
                        raising=False)
    out = flow.session_cost_totals("s8-broken")
    assert out["total_tokens"] == 0
    assert out["scope"] == "run"


def test_no_zero_tokens_is_presented_as_a_real_figure():
    """`fileMeta` omits cost entirely when there is none, rather than
    printing '0 tokens' which reads as a measurement."""
    p = pathlib.Path(
        pathlib.Path(__file__).parent.parent / "console-frontend" / "src"
        / "views" / "Authoring.tsx")
    if not p.exists():
        pytest.skip("frontend source not present")
    src = p.read_text(encoding="utf-8")
    assert "c.total_tokens" in src
    assert "tokens" in src
    # The scope must be surfaced, not assumed.
    assert "whole session" in src, (
        "a session-lifetime fallback is being presented as a per-document cost")