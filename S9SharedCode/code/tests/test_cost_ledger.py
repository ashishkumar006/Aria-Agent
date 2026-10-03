"""P2.3 — Cost ledger tests (deterministic, no live LLM).

Tests the per-turn cost accumulation and /api/cost scoping logic in
`agent_server.py` without needing a running gateway.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_turn_costs(session_id: str, entries: list[dict], cost_dir: Path | None = None) -> None:
    """Write a fake turn_costs.json for a session."""
    import agent_server as ag
    base = cost_dir or ag._TURN_COST_DIR
    p = base / session_id / "turn_costs.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _read_turn_costs(session_id: str) -> list:
    from agent_server import _read_turn_costs as _r
    return _r(session_id)


# ── tests ─────────────────────────────────────────────────────────────────────

class TestTurnCostLedger:
    """Per-turn cost accumulation in state/sessions/<sid>/turn_costs.json."""

    def test_empty_ledger_returns_empty(self):
        costs = _read_turn_costs("nonexistent-session-xyz")
        assert costs == []

    def test_single_turn_recorded(self, tmp_path, monkeypatch):
        """One turn produces one entry with per_agent + totals."""
        sid = "t-cost-1"
        _write_turn_costs(sid, [
            {
                "ts": 1000.0,
                "query": "hello",
                "per_agent": {
                    "planner": {"usd": 0.001, "in_tok": 50, "out_tok": 20, "calls": 1},
                },
                "totals": {"usd": 0.001, "in_tok": 50, "out_tok": 20, "calls": 1},
            }
        ])
        costs = _read_turn_costs(sid)
        assert len(costs) == 1
        assert costs[0]["totals"]["calls"] == 1
        assert costs[0]["per_agent"]["planner"]["usd"] == 0.001

    def test_multiple_turns_accumulate(self):
        """Two turns produce two entries; totals sum across turns."""
        sid = "t-cost-2"
        _write_turn_costs(sid, [
            {
                "ts": 1000.0,
                "query": "q1",
                "per_agent": {"planner": {"usd": 0.001, "in_tok": 50, "out_tok": 20, "calls": 1}},
                "totals": {"usd": 0.001, "in_tok": 50, "out_tok": 20, "calls": 1},
            },
            {
                "ts": 2000.0,
                "query": "q2",
                "per_agent": {"researcher": {"usd": 0.002, "in_tok": 100, "out_tok": 40, "calls": 1}},
                "totals": {"usd": 0.002, "in_tok": 100, "out_tok": 40, "calls": 1},
            },
        ])
        costs = _read_turn_costs(sid)
        assert len(costs) == 2
        total_usd = sum(c["totals"]["usd"] for c in costs)
        assert total_usd == pytest.approx(0.003)

    def test_corrupt_ledger_returns_empty(self, tmp_path, monkeypatch):
        """A corrupt turn_costs.json must not crash the cost endpoint."""
        sid = "t-cost-corrupt"
        p = tmp_path / "sessions" / sid / "turn_costs.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not valid json", encoding="utf-8")

        import agent_server as ag
        monkeypatch.setattr(ag, "_TURN_COST_DIR", tmp_path / "sessions")
        costs = ag._read_turn_costs(sid)
        assert costs == []


class TestCostEndpoint:
    """The /api/cost endpoint aggregates per-agent spend correctly."""

    def test_cost_scoped_to_conversation(self, tmp_path, monkeypatch):
        """When two conversations share a session prefix, /api/cost must
        only return the requested conversation's ledger."""
        import asyncio

        from agent_server import cost_dashboard

        import agent_server as ag
        monkeypatch.setattr(ag, "_TURN_COST_DIR", tmp_path)

        # Write two separate ledgers under the monkeypatched directory.
        for sid, usd, agent_name in [
            ("s8-aaa", 0.1, "planner"),
            ("s8-bbb", 0.2, "researcher"),
        ]:
            _write_turn_costs(sid, [
                {
                    "ts": 1000.0,
                    "query": f"conv {sid[-3:]} q1",
                    "per_agent": {
                        agent_name: {"usd": usd, "in_tok": 10, "out_tok": 5, "calls": 1}
                    },
                    "totals": {"usd": usd, "in_tok": 10, "out_tok": 5, "calls": 1},
                }
            ], cost_dir=tmp_path)

        # Mock resolve_session to return our fixed session ids.
        with mock.patch.object(ag, "resolve_session", lambda cid: f"s8-{cid}"):
            # conversation_id "aaa" → session s8-aaa
            r_a = asyncio.run(cost_dashboard(conversation_id="aaa"))
            assert r_a["totals"]["dollars"] == pytest.approx(0.1)
            assert r_a["totals"]["calls"] == 1
            assert not any(row["agent"] == "researcher" for row in r_a["rows"])

            # conversation_id "bbb" → session s8-bbb
            r_b = asyncio.run(cost_dashboard(conversation_id="bbb"))
            assert r_b["totals"]["dollars"] == pytest.approx(0.2)
            assert r_b["totals"]["calls"] == 1

    def test_cost_fallback_when_ledger_empty(self, tmp_path, monkeypatch):
        """When turn_costs.json is empty, /api/cost falls back to the
        gateway's per-session snapshot (returns empty rows, not 500)."""
        import asyncio

        from agent_server import cost_dashboard

        import agent_server as ag
        monkeypatch.setattr(ag, "_TURN_COST_DIR", tmp_path)

        with mock.patch.object(ag, "resolve_session", return_value="s8-empty"):
            r = asyncio.run(cost_dashboard(conversation_id="empty"))
            assert "rows" in r
            assert "totals" in r
            assert r["totals"]["calls"] == 0
