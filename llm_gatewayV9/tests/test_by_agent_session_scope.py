"""The gateway's per-session cost rollup must actually see that session.

`db.by_agent(session=...)` filtered on `ts >= <start of the current UTC day>`
*as well as* the session. Any conversation whose calls were not made today
returned zero rows — measured against the live store, 1,870 of 1,946 sessions
with agent-tagged calls (96%) were invisible.

The agent's Ledger consumes this endpoint, and an empty result is
indistinguishable from "no spend". So the page rendered a confident $0.00 for
conversations that had been billed, and the per-turn cost writer, which trusts
the same numbers as its baseline, wrote no turn at all for 45 of 71 sessions.
A failure would at least have been visible; this was a success-shaped lie.
"""

from __future__ import annotations

import time

import pytest

import db as gdb


@pytest.fixture()
def seeded(tmp_path, monkeypatch):
    """A call table with one session from today and one from last week.

    `DB_PATH` is read inside `conn()` as a module global, so patching the
    attribute is enough to point the whole module at a temp database.
    """
    monkeypatch.setattr(gdb, "DB_PATH", str(tmp_path / "gateway.db"))
    gdb.init()
    now = time.time()
    old = now - 14 * 86400
    with gdb.conn() as c:
        c.execute(
            "INSERT INTO calls (ts, session, agent, provider, model, "
            "input_tokens, output_tokens, latency_ms, retries, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, "s8-today", "researcher", "groq", "m", 1000, 200,
             50, 0, "ok"))
        c.execute(
            "INSERT INTO calls (ts, session, agent, provider, model, "
            "input_tokens, output_tokens, latency_ms, retries, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (old, "s8-lastweek", "researcher", "groq", "m", 45462, 900,
             50, 0, "ok"))
    return "s8-lastweek"


def test_an_old_session_is_still_visible_when_named(seeded):
    rows = gdb.by_agent(session=seeded)
    assert rows, (
        "naming a session still applies the calendar-day floor, so 96% of "
        "sessions report zero spend on a page that renders $0.00 as fact")
    assert "researcher" in rows, rows


def test_the_old_sessions_tokens_are_counted(seeded):
    rows = gdb.by_agent(session=seeded)
    total_in = sum(int(r.get("in_tok") or 0)
                   for group in rows.values() for r in group)
    assert total_in >= 45462, (
        f"expected the older session's 45,462 input tokens, got {total_in}")


def test_the_unscoped_rollup_is_still_day_scoped(seeded):
    """Fixing the scoped path must not make the all-time view unbounded."""
    today_only = gdb.by_agent()
    scoped = gdb.by_agent(session="s8-lastweek")
    today_in = sum(int(r.get("in_tok") or 0)
                   for group in today_only.values() for r in group)
    old_in = sum(int(r.get("in_tok") or 0)
                 for group in scoped.values() for r in group)
    assert today_in < old_in, (
        "the day floor was dropped from the unscoped rollup as well")
    assert today_in == 1000, today_in


def test_explicit_since_is_still_honoured(seeded):
    assert not gdb.by_agent(session=seeded, since=time.time() + 3600), (
        "an explicit future `since` must exclude everything")


def test_an_unknown_session_returns_empty_not_an_error(seeded):
    assert gdb.by_agent(session="s8-never-existed") == {}
