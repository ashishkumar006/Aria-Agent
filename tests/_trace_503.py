"""Trace the 503s around the AI-summary turn (ts≈1787601152) from the gateway
SQLite call log, and reconstruct what the formatter received."""
import json, sqlite3, pathlib

DB = None
for cand in [
    pathlib.Path("../../llm_gatewayV9/gateway_v9.db"),
    pathlib.Path("../../llm_gatewayV9/gateway_v8.db"),
]:
    if cand.exists():
        DB = cand
        break
else:
    print("DB:", DB)
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT ts, provider, model, status, error FROM calls "
        "WHERE status='error' ORDER BY ts DESC LIMIT 30"
    ).fetchall()
    window_start = 1787601152 - 300
    in_window = [r for r in rows if r["ts"] >= window_start]
    print(f"recent errors: {len(rows)}, within turn window: {len(in_window)}")
    for r in in_window[:12]:
        err = str(r["error"] or "")[:110]
        print(f"  {r['provider']:14} | {err}")
