"""What Gemini is actually sent about thinking, and what the ledger records.

Three separate defects, all invisible because nothing asserted the wire format:

1. `_gemini_thinking_knob` returned None for every `flash-lite` model. Both
   live models ARE flash-lite, so no `thinkingConfig` was ever built, Google's
   own default ran every call, and the gateway had no way to ask for less or
   tell whether it had thought at all.
2. `reasoning="off"` was a no-op even for models with a knob — the guard read
   `if reasoning and reasoning != "off"`, so asking to stop thinking sent
   nothing.
3. `thoughtsTokenCount` was read out of `usageMetadata` and thrown away, so a
   call that spent 80 s thinking and returned one token looked identical to a
   hung request, and every tokens/s figure computed from `output_tokens` alone
   was wrong by the whole thinking budget.

`reasoning=None` — what the agent sends today — still sends no config, so this
changes default traffic not at all. It makes the control exist.
"""

from __future__ import annotations

import sqlite3

import providers
from providers import _gemini_thinking_config


LIVE = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-lite"]


def test_the_live_models_have_a_knob():
    for m in LIVE:
        assert providers._gemini_thinking_knob(m) == "level", (
            f"{m} is a live model and has no thinking knob, so every call to it "
            f"runs on Google's default and the gateway cannot say otherwise")


def test_off_sends_the_floor_on_gemini_3():
    for m in LIVE:
        assert _gemini_thinking_config(m, "off") == {"thinkingLevel": "minimal"}


def test_off_sends_a_zero_budget_on_2_5_flash():
    assert _gemini_thinking_config("gemini-2.5-flash", "off") == {"thinkingBudget": 0}


def test_a_requested_level_is_passed_through():
    assert _gemini_thinking_config("gemini-3.5-flash-lite", "high") == {"thinkingLevel": "high"}
    assert _gemini_thinking_config("gemini-2.5-flash", "low") == {
        "thinkingBudget": providers._GEMINI_BUDGETS["low"]}


def test_default_traffic_is_unchanged():
    """`reasoning=None` is what the agent sends on every call. It must still
    send nothing, or this fix would silently change the shape of all traffic."""
    for m in LIVE + ["gemini-2.5-flash", "gemini-2.5-pro"]:
        assert _gemini_thinking_config(m, None) is None


def test_models_that_genuinely_cannot_think_stay_silent():
    assert _gemini_thinking_config("gemini-1.5-flash", "off") is None
    assert _gemini_thinking_config("gemini-2.0-flash-lite", "high") is None
    assert _gemini_thinking_config("gpt-4o", "off") is None


# ── the ledger now carries thinking and the rate ────────────────────────
def test_log_call_records_thinking_and_computes_the_rate(tmp_path, monkeypatch):
    """tokens/s is only meaningful over the tokens the model GENERATED, and
    thinking tokens are generated. Dividing by `output_tokens` alone made a
    1,100-input / 1-output / 80 s call read as 0.01 tok/s with nothing to
    explain it."""
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ledger.db"))
    db.init()
    db.log_call(provider="gemini35lite", model="gemini-3.5-flash-lite",
                input_tokens=1100, output_tokens=20, thinking_tokens=180,
                latency_ms=8000, status="ok", prompt_chars=4400,
                response_chars=80, agent="chat")

    row = sqlite3.connect(tmp_path / "ledger.db").execute(
        "SELECT thinking_tokens, output_tps FROM calls").fetchone()
    assert row[0] == 180
    # 200 generated tokens over 8 s.
    assert abs(row[1] - 25.0) < 0.01, f"rate was {row[1]}, not the generated-token rate"


def test_log_call_without_thinking_still_records_a_rate(tmp_path, monkeypatch):
    import db

    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ledger2.db"))
    db.init()
    db.log_call(provider="kilo", model="deepseek-v3", input_tokens=500,
                output_tokens=100, latency_ms=2000, status="ok")
    row = sqlite3.connect(tmp_path / "ledger2.db").execute(
        "SELECT thinking_tokens, output_tps FROM calls").fetchone()
    assert row[0] == 0
    assert abs(row[1] - 50.0) < 0.01


def test_the_migration_adds_the_columns_to_an_older_database(tmp_path, monkeypatch):
    """The live `gateway_v8.db` has 107k rows and no thinking column. If the
    ALTERs do not run, every new write raises and the ledger silently stops
    recording."""
    import db

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL, provider TEXT, model TEXT, input_tokens INTEGER,
        output_tokens INTEGER, cache_create_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0, latency_ms INTEGER, status TEXT,
        error TEXT, prompt_chars INTEGER, response_chars INTEGER,
        override TEXT, attempted TEXT, tool_calls INTEGER DEFAULT 0,
        reasoning_applied INTEGER DEFAULT 0, tool_dialect TEXT,
        call_role TEXT, router_decision TEXT, embed_dim INTEGER,
        agent TEXT, session TEXT, retries INTEGER DEFAULT 0, channel TEXT,
        trust_level TEXT, policy_verdict TEXT, policy_rule TEXT, user_id TEXT)""")
    con.execute("INSERT INTO calls (ts, provider, model, latency_ms, status,"
                " prompt_chars, response_chars) VALUES (1,'p','m',10,'ok',40,8)")
    con.commit()
    con.close()

    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init()          # the gateway runs this at startup; the ALTERs live there
    db.log_call(provider="gemini35lite", model="gemini-3.5-flash-lite",
                input_tokens=10, output_tokens=4, thinking_tokens=90,
                latency_ms=1500, status="ok")

    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(calls)")}
    assert {"thinking_tokens", "output_tps"} <= cols
    kept = sqlite3.connect(path).execute(
        "SELECT thinking_tokens, output_tps FROM calls ORDER BY id").fetchall()
    assert kept[0] == (0, None), "the migration rewrote the historical row"
    assert kept[1][0] == 90
