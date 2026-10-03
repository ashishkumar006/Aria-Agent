"""Tool ledger: per-tool completions recorded apart from call counts.

Uses a throwaway DB file (monkeypatched DB_PATH) — never touches the
live gateway_v8.db.
"""
import db


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init()


def test_log_and_aggregate(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    assert db.tool_usage() == []
    db.log_tool_use("web_search", provider="gemini", model="m",
                    agent="researcher", session="s1")
    db.log_tool_use("web_search", provider="gemini", model="m",
                    agent="researcher", session="s1")
    db.log_tool_use("fetch_url", provider="groq", model="m2",
                    agent="researcher", session="s1", status="error")
    rows = {r["tool"]: r for r in db.tool_usage()}
    assert set(rows) == {"web_search", "fetch_url"}
    assert rows["web_search"]["uses"] == 2
    assert rows["web_search"]["ok"] == 2
    assert rows["web_search"]["errors"] == 0
    assert rows["web_search"]["providers"] == ["gemini"]
    assert rows["web_search"]["agents"] == ["researcher"]
    assert rows["fetch_url"]["errors"] == 1
    assert rows["web_search"]["last_ts"] > 0


def test_day_scoping_and_name_cap(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    db.log_tool_use("x" * 500, provider="gemini")
    rows = db.tool_usage()
    assert len(rows[0]["tool"]) == 200
    assert db.tool_usage(since=9999999999) == []
