"""Tokens per document, reported in the done frame.

The user wants to see what each produced document cost. `turn_costs.json`
already records per-turn deltas, but nothing reached the console, and a run
that renders two files gave no way to attribute either.
"""
import json
import pathlib

import pytest

import flow


def _row(in_tok, out_tok, usd=0.0, calls=1):
    """The REAL sidecar shape: totals + per_agent, not flat token keys."""
    return {"ts": 1791769381.18, "query": "q",
            "per_agent": {"researcher": {"usd": usd, "in_tok": in_tok,
                                          "out_tok": out_tok, "calls": calls}},
            "totals": {"usd": usd, "in_tok": in_tok, "out_tok": out_tok,
                       "calls": calls}}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    sid = "s8-tokens"
    d = tmp_path / "state" / "sessions" / sid
    d.mkdir(parents=True)
    return sid, d


def test_turn_costs_are_summarised_for_the_frame(session):
    """Real schema: `in_tok`/`out_tok` under `totals` and `per_agent`.

    Reading top-level `input_tokens` returned zero for every real run, which
    is why the console showed no cost at all.
    """
    sid, d = session
    (d / "turn_costs.json").write_text(json.dumps([
        _row(12_000, 3_000, 0.02, 4),
        _row(20_000, 8_000, 0.05, 3),
    ]), encoding="utf-8")

    tot = flow.session_cost_totals(sid)
    assert tot["input_tokens"] == 32_000
    assert tot["output_tokens"] == 11_000
    assert tot["total_tokens"] == 43_000
    assert tot["turns"] == 2
    assert tot["calls"] == 7
    assert tot["cost"] == pytest.approx(0.07)


def test_cost_is_broken_down_by_skill(session):
    sid, d = session
    (d / "turn_costs.json").write_text(json.dumps([
        {"ts": 1, "per_agent": {
            "researcher": {"usd": 0.01, "in_tok": 50_000, "out_tok": 5_000,
                           "calls": 6},
            "author": {"usd": 0.04, "in_tok": 30_000, "out_tok": 9_000,
                       "calls": 1}},
         "totals": {"usd": 0.05, "in_tok": 80_000, "out_tok": 14_000,
                    "calls": 7}},
    ]), encoding="utf-8")
    by = flow.session_cost_totals(sid)["by_agent"]
    assert by["researcher"]["calls"] == 6
    assert by["author"]["output_tokens"] == 9_000


def test_a_row_with_only_per_agent_still_totals(session):
    """Some rows carry per_agent but no totals; it must not read as zero."""
    sid, d = session
    (d / "turn_costs.json").write_text(json.dumps([
        {"ts": 1, "per_agent": {"planner": {"usd": 0.0, "in_tok": 5103,
                                            "out_tok": 900, "calls": 1}}},
    ]), encoding="utf-8")
    tot = flow.session_cost_totals(sid)
    assert tot["total_tokens"] == 5103 + 900
    assert tot["input_tokens"] == 5103


def test_missing_sidecar_is_zero_not_a_crash(session):
    sid, _d = session
    tot = flow.session_cost_totals(sid)
    assert tot["total_tokens"] == 0
    assert tot["turns"] == 0


def test_a_corrupt_sidecar_does_not_break_the_frame(session):
    sid, d = session
    (d / "turn_costs.json").write_text("{not json", encoding="utf-8")
    assert flow.session_cost_totals(sid)["total_tokens"] == 0


def test_tokens_are_attributed_to_each_produced_file(session):
    """One file = the run's tokens; two files must not each claim all of it."""
    import artifacts as arts
    sid, d = session
    a = arts.put(b"%PDF-1.4 one", content_type="application/pdf",
                 source="author:pdf", descriptor="One (pdf)")
    b = arts.put(b"%PDF-1.4 two", content_type="application/pdf",
                 source="author:pdf", descriptor="Two (pdf)")
    (d / "produced_files.json").write_text(json.dumps([
        {"artifact": a, "filename": "One.pdf", "format": "pdf",
         "skill": "author", "bytes": 9, "blocks": 10,
         "stats": {"words": 500, "sections": 3, "format": "pdf"}},
        {"artifact": b, "filename": "Two.pdf", "format": "pdf",
         "skill": "author", "bytes": 9, "blocks": 20,
         "stats": {"words": 900, "sections": 5, "format": "pdf"}},
    ]), encoding="utf-8")
    (d / "turn_costs.json").write_text(json.dumps([_row(1000, 9000, 0.1)]),
                                       encoding="utf-8")

    files = flow.take_produced_files(sid)
    assert len(files) == 2
    # Shared, explicitly: this is per-RUN, and the frame says so, rather than
    # implying each document cost 10k on its own.
    for f in files:
        assert f["cost"]["total_tokens"] == 10_000
        assert f["cost"]["scope"] == "run"


def test_a_path_traversal_session_id_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(flow, "_SROOT", tmp_path)
    for bad in ("../../etc", "a/b", "..", ""):
        assert flow.session_cost_totals(bad)["total_tokens"] == 0